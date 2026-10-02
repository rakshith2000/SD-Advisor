"""Self-service role elevation.

A VIEWER asks for LEAD, every administrator is emailed, one of them decides.

            open_request()
    (none) --------------> PENDING ---- approve ---> APPROVED   terminal
                              |
                              |-------- reject ----> REJECTED   terminal
                              |---- requester ------> CANCELLED terminal
                              '---- nightly sweep --> EXPIRED   terminal

Three things in here are load-bearing and easy to get wrong.

Decisions are guarded in SQL, not in Python. The request email goes to every
administrator at once, so two of them opening it and clicking Approve within
seconds of each other is ordinary, not exotic. A read-then-write would apply
the grant twice and record the second approver over the first; an UPDATE that
carries `AND status = 'PENDING'` applies once and reports honestly which it
was.

Approval is also a mail subscription. leads_for() selects on
role IN ('ADMIN','LEAD'), so granting LEAD starts delivering the daily digest
and every risk alert batch to that address - messages carrying incident
descriptions and caller names. The caller is expected to surface that before
the administrator clicks, which is why granted_groups is part of the decision
rather than copied from the request.

And the ceiling is deliberate. allowed_targets does not include ADMIN: an
administrator can change the scoring weights and trigger live mail to the
whole lead group, so that grant is made directly by someone who already holds
it, never through a form.
"""

import datetime
from typing import Any, Dict, List, Optional, Sequence

import pymysql

from core.logging_setup import get_logger
from core.timeutil import utc_now

log = get_logger('web.access')

PENDING = 'PENDING'
APPROVED = 'APPROVED'
REJECTED = 'REJECTED'
EXPIRED = 'EXPIRED'
CANCELLED = 'CANCELLED'

TERMINAL = (APPROVED, REJECTED, EXPIRED, CANCELLED)

# Outcomes of a decision attempt. ALREADY_DECIDED is not an error - it is the
# expected result of the second administrator clicking, and the caller renders
# who got there first rather than an exception.
APPLIED = 'APPLIED'
ALREADY_DECIDED = 'ALREADY_DECIDED'
NOT_FOUND = 'NOT_FOUND'
REFUSED = 'REFUSED'


class AccessRequestError(ValueError):
    """Raised for a request the policy will not accept.

    Carries a message written for the person who will read it on screen.
    """


def _split(value: Optional[str]) -> List[str]:
    return [g.strip() for g in (value or '').split(',') if g.strip()]


def _join(groups: Optional[Sequence[str]]) -> Optional[str]:
    cleaned = [g.strip() for g in (groups or []) if g and g.strip()]
    # Order-insensitive and de-duplicated, so two administrators selecting the
    # same queues in a different order produce the same stored value.
    return ', '.join(sorted(set(cleaned), key=str.lower)) or None


class RoleRequestService:
    def __init__(self, db, users, settings):
        self.db = db
        self.users = users
        self.settings = settings

    # -- configuration -----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get('auth.role_requests.enabled', True))

    @property
    def allowed_targets(self) -> List[str]:
        return list(self.settings.get('auth.role_requests.allowed_targets', ['LEAD']))

    @property
    def expire_after_days(self) -> int:
        return int(self.settings.get('auth.role_requests.expire_after_days', 14))

    @property
    def reject_cooldown_days(self) -> int:
        return int(self.settings.get('auth.role_requests.reject_cooldown_days', 30))

    @property
    def min_justification(self) -> int:
        return int(self.settings.get('auth.role_requests.require_justification_chars', 20))

    # -- reads -------------------------------------------------------------

    def get(self, request_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.query_one('SELECT * FROM role_request WHERE id = %s', (request_id,))
        return self._decorate(row) if row else None

    def pending(self) -> List[Dict[str, Any]]:
        rows = self.db.retrieve(
            'role_request',
            conditions=[{'col': 'status', 'op': 'eq', 'val': PENDING}],
            order_by='created_at ASC')
        return [self._decorate(r) for r in rows]

    def pending_count(self) -> int:
        row = self.db.query_one(
            'SELECT COUNT(*) AS n FROM role_request WHERE status = %s', (PENDING,))
        return int((row or {}).get('n') or 0)

    def history_for(self, username: str, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self.db.retrieve(
            'role_request',
            conditions=[{'col': 'username', 'op': 'eq', 'val': username}],
            order_by='created_at DESC', limit=limit)
        return [self._decorate(r) for r in rows]

    def open_request_for(self, username: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            'SELECT * FROM role_request WHERE username = %s AND status = %s '
            ' ORDER BY id DESC LIMIT 1', (username, PENDING))
        return self._decorate(row) if row else None

    @staticmethod
    def _decorate(row: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(row)
        row['requested_group_list'] = _split(row.get('requested_groups'))
        row['granted_group_list'] = _split(row.get('granted_groups'))
        row['is_open'] = row.get('status') == PENDING
        return row

    # -- opening a request -------------------------------------------------

    def open_request(self, user: Dict[str, Any], to_role: str,
                     justification: str, requested_groups: Sequence[str],
                     valid_groups: Sequence[str]) -> Dict[str, Any]:
        """Record a request. Raises AccessRequestError with a readable reason."""
        if not self.enabled:
            raise AccessRequestError('Access requests are not enabled on this service.')

        to_role = (to_role or '').strip().upper()
        if to_role not in self.allowed_targets:
            raise AccessRequestError(
                f'{to_role or "That role"} cannot be requested here. '
                f'Requestable roles: {", ".join(self.allowed_targets) or "none"}.')

        current = (user.get('role') or '').upper()
        if current == to_role:
            raise AccessRequestError(f'Your account already has the {to_role} role.')

        # Rank rather than string compare, so this still holds if a role is
        # ever inserted between the existing three.
        order = {'VIEWER': 0, 'LEAD': 1, 'ADMIN': 2}
        if order.get(current, 0) > order.get(to_role, 0):
            raise AccessRequestError(
                f'Your account already has {current}, which is broader than {to_role}. '
                f'Ask an administrator if you need it reduced.')

        justification = (justification or '').strip()
        if len(justification) < self.min_justification:
            raise AccessRequestError(
                f'Please give a reason of at least {self.min_justification} characters '
                f'so the administrator has something to judge (you wrote {len(justification)}).')

        # Only groups the service actually knows about. A near-miss on an
        # assignment group name does not error anywhere downstream - it simply
        # matches no tickets - so it is rejected at the point of entry instead.
        known = {g.strip().lower(): g.strip() for g in valid_groups}
        chosen, unknown = [], []
        for group in requested_groups or []:
            match = known.get((group or '').strip().lower())
            (chosen if match else unknown).append(match or group)
        if unknown:
            raise AccessRequestError(
                f'Not a known assignment group: {", ".join(str(u) for u in unknown)}.')
        if not chosen:
            raise AccessRequestError(
                'Select at least one assignment group you need access to.')

        self._refuse_if_in_cooldown(user['username'], to_role)

        now = utc_now()
        record = {
            'username': user['username'],
            'external_id': user.get('external_id'),
            'full_name': user.get('full_name') or user['username'],
            'email': user.get('email'),
            'from_role': current or 'VIEWER',
            'to_role': to_role,
            'justification': justification[:4000],
            'requested_groups': _join(chosen),
            'status': PENDING,
            'expires_at': now + datetime.timedelta(days=self.expire_after_days),
            'created_at': now,
        }

        try:
            self.db.insert('role_request', record)
        except pymysql.err.IntegrityError:
            # uq_pending. Reached by a double submit or two tabs, so it is a
            # normal outcome rather than a fault.
            existing = self.open_request_for(user['username'])
            raise AccessRequestError(
                'You already have a request awaiting a decision'
                + (f" (opened {existing['created_at']:%d %b %Y})." if existing else '.'))

        log.info('%s requested %s (groups=%s)', user['username'], to_role, record['requested_groups'])
        return self.open_request_for(user['username']) or {}

    def _refuse_if_in_cooldown(self, username: str, to_role: str) -> None:
        if self.reject_cooldown_days <= 0:
            return
        cutoff = utc_now() - datetime.timedelta(days=self.reject_cooldown_days)
        recent = self.db.query_one(
            'SELECT decided_at, decision_note FROM role_request '
            ' WHERE username = %s AND to_role = %s AND status = %s AND decided_at >= %s '
            ' ORDER BY decided_at DESC LIMIT 1',
            (username, to_role, REJECTED, cutoff))
        if not recent:
            return
        # The administrator can still grant directly; the cooldown only stops
        # the form being used to re-ask.
        raise AccessRequestError(
            f'A previous request was declined on {recent["decided_at"]:%d %b %Y}. '
            f'You can ask again after {self.reject_cooldown_days} days, or speak to an '
            f'administrator if the situation has changed.')

    # -- deciding ----------------------------------------------------------

    def approve(self, request_id: int, admin: Dict[str, Any],
                granted_groups: Sequence[str], note: str = '',
                valid_groups: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        request = self.get(request_id)
        if not request:
            return {'outcome': NOT_FOUND}

        refusal = self._refuse_decision(request, admin)
        if refusal:
            return {'outcome': REFUSED, 'reason': refusal, 'request': request}

        if valid_groups is not None:
            known = {g.strip().lower() for g in valid_groups}
            unknown = [g for g in granted_groups or []
                       if (g or '').strip().lower() not in known]
            if unknown:
                return {'outcome': REFUSED, 'request': request,
                        'reason': f'Not a known assignment group: {", ".join(unknown)}.'}

        groups = _join(granted_groups) or request.get('requested_groups')
        if not groups:
            return {'outcome': REFUSED, 'request': request,
                    'reason': 'Select at least one assignment group to grant.'}

        # The guard. Zero affected rows means another administrator decided
        # first, which the caller renders rather than treating as a failure.
        applied = self.db.execute(
            'UPDATE role_request '
            '   SET status = %s, decided_by = %s, decided_at = %s, '
            '       granted_groups = %s, decision_note = %s '
            ' WHERE id = %s AND status = %s',
            (APPROVED, admin['username'], utc_now(), groups,
             (note or '').strip()[:2000] or None, request_id, PENDING))

        if not applied:
            return {'outcome': ALREADY_DECIDED, 'request': self.get(request_id)}

        # Only after the row is won, so a losing racer never writes the role.
        self.users.set_role(request['username'], request['to_role'],
                            _split(groups), source='REQUEST')

        log.warning('%s approved %s for %s (groups=%s)',
                    admin['username'], request['to_role'], request['username'], groups)
        return {'outcome': APPLIED, 'request': self.get(request_id)}

    def reject(self, request_id: int, admin: Dict[str, Any],
               note: str = '') -> Dict[str, Any]:
        request = self.get(request_id)
        if not request:
            return {'outcome': NOT_FOUND}

        refusal = self._refuse_decision(request, admin)
        if refusal:
            return {'outcome': REFUSED, 'reason': refusal, 'request': request}

        applied = self.db.execute(
            'UPDATE role_request '
            '   SET status = %s, decided_by = %s, decided_at = %s, decision_note = %s '
            ' WHERE id = %s AND status = %s',
            (REJECTED, admin['username'], utc_now(),
             (note or '').strip()[:2000] or None, request_id, PENDING))

        if not applied:
            return {'outcome': ALREADY_DECIDED, 'request': self.get(request_id)}

        log.info('%s declined %s for %s', admin['username'],
                 request['to_role'], request['username'])
        return {'outcome': APPLIED, 'request': self.get(request_id)}

    def _refuse_decision(self, request: Dict[str, Any],
                         admin: Dict[str, Any]) -> Optional[str]:
        """Reasons a decision must not proceed, checked before the SQL guard."""
        if request['username'] == admin.get('username'):
            return 'You cannot decide your own access request.'

        # An account disabled between the request and the decision - by the
        # reconcile job, or by hand - must not be silently re-granted a role
        # that would put it back on the digest.
        account = self.users.live(request['username'])
        if not account or not account.get('active'):
            return (f'{request["username"]} is no longer an active account. '
                    f'The request cannot be granted.')
        return None

    # -- requester-side ----------------------------------------------------

    def cancel(self, request_id: int, user: Dict[str, Any]) -> str:
        applied = self.db.execute(
            'UPDATE role_request SET status = %s, decided_at = %s '
            ' WHERE id = %s AND username = %s AND status = %s',
            (CANCELLED, utc_now(), request_id, user['username'], PENDING))
        return APPLIED if applied else ALREADY_DECIDED

    # -- housekeeping ------------------------------------------------------

    def expire_stale(self) -> int:
        """Age out requests nobody decided.

        Without this the administrator's badge accumulates requests from people
        who have since left, and the only signal that the workflow is being
        ignored disappears into the noise.
        """
        expired = self.db.execute(
            'UPDATE role_request SET status = %s, decided_at = %s '
            ' WHERE status = %s AND expires_at <= %s',
            (EXPIRED, utc_now(), PENDING, utc_now()))
        if expired:
            log.warning('Expired %d access request(s) that were never decided', expired)
        return expired

    def mark_notified(self, request_id: int) -> None:
        self.db.update('role_request', {'notified_at': utc_now()},
                       conditions=[{'col': 'id', 'op': 'eq', 'val': request_id}])

"""The self-service role elevation workflow.

The defects these exist for, in order of how quietly they would have failed.

A decision read-then-written in Python applies twice. The request email goes to
every administrator at once, so two of them opening it and clicking Approve
within seconds is ordinary. The guard is `AND status = 'PENDING'` carried in
the UPDATE, and the test for it has to assert both that the grant happened once
and that the second caller was told who got there first - not that an exception
was raised.

An approval is also a mail subscription. leads_for() selects on
role IN ('ADMIN','LEAD'), so a granted LEAD starts receiving the daily digest
and every risk alert batch. A request approved for an account that has since
been deactivated would quietly put a departed person back on that list.

And the privilege ceiling has to hold in the service, not only in the template:
hiding the ADMIN option in the form is presentation, and the POST is still
reachable by hand.

The fake database models the parts of MySQL this workflow depends on - the
generated pending_key column and its unique index, and the affected-row count
from a conditional UPDATE - because those are precisely the mechanisms under
test.
"""

import datetime
import sys
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.access import (ALREADY_DECIDED, APPLIED, NOT_FOUND, REFUSED,
                        AccessRequestError, RoleRequestService)

GROUPS = ['Desktop Support', 'Network Operations', 'Service Desk']
REASON = 'I run the morning triage for this queue and need to record outcomes.'


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeSettings:
    def __init__(self, **over):
        self.values = {
            'auth.role_requests.enabled': True,
            'auth.role_requests.allowed_targets': ['LEAD'],
            'auth.role_requests.expire_after_days': 14,
            'auth.role_requests.reject_cooldown_days': 30,
            'auth.role_requests.require_justification_chars': 20,
        }
        self.values.update(over)

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeDb:
    """Enough MySQL to exercise the guards: a unique index over the generated
    pending_key column, and affected-row counts from conditional updates."""

    def __init__(self):
        self.rows = []
        self._next_id = 1

    # -- the generated column ---------------------------------------------

    @staticmethod
    def _pending_key(row):
        if row['status'] != 'PENDING':
            return None          # NULLs do not collide in a unique index
        return f"{row['username']}:{row['to_role']}"

    def _assert_unique(self, candidate, ignore_id=None):
        key = self._pending_key(candidate)
        if key is None:
            return
        for row in self.rows:
            if row['id'] != ignore_id and self._pending_key(row) == key:
                raise pymysql.err.IntegrityError(1062, "Duplicate entry for key 'uq_pending'")

    # -- api ---------------------------------------------------------------

    def insert(self, table, data, ignore=False):
        assert table == 'role_request'
        row = {'id': self._next_id, 'decided_by': None, 'decided_at': None,
               'decision_note': None, 'granted_groups': None, 'notified_at': None,
               **data}
        self._assert_unique(row)
        self.rows.append(row)
        self._next_id += 1
        return 1

    def query_one(self, sql, params=None):
        params = params or ()
        if 'COUNT(*)' in sql:
            return {'n': sum(1 for r in self.rows if r['status'] == params[0])}
        if 'FROM role_request WHERE id' in sql:
            return next((dict(r) for r in self.rows if r['id'] == params[0]), None)
        if 'AND status = %s' in sql and 'username = %s' in sql and 'to_role' not in sql:
            return next((dict(r) for r in reversed(self.rows)
                         if r['username'] == params[0] and r['status'] == params[1]), None)
        raise AssertionError(f'unexpected query: {sql}')

    def query(self, sql, params=None):
        params = params or ()
        if 'to_role = %s' in sql:                      # the cooldown probe
            username, to_role, status, cutoff = params
            matches = [dict(r) for r in self.rows
                       if r['username'] == username and r['to_role'] == to_role
                       and r['status'] == status and r.get('decided_at')
                       and r['decided_at'] >= cutoff]
            return sorted(matches, key=lambda r: r['decided_at'], reverse=True)
        raise AssertionError(f'unexpected query: {sql}')

    def retrieve(self, table, columns=None, conditions=None, order_by=None, limit=None):
        rows = [dict(r) for r in self.rows]
        for condition in conditions or []:
            col, op, val = condition['col'], condition.get('op', 'eq'), condition.get('val')
            if op == 'eq':
                rows = [r for r in rows if r.get(col) == val]
            elif op == 'ni':
                rows = [r for r in rows if r.get(col) not in val]
        return rows[:limit] if limit else rows

    def execute(self, sql, params=None):
        """Conditional UPDATEs, returning the affected-row count that the
        service treats as "did I win the race"."""
        params = list(params or ())

        if 'SET status = %s, decided_by' in sql and 'granted_groups' in sql:
            status, by, at, groups, note, request_id, required = params
            return self._conditional(request_id, required, {
                'status': status, 'decided_by': by, 'decided_at': at,
                'granted_groups': groups, 'decision_note': note})

        if 'SET status = %s, decided_by' in sql:
            status, by, at, note, request_id, required = params
            return self._conditional(request_id, required, {
                'status': status, 'decided_by': by, 'decided_at': at,
                'decision_note': note})

        if 'username = %s AND status = %s' in sql:          # cancel
            status, at, request_id, username, required = params
            row = next((r for r in self.rows if r['id'] == request_id), None)
            if not row or row['status'] != required or row['username'] != username:
                return 0
            row.update({'status': status, 'decided_at': at})
            return 1

        if 'expires_at <= %s' in sql:                        # expiry sweep
            status, at, required, now = params
            hit = [r for r in self.rows
                   if r['status'] == required and r['expires_at'] <= now]
            for row in hit:
                row.update({'status': status, 'decided_at': at})
            return len(hit)

        raise AssertionError(f'unexpected statement: {sql}')

    def _conditional(self, request_id, required_status, changes):
        row = next((r for r in self.rows if r['id'] == request_id), None)
        if not row or row['status'] != required_status:
            return 0
        candidate = {**row, **changes}
        self._assert_unique(candidate, ignore_id=request_id)
        row.update(changes)
        return 1

    def update(self, table, values, conditions=None):
        request_id = conditions[0]['val']
        row = next((r for r in self.rows if r['id'] == request_id), None)
        if row:
            row.update(values)
            return 1
        return 0


class FakeUsers:
    def __init__(self, accounts=None):
        self.accounts = accounts or {
            'a.viewer': {'username': 'a.viewer', 'role': 'VIEWER', 'active': 1,
                         'email': 'a.viewer@example.com', 'assignment_groups': None},
            'the.admin': {'username': 'the.admin', 'role': 'ADMIN', 'active': 1,
                          'email': 'the.admin@example.com', 'assignment_groups': None},
            'other.admin': {'username': 'other.admin', 'role': 'ADMIN', 'active': 1,
                            'email': 'other.admin@example.com', 'assignment_groups': None},
        }
        self.role_changes = []

    def live(self, username):
        return self.accounts.get(username)

    def set_role(self, username, role, assignment_groups=None, source='MANUAL'):
        self.accounts[username]['role'] = role
        self.accounts[username]['assignment_groups'] = ', '.join(assignment_groups or [])
        self.role_changes.append((username, role, assignment_groups, source))


@pytest.fixture
def db():
    return FakeDb()


@pytest.fixture
def users():
    return FakeUsers()


@pytest.fixture
def service(db, users):
    return RoleRequestService(db, users, FakeSettings())


VIEWER = {'username': 'a.viewer', 'role': 'VIEWER', 'full_name': 'A Viewer',
          'email': 'a.viewer@example.com'}
ADMIN = {'username': 'the.admin', 'role': 'ADMIN', 'full_name': 'The Admin'}
OTHER_ADMIN = {'username': 'other.admin', 'role': 'ADMIN', 'full_name': 'Other Admin'}


def open_one(service, groups=('Service Desk',), user=VIEWER):
    return service.open_request(user, 'LEAD', REASON, list(groups), GROUPS)


# ---------------------------------------------------------------------------
# opening
# ---------------------------------------------------------------------------

class TestOpenRequest:
    def test_a_valid_request_is_recorded_as_pending(self, service):
        row = open_one(service)
        assert row['status'] == 'PENDING'
        assert row['from_role'] == 'VIEWER'
        assert row['to_role'] == 'LEAD'
        assert row['requested_groups'] == 'Service Desk'

    def test_expiry_is_set_from_configuration(self, service):
        row = open_one(service)
        assert (row['expires_at'] - row['created_at']).days == 14

    def test_a_second_open_request_is_refused_by_the_unique_index(self, service):
        open_one(service)
        with pytest.raises(AccessRequestError, match='already have a request'):
            open_one(service)

    def test_admin_cannot_be_requested_even_though_the_form_hides_it(self, service):
        """The template omits the option; the POST is still reachable."""
        with pytest.raises(AccessRequestError, match='cannot be requested'):
            service.open_request(VIEWER, 'ADMIN', REASON, ['Service Desk'], GROUPS)

    def test_requesting_a_queue_you_already_hold_is_refused(self, service, users):
        """Asking for the role you have is how you ask for more queues - but
        asking for a queue you already have is just a no-op somebody would
        have to decide."""
        users.accounts['a.viewer'].update(
            {'role': 'LEAD', 'assignment_groups': 'Service Desk'})
        lead = {**VIEWER, 'role': 'LEAD'}
        with pytest.raises(AccessRequestError, match='already has that assignment group'):
            service.open_request(lead, 'LEAD', REASON, ['Service Desk'], GROUPS)

    def test_an_admin_cannot_request_a_narrower_role(self, service):
        with pytest.raises(AccessRequestError, match='broader'):
            service.open_request({**VIEWER, 'role': 'ADMIN'}, 'LEAD',
                                 REASON, ['Service Desk'], GROUPS)

    def test_a_thin_justification_is_refused(self, service):
        with pytest.raises(AccessRequestError, match='at least 20 characters'):
            service.open_request(VIEWER, 'LEAD', 'need it', ['Service Desk'], GROUPS)

    def test_an_unknown_assignment_group_is_refused_at_entry(self, service):
        """A near-miss on a group name errors nowhere downstream - it simply
        matches no tickets - so it has to be caught here."""
        with pytest.raises(AccessRequestError, match='Not a known assignment group'):
            service.open_request(VIEWER, 'LEAD', REASON, ['Servce Desk'], GROUPS)

    def test_group_names_are_normalised_to_the_known_spelling(self, service):
        row = service.open_request(VIEWER, 'LEAD', REASON, ['  service DESK '], GROUPS)
        assert row['requested_groups'] == 'Service Desk'

    def test_no_groups_at_all_is_refused(self, service):
        with pytest.raises(AccessRequestError, match='at least one assignment group'):
            service.open_request(VIEWER, 'LEAD', REASON, [], GROUPS)

    def test_requests_are_refused_when_the_feature_is_off(self, db, users):
        service = RoleRequestService(db, users,
                                     FakeSettings(**{'auth.role_requests.enabled': False}))
        with pytest.raises(AccessRequestError, match='not enabled'):
            open_one(service)


# ---------------------------------------------------------------------------
# approving - the race
# ---------------------------------------------------------------------------

class TestApprove:
    def test_approval_grants_the_role_and_the_scope(self, service, users):
        row = open_one(service)
        result = service.approve(row['id'], ADMIN, ['Service Desk'], 'Agreed', GROUPS)

        assert result['outcome'] == APPLIED
        assert users.accounts['a.viewer']['role'] == 'LEAD'
        assert users.role_changes == [('a.viewer', 'LEAD', ['Service Desk'], 'REQUEST')]

    def test_the_decision_is_recorded_against_the_administrator(self, service):
        row = open_one(service)
        decided = service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)['request']
        assert decided['status'] == 'APPROVED'
        assert decided['decided_by'] == 'the.admin'
        assert decided['decided_at'] is not None

    def test_a_second_administrator_is_told_who_decided_first(self, service, users):
        """Both open the email, both click. The grant must apply once."""
        row = open_one(service)

        first = service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)
        second = service.approve(row['id'], OTHER_ADMIN, GROUPS, '', GROUPS)

        assert first['outcome'] == APPLIED
        assert second['outcome'] == ALREADY_DECIDED
        assert second['request']['decided_by'] == 'the.admin'
        assert len(users.role_changes) == 1

    def test_approve_racing_reject_resolves_to_one_outcome(self, service, users):
        row = open_one(service)

        rejected = service.reject(row['id'], ADMIN, 'Not yet')
        approved = service.approve(row['id'], OTHER_ADMIN, ['Service Desk'], '', GROUPS)

        assert rejected['outcome'] == APPLIED
        assert approved['outcome'] == ALREADY_DECIDED
        # The losing approve must not have written the role.
        assert users.accounts['a.viewer']['role'] == 'VIEWER'
        assert users.role_changes == []

    def test_an_administrator_cannot_decide_their_own_request(self, db, users):
        service = RoleRequestService(db, users, FakeSettings(
            **{'auth.role_requests.allowed_targets': ['LEAD', 'ADMIN']}))
        users.accounts['the.admin']['role'] = 'VIEWER'
        row = service.open_request({**ADMIN, 'role': 'VIEWER'}, 'LEAD',
                                   REASON, ['Service Desk'], GROUPS)

        result = service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)
        assert result['outcome'] == REFUSED
        assert 'your own' in result['reason']

    def test_a_deactivated_requester_cannot_be_granted(self, service, users):
        """Approval is also a mail subscription - leads_for() selects on
        role IN ('ADMIN','LEAD') - so granting a departed account would put
        them back on the digest."""
        row = open_one(service)
        users.accounts['a.viewer']['active'] = 0

        result = service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)
        assert result['outcome'] == REFUSED
        assert 'no longer an active account' in result['reason']
        assert users.role_changes == []

    def test_the_administrator_may_grant_less_than_was_asked_for(self, service, users):
        row = open_one(service, groups=GROUPS)
        service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)
        assert users.accounts['a.viewer']['assignment_groups'] == 'Service Desk'

    def test_granting_an_unknown_group_is_refused(self, service):
        row = open_one(service)
        result = service.approve(row['id'], ADMIN, ['Invented Queue'], '', GROUPS)
        assert result['outcome'] == REFUSED

    def test_granting_nothing_falls_back_to_what_was_requested(self, service, users):
        row = open_one(service)
        service.approve(row['id'], ADMIN, [], '', GROUPS)
        assert users.accounts['a.viewer']['assignment_groups'] == 'Service Desk'

    def test_granted_groups_are_stored_in_a_stable_order(self, service):
        row = open_one(service, groups=GROUPS)
        decided = service.approve(row['id'], ADMIN,
                                  ['Service Desk', 'Desktop Support'], '', GROUPS)['request']
        assert decided['granted_groups'] == 'Desktop Support, Service Desk'

    def test_a_missing_request_reports_not_found(self, service):
        assert service.approve(999, ADMIN, ['Service Desk'], '', GROUPS)['outcome'] == NOT_FOUND


# ---------------------------------------------------------------------------
# rejecting, cancelling, expiring
# ---------------------------------------------------------------------------

class TestReject:
    def test_rejection_records_the_reason_and_changes_no_role(self, service, users):
        row = open_one(service)
        decided = service.reject(row['id'], ADMIN, 'Not needed for this queue')['request']

        assert decided['status'] == 'REJECTED'
        assert decided['decision_note'] == 'Not needed for this queue'
        assert users.accounts['a.viewer']['role'] == 'VIEWER'

    def test_a_second_rejection_is_reported_as_already_decided(self, service):
        row = open_one(service)
        service.reject(row['id'], ADMIN, 'No')
        assert service.reject(row['id'], OTHER_ADMIN, 'Also no')['outcome'] == ALREADY_DECIDED

    def test_re_requesting_inside_the_cooldown_is_refused(self, service):
        row = open_one(service)
        service.reject(row['id'], ADMIN, 'Not yet')
        with pytest.raises(AccessRequestError, match='declined'):
            open_one(service)

    def test_re_requesting_after_the_cooldown_is_allowed(self, service, db):
        row = open_one(service)
        service.reject(row['id'], ADMIN, 'Not yet')
        db.rows[0]['decided_at'] -= datetime.timedelta(days=31)
        assert open_one(service)['status'] == 'PENDING'

    def test_the_cooldown_can_be_disabled(self, db, users):
        service = RoleRequestService(db, users, FakeSettings(
            **{'auth.role_requests.reject_cooldown_days': 0}))
        row = open_one(service)
        service.reject(row['id'], ADMIN, 'Not yet')
        assert open_one(service)['status'] == 'PENDING'


class TestCancelAndExpire:
    def test_the_requester_may_withdraw(self, service, db):
        row = open_one(service)
        assert service.cancel(row['id'], VIEWER) == APPLIED
        assert db.rows[0]['status'] == 'CANCELLED'

    def test_withdrawing_frees_the_unique_key_to_ask_again(self, service):
        row = open_one(service)
        service.cancel(row['id'], VIEWER)
        assert open_one(service)['status'] == 'PENDING'

    def test_one_user_cannot_withdraw_anothers_request(self, service, db):
        row = open_one(service)
        assert service.cancel(row['id'], {'username': 'someone.else'}) == ALREADY_DECIDED
        assert db.rows[0]['status'] == 'PENDING'

    def test_stale_requests_expire(self, service, db):
        row = open_one(service)
        db.rows[0]['expires_at'] = datetime.datetime(2020, 1, 1)
        assert service.expire_stale() == 1
        assert service.get(row['id'])['status'] == 'EXPIRED'

    def test_expiry_leaves_live_requests_alone(self, service):
        open_one(service)
        assert service.expire_stale() == 0

    def test_an_expired_request_can_be_reopened(self, service, db):
        row = open_one(service)
        db.rows[0]['expires_at'] = datetime.datetime(2020, 1, 1)
        service.expire_stale()
        assert open_one(service)['status'] == 'PENDING'

    def test_a_decided_request_is_never_expired(self, service, db):
        row = open_one(service)
        service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)
        db.rows[0]['expires_at'] = datetime.datetime(2020, 1, 1)
        assert service.expire_stale() == 0
        assert service.get(row['id'])['status'] == 'APPROVED'


class TestQueries:
    def test_pending_count_drives_the_administrator_badge(self, service):
        assert service.pending_count() == 0
        open_one(service)
        assert service.pending_count() == 1

    def test_a_decided_request_leaves_the_badge(self, service):
        row = open_one(service)
        service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)
        assert service.pending_count() == 0

    def test_history_is_retained_after_a_decision(self, service):
        """Never deleted: this table is the access audit trail."""
        row = open_one(service)
        service.reject(row['id'], ADMIN, 'No')
        assert len(service.history_for('a.viewer')) == 1


# ---------------------------------------------------------------------------
# session reconciliation
# ---------------------------------------------------------------------------

class FakeRequest:
    """Starlette gives every request a mutable .state; require_user stashes the
    reconciled account there so templates and routes agree."""

    class _State:
        pass

    def __init__(self, cookies=None):
        self.state = self._State()
        self.cookies = cookies or {}
        self.headers = {'accept': 'text/html'}


class TestSessionReconciliation:
    """A role granted mid-session has to apply on the next click.

    The session cookie is self-contained and valid for twelve hours, so a role
    read from it is a snapshot of sign-in time. The decision email tells the
    requester the change is already live; this is what makes that true - and,
    in the direction that matters more, it is what makes a revocation take
    effect without waiting out the cookie.
    """

    @pytest.fixture(autouse=True)
    def wire(self, users):
        from web import auth
        auth.configure(sessions=None, users=users)
        yield
        auth.configure(sessions=None, users=None)

    def test_an_approved_role_applies_without_signing_in_again(self, users):
        from web.auth import _reconcile
        request = FakeRequest()

        stale = {'username': 'a.viewer', 'role': 'VIEWER', 'groups': []}
        users.set_role('a.viewer', 'LEAD', ['Service Desk'], source='REQUEST')

        assert _reconcile(request, stale)['role'] == 'LEAD'

    def test_granted_scope_applies_too(self, users):
        from web.auth import _reconcile
        users.set_role('a.viewer', 'LEAD', ['Service Desk', 'Desktop Support'],
                       source='REQUEST')
        resolved = _reconcile(FakeRequest(), {'username': 'a.viewer', 'role': 'VIEWER'})
        assert resolved['groups'] == ['Service Desk', 'Desktop Support']

    def test_a_deactivated_account_is_signed_out_on_its_next_request(self, users):
        from fastapi import HTTPException
        from web.auth import _reconcile

        users.accounts['a.viewer']['active'] = 0
        with pytest.raises(HTTPException) as caught:
            _reconcile(FakeRequest(), {'username': 'a.viewer', 'role': 'VIEWER'})
        assert caught.value.status_code == 303

    def test_a_deleted_account_is_signed_out(self, users):
        from fastapi import HTTPException
        from web.auth import _reconcile

        del users.accounts['a.viewer']
        with pytest.raises(HTTPException) as caught:
            _reconcile(FakeRequest(), {'username': 'a.viewer', 'role': 'VIEWER'})
        assert caught.value.status_code == 303

    def test_a_database_failure_reads_as_an_outage_not_a_logout(self, users):
        """Bouncing people to the login screen during a database blip sends
        them to retype credentials that were never the problem."""
        from fastapi import HTTPException
        from web.auth import _reconcile

        def boom(username):
            raise RuntimeError('connection lost')
        users.live = boom

        with pytest.raises(HTTPException) as caught:
            _reconcile(FakeRequest(), {'username': 'a.viewer', 'role': 'VIEWER'})
        assert caught.value.status_code == 503

    def test_the_resolved_account_is_cached_for_the_template(self, users):
        from web.auth import _reconcile, current_user

        request = FakeRequest()
        users.set_role('a.viewer', 'LEAD', ['Service Desk'], source='REQUEST')
        _reconcile(request, {'username': 'a.viewer', 'role': 'VIEWER'})

        # Navigation must not show a viewer's menu to someone the routes are
        # already treating as a lead.
        assert current_user(request)['role'] == 'LEAD'


# ---------------------------------------------------------------------------
# asking for more queues at the role you already hold
# ---------------------------------------------------------------------------

def make_lead(users, groups='Service Desk'):
    users.accounts['a.viewer'].update({'role': 'LEAD', 'assignment_groups': groups})
    return {**VIEWER, 'role': 'LEAD'}


class TestAdditionalGroups:
    """A lead already holds the role, so there is nothing to elevate.

    Before this, open_request refused any request where the current role
    equalled the target - which meant a lead whose groups were wrong, missing
    or merely out of date had no way to say so from inside the product. With
    an empty scope now meaning "sees nothing" rather than "sees everything",
    a freshly granted LEAD could be left staring at an empty board with no
    form to fill in.
    """

    def test_a_lead_may_ask_for_a_queue_they_do_not_have(self, service, users):
        lead = make_lead(users)
        row = service.open_request(lead, 'LEAD', REASON, ['Network Operations'], GROUPS)

        assert row['status'] == 'PENDING'
        assert row['from_role'] == 'LEAD' and row['to_role'] == 'LEAD'
        assert row['requested_groups'] == 'Network Operations'

    def test_it_is_marked_as_an_addition_not_an_elevation(self, service, users):
        """What the administrator's page and email branch on."""
        row = service.open_request(make_lead(users), 'LEAD', REASON,
                                   ['Network Operations'], GROUPS)
        assert row['is_additive'] is True

    def test_an_elevation_is_not_marked_as_an_addition(self, service):
        assert open_one(service)['is_additive'] is False

    def test_queues_already_held_are_stripped_from_the_request(self, service, users):
        """The administrator should read "wants Network Operations added", not
        a list in which the one new queue is buried among those they have."""
        lead = make_lead(users, 'Service Desk')
        row = service.open_request(lead, 'LEAD', REASON,
                                   ['Service Desk', 'Network Operations'], GROUPS)
        assert row['requested_groups'] == 'Network Operations'

    def test_asking_only_for_what_you_have_is_refused(self, service, users):
        lead = make_lead(users, 'Service Desk, Network Operations')
        with pytest.raises(AccessRequestError, match='all of those assignment groups'):
            service.open_request(lead, 'LEAD', REASON,
                                 ['Service Desk', 'Network Operations'], GROUPS)

    def test_a_lead_with_no_queues_at_all_may_ask(self, service, users):
        """The state a newly granted LEAD starts in, and the one where the
        board is empty and nothing explains why."""
        lead = make_lead(users, '')
        row = service.open_request(lead, 'LEAD', REASON, ['Service Desk'], GROUPS)
        assert row['requested_groups'] == 'Service Desk'

    def test_a_viewer_may_ask_for_more_queues_without_elevating(self, service, users):
        """Same need, lower role: seeing another queue is not the same ask as
        being able to act on it. allowed_targets does not gate this, because
        it is not an elevation."""
        users.accounts['a.viewer']['assignment_groups'] = 'Service Desk'
        row = service.open_request(VIEWER, 'VIEWER', REASON,
                                   ['Network Operations'], GROUPS)
        assert row['to_role'] == 'VIEWER' and row['is_additive'] is True

    def test_an_administrator_is_told_they_already_see_everything(self, service):
        with pytest.raises(AccessRequestError, match='already see every assignment group'):
            service.open_request({**ADMIN, 'role': 'ADMIN'}, 'ADMIN', REASON,
                                 ['Service Desk'], GROUPS)

    def test_the_justification_minimum_still_applies(self, service, users):
        with pytest.raises(AccessRequestError, match='at least 20 characters'):
            service.open_request(make_lead(users), 'LEAD', 'more pls',
                                 ['Network Operations'], GROUPS)

    def test_an_unknown_queue_is_still_refused(self, service, users):
        with pytest.raises(AccessRequestError, match='Not a known assignment group'):
            service.open_request(make_lead(users), 'LEAD', REASON,
                                 ['Netwrk Operations'], GROUPS)

    def test_still_only_one_open_request_at_a_time(self, service, users):
        lead = make_lead(users)
        service.open_request(lead, 'LEAD', REASON, ['Network Operations'], GROUPS)
        with pytest.raises(AccessRequestError, match='already have a request'):
            service.open_request(lead, 'LEAD', REASON, ['Desktop Support'], GROUPS)


class TestApprovalIsAdditive:
    """The failure this prevents is a removal wearing the word Approve.

    set_role REPLACES assignment_groups. The decision form offers the queues
    being added - so approving "please add Network Operations" with only that
    selected would have left the lead holding Network Operations and nothing
    else, with the audit row reading APPROVED and no warning anywhere.
    """

    def test_approving_an_addition_keeps_what_they_had(self, service, users):
        lead = make_lead(users, 'Service Desk')
        row = service.open_request(lead, 'LEAD', REASON, ['Network Operations'], GROUPS)

        service.approve(row['id'], ADMIN, ['Network Operations'], '', GROUPS)

        assert users.accounts['a.viewer']['assignment_groups'] == (
            'Service Desk, Network Operations')

    def test_the_resulting_scope_is_reported_to_the_caller(self, service, users):
        """The decision email prints it. "Granted: Network Operations" alone
        reads as though it replaced the rest."""
        lead = make_lead(users, 'Service Desk')
        row = service.open_request(lead, 'LEAD', REASON, ['Network Operations'], GROUPS)

        result = service.approve(row['id'], ADMIN, ['Network Operations'], '', GROUPS)
        assert result['scope'] == ['Service Desk', 'Network Operations']

    def test_the_request_records_only_what_this_decision_granted(self, service, users):
        """Two different facts. granted_groups is the decision; the scope is
        the consequence."""
        lead = make_lead(users, 'Service Desk')
        row = service.open_request(lead, 'LEAD', REASON, ['Network Operations'], GROUPS)

        decided = service.approve(row['id'], ADMIN, ['Network Operations'],
                                  '', GROUPS)['request']
        assert decided['granted_groups'] == 'Network Operations'

    def test_an_administrator_granting_less_still_adds_rather_than_replaces(
            self, service, users):
        lead = make_lead(users, 'Service Desk')
        row = service.open_request(lead, 'LEAD', REASON,
                                   ['Network Operations', 'Desktop Support'], GROUPS)

        service.approve(row['id'], ADMIN, ['Desktop Support'], '', GROUPS)

        assert users.accounts['a.viewer']['assignment_groups'] == (
            'Service Desk, Desktop Support')

    def test_a_duplicate_grant_does_not_appear_twice(self, service, users):
        lead = make_lead(users, 'Service Desk')
        row = service.open_request(lead, 'LEAD', REASON, ['Network Operations'], GROUPS)

        service.approve(row['id'], ADMIN, ['Service Desk', 'Network Operations'],
                        '', GROUPS)

        assert users.accounts['a.viewer']['assignment_groups'] == (
            'Service Desk, Network Operations')

    def test_an_elevation_still_replaces_rather_than_merges(self, service, users):
        """A viewer being made a lead has their scope defined from scratch;
        there is nothing to preserve, and carrying over a viewer's queues
        would grant lead rights over queues nobody chose."""
        users.accounts['a.viewer']['assignment_groups'] = 'Desktop Support'
        row = open_one(service)

        service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)

        assert users.accounts['a.viewer']['assignment_groups'] == 'Service Desk'

    def test_a_role_granted_by_hand_between_ask_and_decision_is_respected(
            self, service, users):
        """Read from the live account, not from from_role. Otherwise an
        administrator who granted LEAD manually while the request sat pending
        would have the approval strip the scope they just set."""
        row = open_one(service)
        users.accounts['a.viewer'].update(
            {'role': 'LEAD', 'assignment_groups': 'Desktop Support'})

        service.approve(row['id'], ADMIN, ['Service Desk'], '', GROUPS)

        assert users.accounts['a.viewer']['assignment_groups'] == (
            'Desktop Support, Service Desk')


class TestCooldownIsPerQueue:
    """A decline is about a queue, not about the role.

    Keyed on the role alone, one refusal for Network Operations in March would
    block a lead from asking for Desktop Support in April - for a month, in a
    message naming neither queue.
    """

    def decline(self, service, users, groups):
        lead = make_lead(users, 'Service Desk')
        row = service.open_request(lead, 'LEAD', REASON, list(groups), GROUPS)
        service.reject(row['id'], ADMIN, 'Not yet')
        return lead

    def test_re_asking_for_the_declined_queue_is_refused(self, service, users):
        lead = self.decline(service, users, ['Network Operations'])
        with pytest.raises(AccessRequestError, match='Network Operations'):
            service.open_request(lead, 'LEAD', REASON, ['Network Operations'], GROUPS)

    def test_asking_for_a_different_queue_is_allowed(self, service, users):
        lead = self.decline(service, users, ['Network Operations'])
        row = service.open_request(lead, 'LEAD', REASON, ['Desktop Support'], GROUPS)
        assert row['requested_groups'] == 'Desktop Support'

    def test_a_partial_overlap_is_refused_and_names_the_queue(self, service, users):
        lead = self.decline(service, users, ['Network Operations'])
        with pytest.raises(AccessRequestError, match='for Network Operations was declined'):
            service.open_request(lead, 'LEAD', REASON,
                                 ['Network Operations', 'Desktop Support'], GROUPS)


class TestWhatTheProfileOffers:
    def test_only_queues_not_already_held_are_listed(self, service, users):
        make_lead(users, 'Service Desk')
        assert service.missing_groups('a.viewer', GROUPS) == [
            'Desktop Support', 'Network Operations']

    def test_an_account_with_everything_is_offered_nothing(self, service, users):
        make_lead(users, ', '.join(GROUPS))
        assert service.missing_groups('a.viewer', GROUPS) == []

    def test_an_account_with_nothing_is_offered_everything(self, service, users):
        make_lead(users, '')
        assert service.missing_groups('a.viewer', GROUPS) == GROUPS

    def test_matching_ignores_case_and_padding(self, service, users):
        make_lead(users, '  service desk ')
        assert 'Service Desk' not in service.missing_groups('a.viewer', GROUPS)

    def test_the_scope_reads_back_as_a_list(self, service, users):
        make_lead(users, 'Service Desk, Network Operations')
        assert service.scope_of('a.viewer') == ['Service Desk', 'Network Operations']

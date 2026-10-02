"""Session authentication for the review UI.

Signed-cookie sessions rather than server-side state, so the service stays
stateless and can be restarted mid-shift without logging everyone out.

Passwords use the bcrypt library directly rather than passlib. passlib 1.7.4
(unmaintained since 2020) probes its bcrypt backend at import time by hashing
a deliberately over-length test value; bcrypt >= 4.1 raises instead of
silently truncating, so every hash call fails with a misleading "password
cannot be longer than 72 bytes" regardless of the actual password. Calling
bcrypt directly avoids that entirely, and the stored format is unchanged -
passlib emitted standard $2b$ hashes, so credentials created under it still
verify.

Roles:
  ADMIN  - everything, including weight tuning and user management
  LEAD   - review, give feedback, snooze, trigger re-analysis
  VIEWER - read only
"""

import datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
import bcrypt

from core.logging_setup import get_logger
from core.timeutil import utc_now

log = get_logger('web.auth')

WRITE_ROLES = {'ADMIN', 'LEAD'}
ADMIN_ROLES = {'ADMIN'}

# The same set as WRITE_ROLES, answering a different question: not "may this
# person change something" but "may this person see the whole picture". The
# digest preview and the accuracy metrics are read-only, but both expose every
# group at once, so neither is appropriate for a scoped viewer.
LEAD_ROLES = {'ADMIN', 'LEAD'}

# bcrypt only consumes the first 72 bytes of input; anything beyond that is
# ignored, so two different long passwords would collide. Reject rather than
# truncate. Note this is BYTES, not characters - accented and non-Latin
# characters cost more than one each.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_CHARS = 8


class PasswordError(ValueError):
    """Raised for a password the policy will not accept."""


def validate_password(raw: str) -> bytes:
    """Check a candidate password and return it encoded, ready to hash."""
    if not raw:
        raise PasswordError('Password is required')

    encoded = raw.encode('utf-8')

    if len(raw) < MIN_PASSWORD_CHARS:
        raise PasswordError(
            f'Password must be at least {MIN_PASSWORD_CHARS} characters '
            f'(got {len(raw)})')

    if len(encoded) > MAX_PASSWORD_BYTES:
        extra = ''
        if len(encoded) != len(raw):
            extra = (f' - note this password is {len(raw)} characters but '
                     f'{len(encoded)} bytes, because it contains non-ASCII characters')
        raise PasswordError(
            f'Password is {len(encoded)} bytes; bcrypt accepts at most '
            f'{MAX_PASSWORD_BYTES}. Use a shorter passphrase{extra}.')

    return encoded


def hash_password(raw: str) -> str:
    """Hash a password. Raises PasswordError if it fails policy."""
    return bcrypt.hashpw(validate_password(raw), bcrypt.gensalt()).decode('ascii')


def verify_password(raw: str, hashed: str) -> bool:
    if not raw or not hashed:
        return False
    try:
        # Truncated defensively: an over-length input at the login form must
        # fail to match, never raise and turn into a 500.
        return bcrypt.checkpw(raw.encode('utf-8')[:MAX_PASSWORD_BYTES],
                              hashed.encode('utf-8'))
    except (ValueError, TypeError):
        return False


# --- Redirect safety ------------------------------------------------------

def safe_next(value: Optional[str], fallback: str = '/board') -> str:
    """Reduce a caller-supplied `next` to a path on this site, or the fallback.

    `next` arrives from a query string or a form field and is handed straight
    to a redirect, so without this an attacker can send a user to
    /login?next=https://look-alike.example, have them authenticate here, and
    then land them on a page of their choosing - the browser shows a genuine
    login on the genuine host first, which is exactly what makes it work.

    The three rejected forms are each absolute to a browser despite looking
    relative:

        https://evil.example   obviously absolute
        //evil.example         protocol-relative; inherits the current scheme
        /\\evil.example        parsed as // by browsers that normalise
                               backslashes, which is most of them

    Fragments and query strings are preserved; the check is on the start of
    the string, so /board?group=X and /ticket/INC1#timeline both survive.
    """
    value = (value or '').strip()
    if not value.startswith('/'):
        return fallback
    if value.startswith('//') or value.startswith('/\\'):
        return fallback
    if '\\' in value or '\n' in value or '\r' in value:
        return fallback
    return value


class SessionManager:
    def __init__(self, settings, secret: str):
        self.cookie = settings.get('web.session_cookie', 'ata_session')
        self.max_age = int(settings.get('web.session_max_age_seconds', 43200))
        self.serializer = URLSafeTimedSerializer(secret, salt='ata-session')
        self.secure = str(settings.get('web.base_url', '')).startswith('https')

    def issue(self, response, user: Dict[str, Any],
              expires_at: Optional[datetime.datetime] = None) -> None:
        """Issue the session cookie.

        expires_at is the identity provider's own expiry. Honouring it matters
        because the local cookie lasts twelve hours by default while a Keycloak
        SSO session is typically much shorter - letting the cookie outlive the
        session it was derived from would be claiming the sign-in is fresher
        than it is.
        """
        max_age = self.max_age
        if expires_at is not None:
            idp_seconds = int((expires_at - utc_now()).total_seconds())
            # Never longer than the provider allows, and never zero - a
            # non-positive value would set a cookie already expired, which
            # presents as an immediate redirect loop back to the login page.
            max_age = max(60, min(max_age, idp_seconds))

        token = self.serializer.dumps({
            'username': user['username'],
            'external_id': user.get('external_id'),
            'auth_source': user.get('auth_source', 'LOCAL'),
            'role': user['role'],
            'full_name': user.get('full_name') or user['username'],
            'groups': [g.strip() for g in (user.get('assignment_groups') or '').split(',') if g.strip()],
            'exp': int((utc_now() + datetime.timedelta(seconds=max_age)).timestamp()),
        })
        response.set_cookie(
            self.cookie, token,
            max_age=max_age, httponly=True, samesite='lax', secure=self.secure,
        )

    def read(self, request: Request) -> Optional[Dict[str, Any]]:
        token = request.cookies.get(self.cookie)
        if not token:
            return None
        try:
            payload = self.serializer.loads(token, max_age=self.max_age)
        except SignatureExpired:
            return None
        except BadSignature:
            log.warning('Rejected a session cookie with a bad signature')
            return None

        # The browser is asked to drop the cookie at max_age, and itsdangerous
        # enforces the configured ceiling - but neither honours a shorter
        # provider expiry, and a cookie can be replayed by anything that holds
        # a copy. The embedded exp is the one that cannot be ignored.
        expires = payload.get('exp')
        if expires and utc_now().timestamp() > float(expires):
            return None

        return payload

    def clear(self, response) -> None:
        response.delete_cookie(self.cookie)


# --- Dependency factories -------------------------------------------------
# Wired up in app.py once the SessionManager exists.

_sessions: Optional[SessionManager] = None
_users: Optional['UserStore'] = None


def configure(sessions: SessionManager, users: Optional['UserStore'] = None) -> None:
    global _sessions, _users
    _sessions = sessions
    _users = users


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    """The caller, as resolved for this request.

    Prefers the copy require_user has already reconciled against the database
    and stashed on request.state. Templates render from this, so without that
    preference the navigation would show the role the cookie was issued with
    while the routes enforced the current one - and a lead who was just
    granted access would see a viewer's menu.
    """
    resolved = getattr(request.state, 'advisor_user', None)
    if resolved is not None:
        return resolved
    if _sessions is None:
        return None
    return _sessions.read(request)


def _reconcile(request: Request, claims: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay the stored account on the cookie's claims.

    The session cookie is self-contained and valid for twelve hours, so role
    and scope taken from it are a snapshot of sign-in time. An approved
    elevation would not apply until the next login, and - the direction that
    matters more - neither would a revocation.

    One indexed lookup per request. At this service's traffic that is not worth
    caching, and caching it would reintroduce the staleness this removes.
    """
    if _users is None:
        return claims

    try:
        row = _users.live(claims['username'])
    except Exception:
        # Every page needs the database anyway, so failing closed costs
        # nothing real - but it must read as an outage, not as a logout.
        # Bouncing people to the login screen during a database blip sends
        # them to retype credentials that were never the problem.
        log.exception('Could not reconcile session for %r', claims.get('username'))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail='Service temporarily unavailable')

    if not row or not row.get('active'):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={'Location': '/login?error=Your+account+is+no+longer+active'})

    user = {
        **claims,
        'role': row['role'],
        'full_name': row.get('full_name') or claims['username'],
        'email': row.get('email'),
        'groups': [g.strip() for g in (row.get('assignment_groups') or '').split(',')
                   if g.strip()],
    }
    request.state.advisor_user = user
    return user


def require_user(request: Request) -> Dict[str, Any]:
    user = current_user(request)
    if not user:
        # Browsers get a redirect; API clients get a 401.
        accepts_html = 'text/html' in (request.headers.get('accept') or '')
        if accepts_html:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={'Location': f'/login?next={request.url.path}'},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail='Authentication required')
    return _reconcile(request, user)


def require_write(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    if user.get('role') not in WRITE_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='This action needs a lead or admin account')
    return user


def require_lead(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    """For read-only views that expose every group at once.

    Distinct from require_write only in intent, not in the role set: these
    endpoints change nothing, but a viewer scoped to one queue must not reach
    them, because they answer across the whole estate.
    """
    if user.get('role') not in LEAD_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='This view needs a lead or admin account')
    return user


def require_admin(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    if user.get('role') not in ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='Administrator only')
    return user


# --- User store -----------------------------------------------------------

class UserStore:
    def __init__(self, db):
        self.db = db

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            'SELECT * FROM advisor_user WHERE username = %s AND active = 1',
            (username.strip(),))
        if not row:
            return None

        # An SSO account carries no password hash. verify_password already
        # returns False for an empty hash, but refusing explicitly states the
        # rule rather than relying on a downstream helper to keep it - this is
        # the check that stops a password form being an alternative route into
        # an account that is supposed to require Keycloak.
        if not row.get('password_hash'):
            log.info('Password login refused for %r: account has no local password',
                     username[:40])
            return None

        if not verify_password(password, row['password_hash']):
            return None

        self.db.update('advisor_user', {'last_login_at': utc_now()},
                       conditions=[{'col': 'id', 'op': 'eq', 'val': row['id']}])
        return row

    def live(self, username: str) -> Optional[Dict[str, Any]]:
        """The account as it stands now, regardless of what the cookie says.

        The session cookie is self-contained and valid for twelve hours, so a
        role granted - or revoked - mid-session would otherwise not take effect
        until the user signed in again. Reading the row per request is what
        makes an approval apply on the next click, and a deactivation too.
        """
        return self.db.query_one(
            'SELECT * FROM advisor_user WHERE username = %s', (username.strip(),))

    def active_admins(self) -> List[Dict[str, Any]]:
        return self.db.retrieve('advisor_user', conditions=[
            {'col': 'active', 'op': 'eq', 'val': 1},
            {'col': 'role', 'op': 'eq', 'val': 'ADMIN'},
        ], order_by='username')

    def set_role(self, username: str, role: str,
                 assignment_groups: Optional[List[str]] = None,
                 source: str = 'MANUAL') -> None:
        """Change an account's role and scope.

        Refuses to remove the last administrator. Nothing else in the service
        can restore one through the UI, so a mistaken demotion - or a reconcile
        run against a misconfigured realm - would leave the system with no
        administrator and no way back in short of the CLI.
        """
        role = (role or '').strip().upper()
        current = self.live(username)
        if not current:
            raise ValueError(f'No account named {username!r}')

        if current['role'] == 'ADMIN' and role != 'ADMIN':
            remaining = [a for a in self.active_admins()
                         if a['username'] != current['username']]
            if not remaining:
                raise ValueError(
                    f'{username} is the only active administrator. Promote another '
                    f'account before changing this one.')

        self.db.update('advisor_user', {
            'role': role,
            'assignment_groups': ', '.join(assignment_groups) if assignment_groups else None,
            'role_source': source,
            'role_changed_at': utc_now(),
        }, conditions=[{'col': 'id', 'op': 'eq', 'val': current['id']}])

        log.info('Role for %s: %s -> %s (source=%s)',
                 username, current['role'], role, source)

    def jit_upsert(self, identity: Dict[str, Any], default_role: str = 'VIEWER',
                   default_groups: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create or refresh an account from verified token claims.

        Keyed on external_id, never on username. A username can change - a name
        change, or the unresolved kohler.com versus kohlerco.com question - and
        a upsert keyed on one would create a second account, orphaning that
        person's recommendation_feedback history and leaving them to wonder
        where their access went.

        Role and scope are deliberately not touched on an existing account.
        They are this application's to decide, and an administrator's grant
        must not be undone by the holder simply signing in again.
        """
        external_id = identity['external_id']
        now = utc_now()

        row = self.db.query_one(
            'SELECT * FROM advisor_user WHERE external_id = %s', (external_id,))

        if row is None:
            # Adopt a pre-existing local account with the same username rather
            # than creating a duplicate: this is how a hand-provisioned lead
            # keeps their role, their scope and their history through the
            # cutover instead of being demoted to VIEWER on their first SSO
            # login.
            row = self.db.query_one(
                'SELECT * FROM advisor_user WHERE username = %s AND external_id IS NULL',
                (identity['username'],))
            if row is not None:
                log.info('Linking existing account %r to external identity %s',
                         identity['username'], external_id)

        if row is not None:
            self.db.update('advisor_user', {
                'external_id': external_id,
                'full_name': identity.get('full_name') or row.get('full_name'),
                'email': identity.get('email') or row.get('email'),
                'auth_source': 'OIDC',
                'last_login_at': now,
                'last_seen_idp_at': now,
            }, conditions=[{'col': 'id', 'op': 'eq', 'val': row['id']}])
            return self.live(row['username'])

        self.db.insert('advisor_user', {
            'username': identity['username'],
            'external_id': external_id,
            'full_name': identity.get('full_name') or identity['username'],
            'email': identity.get('email'),
            'password_hash': None,
            'role': default_role,
            'auth_source': 'OIDC',
            'role_source': 'MANUAL',
            'assignment_groups': ', '.join(default_groups) if default_groups else None,
            'active': 1,
            'last_login_at': now,
            'last_seen_idp_at': now,
            'created_at': now,
        })
        log.info('Provisioned %r as %s from single sign-on', identity['username'], default_role)
        return self.live(identity['username'])

    def create(self, username: str, password: str, full_name: str = '',
               email: str = '', role: str = 'LEAD',
               assignment_groups: str = '') -> int:
        return self.db.insert('advisor_user', {
            'username': username.strip(),
            'full_name': full_name.strip() or username.strip(),
            'email': email.strip() or None,
            'password_hash': hash_password(password),
            'role': role,
            'assignment_groups': assignment_groups or None,
            'active': 1,
            'created_at': utc_now(),
        })

    # -- scope ------------------------------------------------------------
    #
    # Two questions, one rule. visible_groups() builds a query filter;
    # can_see_group() answers about a single record. Routes that list use the
    # first, routes that fetch by number use the second - and every route must
    # use one of them, because a scope enforced on the board and not on
    # /ticket/{number} is not a scope, it is a sort order.

    @staticmethod
    def _unrestricted(user: Dict[str, Any]) -> bool:
        """Does an empty scope mean "everything" for this user?

        Yes for LEAD and ADMIN - that is the long-standing behaviour and every
        account provisioned by hand relies on it. No for VIEWER, because a
        VIEWER account is created automatically on first SSO login and an
        empty scope there means "not provisioned yet", not "trusted with the
        whole estate". Defaulting the auto-created role to see everything is
        the failure this inversion exists to prevent.
        """
        return user.get('role') != 'VIEWER'

    def visible_groups(self, user: Dict[str, Any],
                       all_groups: List[str]) -> List[str]:
        scoped = {g.strip().lower() for g in (user.get('groups') or []) if g.strip()}
        if not scoped:
            return list(all_groups) if self._unrestricted(user) else []
        # Intersect rather than return the raw scope: a group that has been
        # renamed in ServiceNow should drop out of the filter rather than be
        # sent to MySQL as a value that matches nothing.
        return [g for g in all_groups if g.strip().lower() in scoped]

    def can_see_group(self, user: Dict[str, Any], group: Optional[str]) -> bool:
        """Whether one record's assignment group is within the user's scope.

        A ticket with no assignment group at all is visible to unrestricted
        users and to nobody else - it belongs to no queue, so it cannot be in
        a scoped user's queue.
        """
        scoped = {g.strip().lower() for g in (user.get('groups') or []) if g.strip()}
        if not scoped:
            return self._unrestricted(user)
        return (group or '').strip().lower() in scoped

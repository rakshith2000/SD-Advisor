"""Session authentication for the review UI.

Signed-cookie sessions rather than server-side state, so the service stays
stateless and can be restarted mid-shift without logging everyone out.
Passwords are bcrypt via passlib.

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
from passlib.context import CryptContext

from core.logging_setup import get_logger

log = get_logger('web.auth')

pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')

WRITE_ROLES = {'ADMIN', 'LEAD'}
ADMIN_ROLES = {'ADMIN'}


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(raw, hashed)
    except Exception:
        return False


class SessionManager:
    def __init__(self, settings, secret: str):
        self.cookie = settings.get('web.session_cookie', 'ata_session')
        self.max_age = int(settings.get('web.session_max_age_seconds', 43200))
        self.serializer = URLSafeTimedSerializer(secret, salt='ata-session')
        self.secure = str(settings.get('web.base_url', '')).startswith('https')

    def issue(self, response, user: Dict[str, Any]) -> None:
        token = self.serializer.dumps({
            'username': user['username'],
            'role': user['role'],
            'full_name': user.get('full_name') or user['username'],
            'groups': [g.strip() for g in (user.get('assignment_groups') or '').split(',') if g.strip()],
        })
        response.set_cookie(
            self.cookie, token,
            max_age=self.max_age, httponly=True, samesite='lax', secure=self.secure,
        )

    def read(self, request: Request) -> Optional[Dict[str, Any]]:
        token = request.cookies.get(self.cookie)
        if not token:
            return None
        try:
            return self.serializer.loads(token, max_age=self.max_age)
        except SignatureExpired:
            return None
        except BadSignature:
            log.warning('Rejected a session cookie with a bad signature')
            return None

    def clear(self, response) -> None:
        response.delete_cookie(self.cookie)


# --- Dependency factories -------------------------------------------------
# Wired up in app.py once the SessionManager exists.

_sessions: Optional[SessionManager] = None


def configure(sessions: SessionManager) -> None:
    global _sessions
    _sessions = sessions


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    if _sessions is None:
        return None
    return _sessions.read(request)


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
    return user


def require_write(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    if user.get('role') not in WRITE_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='This action needs a lead or admin account')
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
        if not row or not verify_password(password, row['password_hash']):
            return None

        self.db.update('advisor_user', {'last_login_at': datetime.datetime.now()},
                       conditions=[{'col': 'id', 'op': 'eq', 'val': row['id']}])
        return row

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
            'created_at': datetime.datetime.now(),
        })

    def visible_groups(self, user: Dict[str, Any],
                       all_groups: List[str]) -> List[str]:
        """A user with no group restriction sees everything."""
        scoped = user.get('groups') or []
        return scoped if scoped else all_groups

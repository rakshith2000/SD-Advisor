"""HashiCorp Vault access.

Supports two authentication methods:

  token    A static token read from config/.vault_token. Simple, but the token
           expires and something has to renew it out of band.

  approle  role_id + secret_id are exchanged for a short-lived token at
           startup, and a background thread renews it. When the token reaches
           its max TTL, or is revoked, the client silently logs in again. This
           is the recommended method for a long-running service.

Differences from the existing audit tool's vault.py, on purpose:
  * TLS verification is ON by default (a CA bundle can be supplied).
  * Secrets are cached in-process, so we don't hit Vault once per ticket - and
    a Vault outage mid-run degrades rather than stopping work.
  * retrieve_secret raises instead of returning a (bool, payload) tuple - a
    missing credential should stop the run loudly, not silently become None
    and surface later as an auth failure.
"""

import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import hvac

from core.config import Settings
from core.logging_setup import get_logger

log = get_logger('core.vault')

# Renew when this fraction of the lease has elapsed.
DEFAULT_RENEW_RATIO = 0.5
MIN_RENEW_SECONDS = 60
# If Vault is unreachable, retry the renewal this often rather than giving up.
RENEW_RETRY_SECONDS = 60


class VaultError(RuntimeError):
    pass


def _read_secret_file(path: Path, label: str) -> str:
    if not path.exists():
        raise VaultError(f'{label} file not found: {path}')
    value = path.read_text(encoding='utf-8').strip()
    if not value:
        raise VaultError(f'{label} file is empty: {path}')

    # Warn rather than fail - an over-permissive file is worth flagging but is
    # not a reason to refuse to start a service that is otherwise healthy.
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            log.warning('%s at %s is mode %o; should be 600', label, path, mode)
    except OSError:
        pass

    return value


class VaultClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._cache: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()

        self.url = settings.require('vault.url')
        self.auth_method = str(settings.get('vault.auth_method', 'token')).strip().lower()

        verify: Any = settings.get('vault.verify_tls', True)
        ca_bundle = settings.get('vault.ca_bundle')
        if verify and ca_bundle and Path(ca_bundle).exists():
            verify = ca_bundle

        self._client = hvac.Client(url=self.url, verify=verify)

        # Renewal state
        self._stop = threading.Event()
        self._renewer: Optional[threading.Thread] = None
        self._lease_duration = 0
        self._renewable = False
        self._last_auth_at = 0.0
        self._renew_count = 0
        self._relogin_count = 0

        if self.auth_method == 'approle':
            self._init_approle()
        elif self.auth_method == 'token':
            self._init_static_token()
        else:
            raise VaultError(
                f"Unknown vault.auth_method {self.auth_method!r}; expected 'token' or 'approle'")

        if not self._client.is_authenticated():
            raise VaultError(f'Vault authentication failed against {self.url}')

        log.info('Vault session established (%s, auth=%s)', self.url, self.auth_method)

    # -- initialisation ----------------------------------------------------

    def _init_static_token(self) -> None:
        token_path = self._settings.resolve_path('vault.token_file', 'config/.vault_token')
        self._client.token = _read_secret_file(token_path, 'Vault token')

        # Report the remaining lifetime so an expiring static token is visible
        # before it becomes an outage.
        try:
            data = self._client.auth.token.lookup_self()['data']
            ttl = int(data.get('ttl') or 0)
            if ttl and ttl < 7 * 86400:
                log.warning('Static Vault token expires in %.1f days - consider AppRole '
                            '(vault.auth_method = approle)', ttl / 86400.0)
        except Exception:
            log.debug('Could not look up static token TTL')

    def _init_approle(self) -> None:
        self.role_id_path = self._settings.resolve_path(
            'vault.approle.role_id_file', 'config/.vault_role_id')
        self.secret_id_path = self._settings.resolve_path(
            'vault.approle.secret_id_file', 'config/.vault_secret_id')

        self.write_token_file = bool(
            self._settings.get('vault.approle.write_token_file', False))
        self.token_file_path = self._settings.resolve_path(
            'vault.token_file', 'config/.vault_token')

        self.renew_ratio = float(
            self._settings.get('vault.approle.renew_ratio', DEFAULT_RENEW_RATIO))
        self.min_renew_seconds = int(
            self._settings.get('vault.approle.min_renew_seconds', MIN_RENEW_SECONDS))

        self._login()
        self._start_renewer()

    # -- AppRole login and renewal ----------------------------------------

    def _login(self) -> None:
        """Exchange role_id + secret_id for a token."""
        role_id = _read_secret_file(self.role_id_path, 'Vault role_id')
        secret_id = _read_secret_file(self.secret_id_path, 'Vault secret_id')

        try:
            response = self._client.auth.approle.login(
                role_id=role_id, secret_id=secret_id, use_token=True)
        except Exception as exc:
            raise VaultError(f'AppRole login failed against {self.url}: {exc}') from exc

        auth = response.get('auth') or {}
        token = auth.get('client_token')
        if not token:
            raise VaultError('AppRole login returned no client_token')

        self._client.token = token
        self._lease_duration = int(auth.get('lease_duration') or 0)
        self._renewable = bool(auth.get('renewable'))
        self._last_auth_at = time.time()
        self._relogin_count += 1

        log.info('AppRole login succeeded (ttl=%ss, renewable=%s, policies=%s)',
                 self._lease_duration, self._renewable,
                 ','.join(auth.get('token_policies') or []) or 'unknown')

        self._publish_token_file(token)

    def _publish_token_file(self, token: str) -> None:
        """Optionally mirror the current token to .vault_token.

        The application never reads this file in approle mode - it exists only
        so existing operational tooling that expects a token on disk keeps
        working. Written atomically at mode 600 so a reader never sees a
        partial value.
        """
        if not getattr(self, 'write_token_file', False):
            return

        path = self.token_file_path
        tmp = path.with_suffix(path.suffix + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Create with restrictive permissions from the outset rather than
            # chmod-ing after the secret is already on disk.
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, token.encode('utf-8'))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(str(tmp), str(path))
            log.debug('Mirrored Vault token to %s', path)
        except OSError:
            log.exception('Could not write the token file at %s', path)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _renew(self) -> bool:
        """Renew the current token. Returns False if a re-login is needed."""
        if not self._renewable:
            return False

        try:
            response = self._client.auth.token.renew_self()
        except Exception as exc:
            # Expected at max_ttl, and whenever the token has been revoked.
            log.info('Token renewal declined (%s) - logging in again', str(exc)[:160])
            return False

        auth = response.get('auth') or {}
        self._lease_duration = int(auth.get('lease_duration') or 0)
        self._renewable = bool(auth.get('renewable'))
        self._renew_count += 1

        if self._lease_duration <= self.min_renew_seconds:
            # Vault is handing back ever-shorter leases, which means max_ttl is
            # close. Re-login now rather than thrashing.
            log.info('Renewed lease is only %ss - logging in again', self._lease_duration)
            return False

        log.debug('Vault token renewed (ttl=%ss)', self._lease_duration)
        self._publish_token_file(self._client.token)
        return True

    def _sleep_seconds(self) -> int:
        if self._lease_duration <= 0:
            return RENEW_RETRY_SECONDS
        return max(self.min_renew_seconds, int(self._lease_duration * self.renew_ratio))

    def _renewal_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self._sleep_seconds()):
                return
            try:
                if not self._renew():
                    self._login()
            except Exception:
                # A Vault outage must not kill the service. Cached secrets keep
                # working; retry shortly.
                log.exception('Vault token refresh failed - retrying in %ss',
                              RENEW_RETRY_SECONDS)
                self._lease_duration = RENEW_RETRY_SECONDS * 2

    def _start_renewer(self) -> None:
        if self._renewer is not None:
            return
        self._renewer = threading.Thread(
            target=self._renewal_loop, name='vault-token-renewer', daemon=True)
        self._renewer.start()
        log.info('Vault token renewer started (first refresh in ~%ss)', self._sleep_seconds())

    def close(self) -> None:
        self._stop.set()
        if self._renewer is not None and self._renewer.is_alive():
            self._renewer.join(timeout=5)

    # -- secrets -----------------------------------------------------------

    def secret(self, path: str) -> Dict[str, str]:
        """Return the full key/value map stored at a KV v2 path."""
        with self._lock:
            if path in self._cache:
                return self._cache[path]

        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                path=path, raise_on_deleted_version=False)
            data = response['data']['data']
        except Exception as exc:  # hvac raises a wide variety of types
            raise VaultError(f"Unable to read Vault secret '{path}': {exc}") from exc

        with self._lock:
            self._cache[path] = data
        return data

    def credential_pair(self, path: str) -> Tuple[str, str]:
        """Return (username, password) for secrets stored as a single kv pair.

        The existing estate stores ServiceNow and MySQL credentials as one
        {username: password} entry, so this keeps us compatible with how the
        secrets are already written.
        """
        data = self.secret(path)
        if not data:
            raise VaultError(f"Vault secret '{path}' is empty")

        if 'username' in data and 'password' in data:
            return str(data['username']), str(data['password'])

        if len(data) != 1:
            raise VaultError(
                f"Vault secret '{path}' has {len(data)} keys; expected a single "
                f"username:password pair or explicit username/password keys")

        username, password = next(iter(data.items()))
        return str(username), str(password)

    def value(self, path: str, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.secret(path).get(key, default)

    def invalidate(self, path: Optional[str] = None) -> None:
        with self._lock:
            if path is None:
                self._cache.clear()
            else:
                self._cache.pop(path, None)

    # -- introspection (used by run.py doctor) -----------------------------

    def auth_status(self) -> Dict[str, Any]:
        status: Dict[str, Any] = {
            'method': self.auth_method,
            'url': self.url,
            'renewals': self._renew_count,
            'logins': self._relogin_count,
            'renewer_running': bool(self._renewer and self._renewer.is_alive()),
        }
        try:
            data = self._client.auth.token.lookup_self()['data']
            status['ttl_seconds'] = int(data.get('ttl') or 0)
            status['renewable'] = bool(data.get('renewable'))
            status['policies'] = data.get('policies') or []
            status['expire_time'] = data.get('expire_time')
        except Exception as exc:
            status['error'] = str(exc)[:200]
        return status

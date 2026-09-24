"""HashiCorp Vault access.

Differences from the existing audit tool's vault.py, on purpose:
  * TLS verification is ON by default (a CA bundle can be supplied).
  * Secrets are cached in-process, so we don't hit Vault once per ticket.
  * retrieve_secret raises instead of returning a (bool, payload) tuple - a
    missing credential should stop the run loudly, not silently become None
    and surface later as an auth failure.
"""

import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import hvac

from core.config import Settings
from core.logging_setup import get_logger

log = get_logger('core.vault')


class VaultError(RuntimeError):
    pass


class VaultClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._cache: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()

        url = settings.require('vault.url')
        token = self._read_token(settings)

        verify: object = settings.get('vault.verify_tls', True)
        ca_bundle = settings.get('vault.ca_bundle')
        if verify and ca_bundle and Path(ca_bundle).exists():
            verify = ca_bundle

        self._client = hvac.Client(url=url, token=token, verify=verify)

        if not self._client.is_authenticated():
            raise VaultError(f'Vault authentication failed against {url}')

        log.info('Vault session established (%s)', url)

    @staticmethod
    def _read_token(settings: Settings) -> str:
        token_path = settings.resolve_path('vault.token_file')
        if not token_path.exists():
            raise VaultError(f'Vault token file not found: {token_path}')
        token = token_path.read_text(encoding='utf-8').strip()
        if not token:
            raise VaultError(f'Vault token file is empty: {token_path}')
        return token

    def secret(self, path: str) -> Dict[str, str]:
        """Return the full key/value map stored at a KV v2 path."""
        with self._lock:
            if path in self._cache:
                return self._cache[path]

        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                path=path, raise_on_deleted_version=False
            )
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
                f"username:password pair or explicit username/password keys"
            )

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

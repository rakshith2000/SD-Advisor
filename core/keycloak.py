"""Read-only Keycloak Admin API client, for offboarding reconciliation.

Deliberately read-only. The service account needs `view-users` on
realm-management and nothing more. On a realm shared with other applications,
`manage-users` would let a credential sitting on this host reset passwords and
rewrite group membership for those applications' users - which is a large
amount of authority to hold in order to answer one question: does this person
still exist and are they still enabled.

The question matters because a leaver disabled in Entra can no longer sign in
here, but their advisor_user row stays active, and leads_for() selects on
role IN ('ADMIN','LEAD'). They would keep receiving the daily digest and every
risk alert - incident descriptions and caller names - indefinitely, with
nothing raised anywhere.
"""

import time
from typing import Any, Dict, List, Optional

import requests

from core.logging_setup import get_logger

log = get_logger('core.keycloak')

# Refresh slightly before expiry rather than on it, so a long reconcile run
# does not fail halfway through on a token that aged out mid-loop.
TOKEN_SKEW_SECONDS = 30


class KeycloakError(RuntimeError):
    pass


class KeycloakAdmin:
    def __init__(self, settings, vault):
        self.settings = settings
        self._vault = vault

        issuer = str(settings.require('auth.oidc.issuer')).rstrip('/')
        # The admin API lives beside the realm, not under it: an issuer of
        # .../realms/AIOT implies .../admin/realms/AIOT.
        self.base, _, self.realm = issuer.rpartition('/realms/')
        if not self.realm:
            raise KeycloakError(
                f'Cannot derive a realm from auth.oidc.issuer {issuer!r}; '
                f'it should end with /realms/<realm>.')
        self.issuer = issuer
        self.admin_base = f'{self.base}/admin/realms/{self.realm}'

        self.client_id = settings.get('auth.reconcile.client_id', 'ata-reconcile')

        verify: Any = bool(settings.get('auth.oidc.verify_tls', True))
        ca_bundle = settings.get('auth.oidc.ca_bundle')
        if verify and ca_bundle:
            verify = str(ca_bundle)
        self.verify = verify
        self.timeout = int(settings.get('auth.oidc.timeout_seconds', 15))

        self._token: Optional[str] = None
        self._token_expires_at = 0.0

    # -- authentication ----------------------------------------------------

    @property
    def _client_secret(self) -> str:
        path = self.settings.get('vault.paths.web', 'sd_advisor_web')
        secret = self._vault.value(path, 'reconcile_client_secret')
        if not secret:
            raise KeycloakError(
                f"No reconcile_client_secret at Vault path '{path}'. The reconcile "
                f"job cannot authenticate without it.")
        return secret

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token

        url = f'{self.issuer}/protocol/openid-connect/token'
        try:
            response = requests.post(
                url,
                data={'grant_type': 'client_credentials',
                      'client_id': self.client_id,
                      'client_secret': self._client_secret},
                timeout=self.timeout, verify=self.verify)
        except requests.exceptions.SSLError as exc:
            raise KeycloakError(
                f'TLS verification failed against {url}. Add the issuing CA to '
                f'auth.oidc.ca_bundle. ({exc})') from exc
        except Exception as exc:
            raise KeycloakError(f'Could not reach {url}: {exc}') from exc

        if response.status_code != 200:
            raise KeycloakError(
                f'Service account login failed ({response.status_code}): '
                f'{response.text[:300]}')

        payload = response.json()
        self._token = payload['access_token']
        self._token_expires_at = (time.time()
                                  + int(payload.get('expires_in', 60)) - TOKEN_SKEW_SECONDS)
        return self._token

    # -- reads -------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f'{self.admin_base}{path}'
        try:
            response = requests.get(
                url, params=params or {},
                headers={'Authorization': f'Bearer {self._access_token()}'},
                timeout=self.timeout, verify=self.verify)
        except Exception as exc:
            raise KeycloakError(f'Keycloak request to {url} failed: {exc}') from exc

        if response.status_code == 403:
            raise KeycloakError(
                f'Keycloak refused {url} (403). The {self.client_id!r} service account '
                f'needs the realm-management role view-users.')
        if response.status_code != 200:
            raise KeycloakError(
                f'Keycloak returned {response.status_code} for {url}: {response.text[:200]}')

        return response.json()

    def find_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Exact username lookup. None when the account no longer exists."""
        rows = self._get('/users', {'username': username, 'exact': 'true', 'max': 2})
        if not rows:
            return None
        # `exact` makes more than one result impossible on a sane realm; if it
        # happens, refusing to guess is safer than picking the first.
        if len(rows) > 1:
            raise KeycloakError(
                f'{len(rows)} users match the username {username!r} exactly')
        return rows[0]

    def find_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        """Lookup by Keycloak user id - stable across a username change."""
        try:
            return self._get(f'/users/{external_id}')
        except KeycloakError as exc:
            if '404' in str(exc):
                return None
            raise

    def is_active(self, account: Dict[str, Any]) -> bool:
        """Whether this account can still sign in.

        enabled is the direct answer. A user federated from Entra who has been
        disabled upstream shows as enabled=false here once Keycloak next syncs
        them, which is the case this exists to catch.
        """
        return bool(account.get('enabled', False))

    def ping(self) -> Dict[str, Any]:
        """Cheap reachability and permission probe, for run.py doctor."""
        rows = self._get('/users', {'max': 1})
        return {'realm': self.realm, 'readable': True, 'sample_size': len(rows)}

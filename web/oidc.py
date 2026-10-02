"""OpenID Connect against Keycloak - Authorization Code with PKCE.

Keycloak authenticates. It does not authorise: ADMIN / LEAD / VIEWER live in
advisor_user.role and Keycloak is never told they exist. Nothing in this module
reads a role from a token.

Built on PyJWT and requests, both already dependencies, rather than adding an
OIDC framework. The flow is three HTTP calls and one signature check, and doing
it directly means the CA bundle is handled the same way as everywhere else in
this codebase - an internal CA will otherwise fail the token exchange exactly
as it did against ServiceNow.

Four things here are security-load-bearing.

The ID token signature is verified against the issuer's JWKS, with iss, aud,
exp and nonce all checked. An unverified token is attacker-supplied JSON.

The userinfo endpoint is never consulted. It is not signed, so it cannot be
evidence of anything.

state and nonce travel in a short-lived signed cookie. This service keeps no
server-side session store, so there is nowhere else to put them, and without
them the callback accepts an authorization code from anybody.

The JWKS is cached but re-fetched when a token arrives with an unfamiliar kid,
because Keycloak rotates its signing keys and a cache that never refreshes
turns a routine rotation into a total outage at an unpredictable hour.
"""

import base64
import datetime
import hashlib
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import jwt
import requests
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jwt import PyJWKSet

from core.logging_setup import get_logger

log = get_logger('web.oidc')

STATE_COOKIE = 'ata_oidc_state'
ID_TOKEN_COOKIE = 'ata_idt'

# The authorization code is exchanged within seconds of being issued. Five
# minutes is slack for a slow network and a clock a little out of step, not an
# allowance for leaving a half-finished login open in a tab.
STATE_MAX_AGE = 300

# Tolerance on exp and iat. Keycloak and this host should both be on NTP;
# this absorbs the drift between two machines that are, not the hour gap of
# one that is not.
CLOCK_SKEW_SECONDS = 30

JWKS_CACHE_SECONDS = 3600
SUPPORTED_ALGORITHMS = ('RS256', 'RS384', 'RS512', 'ES256', 'ES384', 'PS256')


class OIDCError(RuntimeError):
    """Any failure in the flow. The message is for the log, not the browser."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


class OIDCProvider:
    def __init__(self, settings, vault, session_secret: str):
        self.settings = settings
        self.issuer = str(settings.require('auth.oidc.issuer')).rstrip('/')
        self.client_id = settings.require('auth.oidc.client_id')
        self.redirect_uri = settings.require('auth.oidc.redirect_uri')
        self.scopes = list(settings.get('auth.oidc.scopes', ['openid', 'profile', 'email']))

        self.identity_claim = settings.get('auth.oidc.identity_claim', 'oid')
        self.username_claim = settings.get('auth.oidc.username_claim', 'preferred_username')

        verify: Any = bool(settings.get('auth.oidc.verify_tls', True))
        ca_bundle = settings.get('auth.oidc.ca_bundle')
        if verify and ca_bundle:
            verify = str(ca_bundle)
        self.verify = verify
        self.timeout = int(settings.get('auth.oidc.timeout_seconds', 15))

        # Signed rather than stored: there is no server-side session, and a
        # signed cookie is what lets the callback trust that the state it is
        # comparing against came from this service.
        self.serializer = URLSafeTimedSerializer(session_secret, salt='ata-oidc-state')

        self._client_secret: Optional[str] = None
        self._vault = vault
        self._metadata: Optional[Dict[str, Any]] = None
        self._jwks: Optional[PyJWKSet] = None
        self._jwks_fetched_at = 0.0

    # -- configuration -----------------------------------------------------

    @property
    def client_secret(self) -> str:
        if self._client_secret is None:
            path = self.settings.get('vault.paths.web', 'sd_advisor_web')
            secret = self._vault.value(path, 'oidc_client_secret')
            if not secret:
                raise OIDCError(
                    f"No oidc_client_secret at Vault path '{path}'. The token exchange "
                    f"cannot be authenticated without it.")
            self._client_secret = secret
        return self._client_secret

    def metadata(self) -> Dict[str, Any]:
        """The issuer's discovery document, fetched once per process."""
        if self._metadata is None:
            url = f'{self.issuer}/.well-known/openid-configuration'
            try:
                response = requests.get(url, timeout=self.timeout, verify=self.verify)
                response.raise_for_status()
                self._metadata = response.json()
            except requests.exceptions.SSLError as exc:
                raise OIDCError(
                    f'TLS verification failed against {url}. Add the issuing CA to '
                    f'auth.oidc.ca_bundle - do not disable verification, this '
                    f'connection carries the client secret and the authorization '
                    f'code. ({exc})') from exc
            except Exception as exc:
                raise OIDCError(f'OIDC discovery failed at {url}: {exc}') from exc

            # A discovery document whose issuer disagrees with the configured
            # one means every subsequent iss check would fail, with a confusing
            # error much later in the flow.
            declared = str(self._metadata.get('issuer', '')).rstrip('/')
            if declared != self.issuer:
                raise OIDCError(
                    f'Discovery at {url} declares issuer {declared!r}, but '
                    f'auth.oidc.issuer is {self.issuer!r}. These must match exactly.')

        return self._metadata

    def _endpoint(self, name: str) -> str:
        value = self.metadata().get(name)
        if not value:
            raise OIDCError(f'The issuer does not advertise {name}')
        return str(value)

    # -- JWKS --------------------------------------------------------------

    def _load_jwks(self, force: bool = False) -> PyJWKSet:
        fresh = (not force and self._jwks is not None
                 and (time.time() - self._jwks_fetched_at) < JWKS_CACHE_SECONDS)
        if fresh:
            return self._jwks

        url = self._endpoint('jwks_uri')
        try:
            response = requests.get(url, timeout=self.timeout, verify=self.verify)
            response.raise_for_status()
            self._jwks = PyJWKSet.from_dict(response.json())
            self._jwks_fetched_at = time.time()
        except Exception as exc:
            if self._jwks is not None:
                # A stale key set still verifies tokens signed by keys we
                # already hold. Failing the login because the JWKS endpoint
                # blipped would be worse than using a cache a few minutes old.
                log.warning('Could not refresh JWKS from %s (%s) - using the cached set',
                            url, str(exc)[:120])
                return self._jwks
            raise OIDCError(f'Could not fetch JWKS from {url}: {exc}') from exc

        return self._jwks

    def _signing_key(self, token: str):
        try:
            kid = jwt.get_unverified_header(token).get('kid')
        except jwt.PyJWTError as exc:
            raise OIDCError(f'Malformed token header: {exc}') from exc

        for attempt in (False, True):
            jwks = self._load_jwks(force=attempt)
            for key in jwks.keys:
                if key.key_id == kid:
                    return key
            if not attempt:
                # Unknown kid is the normal signature of a key rotation, so
                # re-fetch once before concluding the token is bad.
                log.info('Signing key %r not in the cached JWKS - refreshing', kid)

        raise OIDCError(f'No signing key matching kid={kid!r} is published by the issuer')

    # -- step 1: send the browser to Keycloak ------------------------------

    def authorization_url(self, next_path: str = '/board') -> Tuple[str, str]:
        """Returns (url, signed state cookie value)."""
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode('ascii')).digest())
        state = _b64url(secrets.token_bytes(24))
        nonce = _b64url(secrets.token_bytes(24))

        params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'scope': ' '.join(self.scopes),
            'redirect_uri': self.redirect_uri,
            'state': state,
            'nonce': nonce,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        }
        url = f"{self._endpoint('authorization_endpoint')}?{urlencode(params)}"

        cookie = self.serializer.dumps({
            'state': state, 'nonce': nonce, 'verifier': verifier, 'next': next_path})
        return url, cookie

    # -- step 2: the browser comes back ------------------------------------

    def exchange(self, code: str, state_param: str,
                 state_cookie: Optional[str]) -> Dict[str, Any]:
        """Verify the callback and return {claims, id_token, next}."""
        if not state_cookie:
            raise OIDCError(
                'No state cookie on the callback. Either the login was started '
                'somewhere else, the cookie was blocked, or this is a forged request.')

        try:
            stashed = self.serializer.loads(state_cookie, max_age=STATE_MAX_AGE)
        except SignatureExpired:
            raise OIDCError('The login took too long to complete. Start again.')
        except BadSignature as exc:
            raise OIDCError(f'The state cookie failed its signature check: {exc}') from exc

        # secrets.compare_digest rather than == : state is a secret being
        # compared against attacker-influenced input.
        if not secrets.compare_digest(str(stashed.get('state', '')), str(state_param or '')):
            raise OIDCError('State mismatch on the callback - refusing the authorization code.')

        tokens = self._redeem(code, stashed['verifier'])
        id_token = tokens.get('id_token')
        if not id_token:
            raise OIDCError('The token response contained no id_token')

        claims = self._verify(id_token, expected_nonce=stashed.get('nonce'))
        return {'claims': claims, 'id_token': id_token, 'next': stashed.get('next', '/board')}

    def _redeem(self, code: str, verifier: str) -> Dict[str, Any]:
        try:
            response = requests.post(
                self._endpoint('token_endpoint'),
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': self.redirect_uri,
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'code_verifier': verifier,
                },
                headers={'Accept': 'application/json'},
                timeout=self.timeout, verify=self.verify)
        except requests.exceptions.SSLError as exc:
            raise OIDCError(
                f'TLS verification failed on the token exchange. Add the issuing CA to '
                f'auth.oidc.ca_bundle. ({exc})') from exc
        except Exception as exc:
            raise OIDCError(f'Token exchange failed: {exc}') from exc

        if response.status_code != 200:
            # Keycloak's error body is the only useful diagnostic here, and it
            # names the cause - invalid_grant for a reused code, invalid_client
            # for a wrong secret, and so on.
            raise OIDCError(
                f'Token endpoint returned {response.status_code}: {response.text[:300]}')

        return response.json()

    def _verify(self, id_token: str, expected_nonce: Optional[str]) -> Dict[str, Any]:
        key = self._signing_key(id_token)
        algorithm = getattr(key, 'algorithm_name', None) or 'RS256'
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise OIDCError(f'Refusing a token signed with {algorithm}')

        try:
            claims = jwt.decode(
                id_token, key=key.key, algorithms=[algorithm],
                audience=self.client_id, issuer=self.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={'require': ['exp', 'iat', 'iss', 'aud', 'sub'],
                         'verify_signature': True, 'verify_exp': True,
                         'verify_iat': True, 'verify_aud': True, 'verify_iss': True},
            )
        except jwt.ExpiredSignatureError as exc:
            raise OIDCError(f'The ID token has expired: {exc}') from exc
        except jwt.InvalidAudienceError as exc:
            raise OIDCError(
                f'The ID token is not addressed to {self.client_id!r}: {exc}') from exc
        except jwt.InvalidIssuerError as exc:
            raise OIDCError(f'The ID token was issued by someone else: {exc}') from exc
        except jwt.PyJWTError as exc:
            raise OIDCError(f'The ID token failed verification: {exc}') from exc

        # Checked here rather than by PyJWT, which has no notion of it. Without
        # this an ID token captured from another login can be replayed into
        # this one.
        if expected_nonce:
            if not secrets.compare_digest(str(claims.get('nonce', '')), str(expected_nonce)):
                raise OIDCError('Nonce mismatch - possible replay of an earlier ID token')

        return claims

    # -- what the application needs out of a verified token ----------------

    def identity(self, claims: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten verified claims into the fields advisor_user stores.

        external_id falls back to `sub` when the configured claim is absent.
        `sub` is always present and stable per issuer, so the account is still
        keyed on something durable rather than on the username - which is the
        value that changes.
        """
        external_id = str(claims.get(self.identity_claim) or claims.get('sub') or '').strip()
        username = str(
            claims.get(self.username_claim)
            or claims.get('preferred_username')
            or claims.get('email')
            or external_id).strip()

        if not external_id or not username:
            raise OIDCError(
                f'The ID token carries neither {self.identity_claim!r} nor enough to '
                f'identify the user. Check the protocol mappers on this client.')

        email = str(claims.get('email') or '').strip()
        if not email:
            # Not fatal - they can still sign in and read the board - but they
            # will never receive a digest, and that failure is otherwise silent.
            log.warning('No email claim for %s; this account cannot receive the '
                        'digest or risk alerts until one is set', username)

        full_name = str(
            claims.get('name')
            or ' '.join(p for p in (claims.get('given_name'), claims.get('family_name')) if p)
            or username).strip()

        return {
            'external_id': external_id,
            'username': username,
            'email': email or None,
            'full_name': full_name,
            # Built from an aware value: utcfromtimestamp is deprecated from
            # Python 3.12 and the project stores naive UTC throughout.
            'expires_at': (datetime.datetime
                           .fromtimestamp(int(claims['exp']), datetime.timezone.utc)
                           .replace(tzinfo=None)),
        }

    # -- step 3: logout ----------------------------------------------------

    def logout_url(self, id_token: Optional[str], post_logout: str) -> str:
        """RP-initiated logout.

        Clearing the local cookie is not a logout: the Keycloak session
        survives, so the sign-in button logs straight back in without a prompt,
        and on a shared machine the next person inherits the session.

        id_token_hint makes it silent. Without it Keycloak shows a confirmation
        screen, which is why the token is kept in a path-scoped cookie.
        """
        try:
            endpoint = self._endpoint('end_session_endpoint')
        except OIDCError:
            log.warning('The issuer advertises no end_session_endpoint - the Keycloak '
                        'session will outlive the local one')
            return post_logout

        params: Dict[str, str] = {'post_logout_redirect_uri': post_logout}
        if id_token:
            params['id_token_hint'] = id_token
        else:
            params['client_id'] = self.client_id
        return f'{endpoint}?{urlencode(params)}'

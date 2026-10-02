"""OpenID Connect against Keycloak.

Every test here is about refusing something. An OIDC client that accepts tokens
it should reject does not fail - it works, for the wrong people, silently, and
nothing in a log or a dashboard distinguishes that from working correctly.

The five refusals that matter, and what each one stops:

  signature    An unverified token is attacker-supplied JSON. Anyone who can
               reach the callback can mint one claiming to be anybody.
  audience     A token minted for the ATK client is signed by the same realm
               with the same key. Without an aud check it verifies here too,
               so any application sharing the realm becomes a way in.
  issuer       A token from a realm this service does not trust.
  expiry       A captured token working forever.
  nonce        An ID token captured from one login replayed into another.

Plus state, which is the only thing tying a callback to a login this service
actually started - there is no server-side session to hold it.

Keys are generated per run rather than fixtured, so the tests exercise real
RS256 verification rather than a stub that could pass while the real path does
not.
"""

import datetime
import json
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.oidc import OIDCError, OIDCProvider

ISSUER = 'https://keycloak.example.com/realms/AIOT'
CLIENT_ID = 'ata-web'
REDIRECT = 'https://advisor.example.com/oidc/callback'
SECRET = 'a-signing-secret-for-the-state-cookie'


# ---------------------------------------------------------------------------
# key material
# ---------------------------------------------------------------------------

def _make_key(kid):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({'kid': kid, 'use': 'sig', 'alg': 'RS256'})
    return private, public_jwk


REAL_KEY, REAL_JWK = _make_key('real-key-1')
ROTATED_KEY, ROTATED_JWK = _make_key('rotated-key-2')
ATTACKER_KEY, _ = _make_key('real-key-1')      # same kid, different key


def issue(key=REAL_KEY, kid='real-key-1', **over):
    now = int(time.time())
    claims = {
        'iss': ISSUER, 'aud': CLIENT_ID, 'sub': 'kc-sub-0001',
        'exp': now + 900, 'iat': now, 'nonce': 'the-nonce',
        'preferred_username': 'jane.doe', 'email': 'jane.doe@example.com',
        'name': 'Jane Doe', 'oid': 'entra-object-id-999',
    }
    claims.update(over)
    for empty in [k for k, v in claims.items() if v is None]:
        del claims[empty]
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': kid})


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeSettings:
    def __init__(self, **over):
        self.values = {
            'auth.oidc.issuer': ISSUER,
            'auth.oidc.client_id': CLIENT_ID,
            'auth.oidc.redirect_uri': REDIRECT,
            'auth.oidc.scopes': ['openid', 'profile', 'email'],
            'auth.oidc.identity_claim': 'oid',
            'auth.oidc.username_claim': 'preferred_username',
            'auth.oidc.verify_tls': True,
        }
        self.values.update(over)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def require(self, key):
        if key not in self.values:
            raise KeyError(key)
        return self.values[key]


class FakeVault:
    def __init__(self, secret='client-secret'):
        self.secret = secret

    def value(self, path, key, default=None):
        return self.secret if key == 'oidc_client_secret' else default


METADATA = {
    'issuer': ISSUER,
    'authorization_endpoint': f'{ISSUER}/protocol/openid-connect/auth',
    'token_endpoint': f'{ISSUER}/protocol/openid-connect/token',
    'jwks_uri': f'{ISSUER}/protocol/openid-connect/certs',
    'end_session_endpoint': f'{ISSUER}/protocol/openid-connect/logout',
}


@pytest.fixture
def provider():
    p = OIDCProvider(FakeSettings(), FakeVault(), SECRET)
    p._metadata = dict(METADATA)
    p._jwks = jwt.PyJWKSet.from_dict({'keys': [REAL_JWK]})
    p._jwks_fetched_at = time.time()
    return p


def start_login(provider, next_path='/board'):
    """Run step one and recover the values the callback will be checked against."""
    url, cookie = provider.authorization_url(next_path)
    stashed = provider.serializer.loads(cookie)
    return url, cookie, stashed


# ---------------------------------------------------------------------------
# step 1
# ---------------------------------------------------------------------------

class TestAuthorizationUrl:
    def test_sends_the_browser_to_the_issuer(self, provider):
        url, _, _ = start_login(provider)
        assert url.startswith(METADATA['authorization_endpoint'])

    def test_requests_a_code_with_pkce(self, provider):
        url, _, _ = start_login(provider)
        assert 'response_type=code' in url
        assert 'code_challenge_method=S256' in url
        assert 'code_challenge=' in url

    def test_the_verifier_never_leaves_this_service(self, provider):
        """Only the challenge goes to Keycloak. A verifier in the URL would
        defeat the whole mechanism."""
        url, _, stashed = start_login(provider)
        assert stashed['verifier'] not in url

    def test_state_and_nonce_are_fresh_every_time(self, provider):
        _, _, first = start_login(provider)
        _, _, second = start_login(provider)
        assert first['state'] != second['state']
        assert first['nonce'] != second['nonce']

    def test_the_destination_rides_in_the_signed_cookie(self, provider):
        _, _, stashed = start_login(provider)
        assert stashed['next'] == '/board'

    def test_the_state_cookie_is_tamper_evident(self, provider):
        _, cookie, _ = start_login(provider)
        from itsdangerous import BadSignature
        with pytest.raises(BadSignature):
            provider.serializer.loads(cookie[:-4] + 'AAAA')


# ---------------------------------------------------------------------------
# ID token verification
# ---------------------------------------------------------------------------

class TestVerify:
    def test_a_good_token_verifies(self, provider):
        claims = provider._verify(issue(), expected_nonce='the-nonce')
        assert claims['preferred_username'] == 'jane.doe'

    def test_a_token_signed_by_the_wrong_key_is_refused(self, provider):
        """The whole point. Same kid, different private key."""
        with pytest.raises(OIDCError, match='failed verification'):
            provider._verify(issue(key=ATTACKER_KEY), expected_nonce='the-nonce')

    def test_an_unsigned_token_is_refused(self, provider):
        """alg=none is the oldest JWT attack there is."""
        forged = jwt.encode({'iss': ISSUER, 'aud': CLIENT_ID, 'sub': 'x',
                             'exp': int(time.time()) + 900, 'iat': int(time.time())},
                            key='', algorithm='none', headers={'kid': 'real-key-1'})
        with pytest.raises(OIDCError):
            provider._verify(forged, expected_nonce=None)

    def test_a_token_for_another_client_in_the_same_realm_is_refused(self, provider):
        """aiot-backend's tokens are signed by this realm with this key. Without
        the audience check, every application sharing the realm is a way in."""
        with pytest.raises(OIDCError, match='not addressed to'):
            provider._verify(issue(aud='aiot-backend'), expected_nonce='the-nonce')

    def test_a_token_from_another_issuer_is_refused(self, provider):
        with pytest.raises(OIDCError, match='issued by someone else'):
            provider._verify(issue(iss='https://keycloak.example.com/realms/OTHER'),
                             expected_nonce='the-nonce')

    def test_an_expired_token_is_refused(self, provider):
        with pytest.raises(OIDCError, match='expired'):
            provider._verify(issue(exp=int(time.time()) - 120),
                             expected_nonce='the-nonce')

    def test_small_clock_skew_is_tolerated(self, provider):
        """Two NTP-synced machines drift by seconds. Refusing that would make
        sign-in fail intermittently for no diagnosable reason."""
        claims = provider._verify(issue(exp=int(time.time()) - 10),
                                  expected_nonce='the-nonce')
        assert claims['sub'] == 'kc-sub-0001'

    def test_a_replayed_token_from_another_login_is_refused(self, provider):
        with pytest.raises(OIDCError, match='Nonce mismatch'):
            provider._verify(issue(nonce='a-different-login'), expected_nonce='the-nonce')

    def test_a_token_with_no_nonce_cannot_satisfy_one(self, provider):
        with pytest.raises(OIDCError, match='Nonce mismatch'):
            provider._verify(issue(nonce=None), expected_nonce='the-nonce')

    def test_a_token_missing_required_claims_is_refused(self, provider):
        with pytest.raises(OIDCError):
            provider._verify(issue(sub=None), expected_nonce='the-nonce')

    def test_an_unknown_kid_triggers_one_jwks_refresh(self, provider):
        """Keycloak rotates signing keys. A cache that never refreshes turns a
        routine rotation into a total outage at an unpredictable hour."""
        refreshes = []

        def refetch(force=False):
            # The cached read must return the STALE set, as the real one does.
            # Handing back the rotated key on the unforced call would let this
            # pass without the refresh path ever running.
            refreshes.append(force)
            return jwt.PyJWKSet.from_dict(
                {'keys': [REAL_JWK, ROTATED_JWK] if force else [REAL_JWK]})

        provider._load_jwks = refetch
        claims = provider._verify(issue(key=ROTATED_KEY, kid='rotated-key-2'),
                                  expected_nonce='the-nonce')
        assert claims['sub'] == 'kc-sub-0001'
        assert refreshes == [False, True]   # cached miss, then a forced refetch

    def test_a_genuinely_unknown_kid_still_fails(self, provider):
        with pytest.raises(OIDCError, match='No signing key'):
            provider._verify(issue(key=ROTATED_KEY, kid='never-published'),
                             expected_nonce='the-nonce')


# ---------------------------------------------------------------------------
# the callback
# ---------------------------------------------------------------------------

class TestExchange:
    def test_a_missing_state_cookie_is_refused(self, provider):
        with pytest.raises(OIDCError, match='No state cookie'):
            provider.exchange('some-code', 'some-state', None)

    def test_a_mismatched_state_is_refused(self, provider):
        _, cookie, _ = start_login(provider)
        with pytest.raises(OIDCError, match='State mismatch'):
            provider.exchange('some-code', 'not-the-state', cookie)

    def test_a_forged_state_cookie_is_refused(self, provider):
        """Minted with a different signing key - i.e. not by this service."""
        from itsdangerous import URLSafeTimedSerializer
        forged = URLSafeTimedSerializer('someone-elses-secret', salt='ata-oidc-state')
        cookie = forged.dumps({'state': 's', 'nonce': 'n', 'verifier': 'v', 'next': '/board'})
        with pytest.raises(OIDCError, match='signature check'):
            provider.exchange('some-code', 's', cookie)

    def test_an_abandoned_login_expires(self, provider):
        from itsdangerous import SignatureExpired

        _, cookie, stashed = start_login(provider)

        def expired(*a, **k):
            raise SignatureExpired('too old')
        provider.serializer.loads = expired

        with pytest.raises(OIDCError, match='took too long'):
            provider.exchange('some-code', stashed['state'], cookie)

    def test_a_successful_exchange_returns_verified_claims(self, provider):
        _, cookie, stashed = start_login(provider, next_path='/ticket/INC1')
        provider._redeem = lambda code, verifier: {
            'id_token': issue(nonce=stashed['nonce'])}

        result = provider.exchange('the-code', stashed['state'], cookie)
        assert result['claims']['preferred_username'] == 'jane.doe'
        assert result['next'] == '/ticket/INC1'

    def test_the_verifier_is_sent_to_the_token_endpoint(self, provider):
        seen = {}
        _, cookie, stashed = start_login(provider)

        def redeem(code, verifier):
            seen.update(code=code, verifier=verifier)
            return {'id_token': issue(nonce=stashed['nonce'])}
        provider._redeem = redeem

        provider.exchange('the-code', stashed['state'], cookie)
        assert seen['code'] == 'the-code'
        assert seen['verifier'] == stashed['verifier']

    def test_a_token_response_without_an_id_token_is_refused(self, provider):
        _, cookie, stashed = start_login(provider)
        provider._redeem = lambda code, verifier: {'access_token': 'only-this'}
        with pytest.raises(OIDCError, match='no id_token'):
            provider.exchange('the-code', stashed['state'], cookie)

    def test_a_token_from_a_different_login_is_refused_at_the_callback(self, provider):
        """State checks out, but the ID token belongs to another session."""
        _, cookie, stashed = start_login(provider)
        provider._redeem = lambda code, verifier: {'id_token': issue(nonce='elsewhere')}
        with pytest.raises(OIDCError, match='Nonce mismatch'):
            provider.exchange('the-code', stashed['state'], cookie)


# ---------------------------------------------------------------------------
# claims -> account fields
# ---------------------------------------------------------------------------

class TestIdentity:
    def test_the_entra_object_id_is_the_identity_key(self, provider):
        identity = provider.identity(jwt.decode(issue(), options={'verify_signature': False}))
        assert identity['external_id'] == 'entra-object-id-999'
        assert identity['username'] == 'jane.doe'

    def test_sub_is_the_fallback_when_the_claim_is_absent(self, provider):
        """A missing mapper must not leave the account keyed on the username,
        which is the value that changes."""
        claims = jwt.decode(issue(oid=None), options={'verify_signature': False})
        assert provider.identity(claims)['external_id'] == 'kc-sub-0001'

    def test_name_is_assembled_when_there_is_no_name_claim(self, provider):
        claims = jwt.decode(issue(name=None, given_name='Jane', family_name='Doe'),
                            options={'verify_signature': False})
        assert provider.identity(claims)['full_name'] == 'Jane Doe'

    def test_a_missing_email_is_surfaced_not_fatal(self, provider, caplog):
        """They can still sign in and read the board - but they will never
        receive a digest, and that failure is otherwise silent."""
        claims = jwt.decode(issue(email=None), options={'verify_signature': False})
        identity = provider.identity(claims)
        assert identity['email'] is None
        assert 'cannot receive the digest' in caplog.text

    def test_expiry_is_carried_through_as_naive_utc(self, provider):
        claims = jwt.decode(issue(), options={'verify_signature': False})
        expires = provider.identity(claims)['expires_at']
        assert isinstance(expires, datetime.datetime)
        assert expires.tzinfo is None

    def test_a_token_identifying_nobody_is_refused(self, provider):
        with pytest.raises(OIDCError, match='protocol mappers'):
            provider.identity({'exp': int(time.time()) + 900})


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------

class TestLogout:
    def test_the_hint_makes_logout_silent(self, provider):
        url = provider.logout_url('the-id-token', 'https://advisor.example.com/login')
        assert url.startswith(METADATA['end_session_endpoint'])
        assert 'id_token_hint=the-id-token' in url

    def test_without_a_hint_it_falls_back_to_the_client_id(self, provider):
        url = provider.logout_url(None, 'https://advisor.example.com/login')
        assert f'client_id={CLIENT_ID}' in url

    def test_the_return_address_is_always_included(self, provider):
        url = provider.logout_url('t', 'https://advisor.example.com/login')
        assert 'post_logout_redirect_uri=' in url

    def test_an_issuer_without_the_endpoint_degrades_to_a_local_logout(self, provider):
        provider._metadata = {k: v for k, v in METADATA.items()
                              if k != 'end_session_endpoint'}
        assert provider.logout_url('t', '/login') == '/login'


class TestDiscovery:
    def test_an_issuer_mismatch_is_caught_at_discovery(self):
        """Otherwise every later iss check fails, with a confusing error much
        further into the flow."""
        provider = OIDCProvider(FakeSettings(), FakeVault(), SECRET)
        provider._metadata = None

        import web.oidc as module

        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {**METADATA, 'issuer': 'https://elsewhere/realms/X'}

        original = module.requests.get
        module.requests.get = lambda *a, **k: Response()
        try:
            with pytest.raises(OIDCError, match='declares issuer'):
                provider.metadata()
        finally:
            module.requests.get = original

    def test_a_missing_client_secret_is_a_clear_error(self):
        provider = OIDCProvider(FakeSettings(), FakeVault(secret=None), SECRET)
        with pytest.raises(OIDCError, match='oidc_client_secret'):
            provider.client_secret

import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from auth.mcp_identity import EntraVerifier, Requester

TENANT = '12345678-1234-1234-1234-123456789abc'
OTHER_TENANT = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
USER = '87654321-4321-4321-4321-cba987654321'
AUDIENCE = 'api://team-context'
ISSUER = f'https://login.microsoftonline.com/{TENANT}/v2.0'


@pytest.fixture(scope='module')
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def setup(keypair):
    resolver = Mock()
    resolver.get_signing_key_from_jwt.return_value = SimpleNamespace(key=keypair.public_key())
    with patch('auth.mcp_identity.jwt.PyJWKClient', return_value=resolver) as client:
        verifier = EntraVerifier(TENANT, AUDIENCE, 'context.read')
    client.assert_called_once_with(f'https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys', timeout=5)
    return verifier, resolver


def claims(**overrides):
    now = int(time.time())
    return {'iss':ISSUER, 'aud':AUDIENCE, 'tid':TENANT, 'oid':USER, 'ver':'2.0',
            'scp':'profile context.read', 'iat':now-5, 'nbf':now-5, 'exp':now+300, **overrides}


def sign(key, payload, headers=None):
    return jwt.encode(payload, key, algorithm='RS256', headers={'kid':'trusted', **(headers or {})})


def test_valid_delegated_identity_has_stable_tenant_and_user(setup, keypair):
    verifier, resolver = setup
    token = sign(keypair, claims())
    assert verifier.verify(token) == Requester(TENANT, USER)
    resolver.get_signing_key_from_jwt.assert_called_once_with(token)


@pytest.mark.parametrize('changes', [
    {'aud':'wrong'}, {'aud':[AUDIENCE, 'another']}, {'iss':'https://evil.example'},
    {'tid':OTHER_TENANT}, {'ver':'1.0'}, {'oid':'not-a-guid'}, {'oid':USER.upper()},
    {'oid':None}, {'scp':'context.read.evil'}, {'scp':['context.read']}, {'scp':''},
    {'exp':1}, {'nbf':4102444800}, {'iat':4102444800},
])
def test_signed_tokens_with_wrong_identity_scope_or_time_fail_closed(setup, keypair, changes):
    assert setup[0].verify(sign(keypair, claims(**changes))) is None


@pytest.mark.parametrize('field', ['aud', 'iss', 'tid', 'oid', 'ver', 'scp', 'exp', 'iat', 'nbf'])
def test_missing_required_claims_fail_closed(setup, keypair, field):
    payload = claims()
    del payload[field]
    assert setup[0].verify(sign(keypair, payload)) is None


def test_forged_signature_and_application_token_fail_closed(setup, keypair):
    verifier, _ = setup
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert verifier.verify(sign(attacker, claims())) is None
    app_claims = claims(roles=['context.read'])
    del app_claims['scp']
    assert verifier.verify(sign(keypair, app_claims)) is None


@pytest.mark.parametrize('token', [None, '', 'x'*16385, 'not-a-token', 123])
def test_malformed_tokens_fail_before_key_lookup(setup, token):
    verifier, resolver = setup
    assert verifier.verify(token) is None
    resolver.get_signing_key_from_jwt.assert_not_called()


def test_bad_headers_rejected_without_fetching_keys(setup, keypair):
    verifier, resolver = setup
    for token in (jwt.encode(claims(), keypair, algorithm='RS256'),
                  sign(keypair, claims(), {'kid':''}), sign(keypair, claims(), {'kid':'a'*129}),
                  jwt.encode(claims(), 'secret' * 8, algorithm='HS256', headers={'kid':'trusted'})):
        assert verifier.verify(token) is None
    resolver.get_signing_key_from_jwt.assert_not_called()


def test_key_resolution_failure_is_not_exposed(setup, keypair):
    verifier, resolver = setup
    resolver.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientConnectionError('private')
    assert verifier.verify(sign(keypair, claims())) is None


@pytest.mark.parametrize('tenant,audience,scope', [
    ('not-a-guid', AUDIENCE, 'context.read'), (TENANT.upper(), AUDIENCE, 'context.read'),
    (None, AUDIENCE, 'context.read'), (TENANT, '', 'context.read'), (TENANT, None, 'context.read'),
    (TENANT, AUDIENCE, ''), (TENANT, AUDIENCE, 'two scopes'), (TENANT, AUDIENCE, None),
])
def test_bad_configuration_rejected(tenant, audience, scope):
    with pytest.raises(ValueError):
        EntraVerifier(tenant, audience, scope)

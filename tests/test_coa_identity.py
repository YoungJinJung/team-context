import json
import time
from uuid import uuid4
from unittest.mock import patch

import pytest
pytest.importorskip('coa_common')
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from auth.coa_identity import CoaIdentity, Principal

TENANT = '12345678-1234-1234-1234-123456789abc'
USER = '87654321-4321-4321-4321-cba987654321'
ISSUER = f'https://login.microsoftonline.com/{TENANT}/v2.0'
AUD = 'api://team-context'
SCOPE = 'context.connect'
COGNITO = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123'


@pytest.fixture(scope='module')
def private_key():
    return rsa.generate_private_key(public_exponent=65537,key_size=2048)


def configured(issuer=ISSUER,mode='entra'):
    return CoaIdentity(issuer,AUD,'us-east-1',SCOPE,mode,True)


def jwks(key):
    result=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    result.update({'kid':'test-key','alg':'RS256','use':'sig'})
    return {'keys':[result]}


def claims(**changes):
    now=int(time.time())
    return {'iss':ISSUER,'sub':'user-subject','aud':AUD,'tid':TENANT,'oid':USER,'ver':'2.0',
            'scp':'openid context.connect','iat':now-10,'exp':now+300,**changes}


def signed(key,payload):
    return 'Bearer '+jwt.encode(payload,key,algorithm='RS256',headers={'kid':'test-key'})


def verify(verifier,key,payload):
    with patch.object(verifier._authorizer,'_get_jwks',return_value=jwks(key)):
        return verifier.verify(signed(key,payload))


def test_real_coa_authorizer_verifies_signed_entra_user(private_key):
    principal=verify(configured(),private_key,claims())
    assert principal==Principal(ISSUER,'user-subject')


def test_issuer_qualified_subject_does_not_alias_other_tenants(private_key):
    other=str(uuid4()); issuer=f'https://login.microsoftonline.com/{other}/v2.0'
    first=verify(configured(),private_key,claims())
    second=verify(configured(issuer),private_key,claims(iss=issuer,tid=other))
    assert first != second and second==Principal(issuer,'user-subject')


@pytest.mark.parametrize('changes',[
    {'aud':'wrong'}, {'iss':'https://evil.example'}, {'tid':str(uuid4())}, {'oid':'bad'}, {'oid':None},
    {'ver':'1.0'}, {'sub':''}, {'sub':None}, {'sub':'bad\nsubject'}, {'scp':'context.connect.other'},
    {'scp':['context.connect']}, {'scp':''}, {'exp':1}, {'exp':int(time.time())-1}, {'exp':'9999999999'},
    {'nbf':int(time.time())+10}, {'iat':int(time.time())+10}, {'nbf':'1'}, {'iat':True},
])
def test_entra_claim_failures_are_denied(private_key,changes):
    assert verify(configured(),private_key,claims(**changes)) is None


@pytest.mark.parametrize('field',['iss','aud','tid','oid','ver','sub','scp','exp'])
def test_missing_claims_are_denied(private_key,field):
    payload=claims();del payload[field]
    assert verify(configured(),private_key,payload) is None


def test_signature_and_key_failure_are_denied(private_key):
    attacker=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    verifier=configured()
    with patch.object(verifier._authorizer,'_get_jwks',return_value=jwks(private_key)):
        assert verifier.verify(signed(attacker,claims())) is None
    with patch.object(verifier._authorizer,'_get_jwks',side_effect=OSError('private')):
        assert verifier.verify(signed(private_key,claims())) is None


def test_application_token_without_delegated_scope_is_denied(private_key):
    payload=claims(roles=['context.connect']);del payload['scp']
    assert verify(configured(),private_key,payload) is None


def test_cognito_access_token_requires_user_and_scope(private_key):
    now=int(time.time())
    payload={'iss':COGNITO,'sub':'cognito-user','client_id':AUD,'token_use':'access','username':'user',
             'scope':'openid context.connect','iat':now-5,'exp':now+300}
    verifier=configured(COGNITO,'cognito')
    assert verify(verifier,private_key,payload)==Principal(COGNITO,'cognito-user')
    for changes in ({'username':''},{'username':None},{'token_use':'id'},{'scope':'openid'},{'scope':['context.connect']}):
        assert verify(verifier,private_key,{**payload,**changes}) is None
    no_user=dict(payload);del no_user['username']
    assert verify(verifier,private_key,no_user) is None


@pytest.mark.parametrize('authorization',[None,'','Basic token','Bearer ','Bearer '+'x'*16385,42])
def test_malformed_header_does_not_call_authorizer(authorization):
    verifier=configured()
    with patch.object(verifier._authorizer,'verify_claims') as auth:
        assert verifier.verify(authorization) is None
        if authorization != 'Bearer ': auth.assert_not_called()


@pytest.mark.parametrize('changes',[
    {'auth_enabled':False},{'auth_enabled':None},{'issuer':'http://login.microsoftonline.com/'+TENANT+'/v2.0'},
    {'issuer':'https://evil.example/'+TENANT+'/v2.0'},{'issuer':ISSUER+'?x=1'},{'issuer':ISSUER+'#fragment'},
    {'issuer':'https://user@login.microsoftonline.com/'+TENANT+'/v2.0'},{'issuer':'https://login.microsoftonline.com:444/'+TENANT+'/v2.0'},
    {'issuer':'https://login.microsoftonline.com/bad/v2.0'},{'mode':'other'}, {'audience':''}, {'region':''}, {'scope':''}, {'scope':'one two'},
])
def test_invalid_config_fails_closed(changes):
    config={'issuer':ISSUER,'audience':AUD,'region':'us-east-1','scope':SCOPE,'mode':'entra','auth_enabled':True}
    config.update(changes)
    with pytest.raises(ValueError): CoaIdentity(**config)


def test_invalid_cognito_region_or_pool_rejected():
    with pytest.raises(ValueError): CoaIdentity('https://cognito-idp.us-west-2.amazonaws.com/us-east-1_abc',AUD,'us-east-1',SCOPE,'cognito',True)
    with pytest.raises(ValueError): CoaIdentity('https://cognito-idp.us-east-1.amazonaws.com/other',AUD,'us-east-1',SCOPE,'cognito',True)


@pytest.mark.parametrize('subject,valid',[
    ('a'*255,True),('a'*256,False),('name with space',True),('bad\x1f',False),('bad\x7f',False),('name\x80',True),
])
def test_subject_length_and_control_character_boundaries(private_key,subject,valid):
    result=verify(configured(),private_key,claims(sub=subject))
    assert result == (Principal(ISSUER,subject) if valid else None)


@pytest.mark.parametrize('issuer',[
    'https:///missing',
    'https://:password@login.microsoftonline.com/'+TENANT+'/v2.0',
    'https://login.microsoftonline.com/'+TENANT,
    'https://login.microsoftonline.com/'+TENANT+'/v2.0/extra',
    'https://login.microsoftonline.com/'+TENANT+'/v1.0',
])
def test_bad_issuer_syntax_rejected(issuer):
    with pytest.raises(ValueError,match='^Invalid issuer$'):
        CoaIdentity(issuer,AUD,'us-east-1',SCOPE,'entra',True)


def test_cognito_uppercase_pool_is_supported():
    verifier=configured('https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AbC123','cognito')
    assert verifier._mode == 'cognito'


def test_request_length_and_time_boundaries_are_strict():
    verifier=configured(); now=1700000000
    valid=claims(iat=now,nbf=now,exp=now+1)
    with patch('auth.coa_identity.time.time',return_value=now), patch.object(verifier._authorizer,'verify_claims',return_value=valid) as call:
        exactly='Bearer '+'x'*(16384-len('Bearer '))
        assert verifier.verify(exactly)==Principal(ISSUER,'user-subject')
        assert verifier.verify(exactly+'x') is None
        call.assert_called_once_with(exactly)
        call.reset_mock()
        call.return_value={**valid,'exp':now}
        assert verifier.verify('Bearer test') is None
        call.return_value={**valid,'iat':now+1}
        assert verifier.verify('Bearer test') is None
        call.return_value={**valid,'nbf':now+1}
        assert verifier.verify('Bearer test') is None


def test_configuration_errors_are_safe_and_actionable():
    with pytest.raises(ValueError,match='^Invalid authentication configuration$'):
        CoaIdentity(ISSUER,'','us-east-1',SCOPE,'entra',True)
    with pytest.raises(ValueError,match='^Invalid identity provider$'):
        CoaIdentity(ISSUER,AUD,'us-east-1',SCOPE,'unknown',True)
    with pytest.raises(ValueError,match='^Invalid issuer$'):
        CoaIdentity('https://evil.example/private',AUD,'us-east-1',SCOPE,'cognito',True)
    with pytest.raises(ValueError,match='^Invalid issuer$'):
        CoaIdentity('https://login.microsoftonline.com/bad/v2.0',AUD,'us-east-1',SCOPE,'entra',True)

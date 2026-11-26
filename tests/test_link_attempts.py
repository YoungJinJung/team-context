import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
pytest.importorskip('coa_common')
from botocore.exceptions import ClientError
from auth.coa_identity import Principal
from auth.link_attempts import LinkAttempts, _digest

ISSUER='https://login.microsoftonline.com/12345678-1234-1234-1234-123456789abc/v2.0'
PRINCIPAL=Principal(ISSUER,'subject-a')
CONFIG={'github':('client-id','https://link.example/oauth/github/callback'),
        'gitlab':('gitlab-id','https://link.example/oauth/gitlab/callback')}


class Database:
    def __init__(self):
        self.items={}
        self.lock=threading.Lock()

    def put_item(self, **kwargs):
        assert kwargs['TableName']=='attempts' and kwargs['ConditionExpression']=='attribute_not_exists(state_key)'
        item=kwargs['Item']; key=item['state_key']['S']
        with self.lock:
            if key in self.items: raise conditional()
            self.items[key]=item

    def delete_item(self, **kwargs):
        assert kwargs['TableName']=='attempts' and kwargs['ReturnValues']=='ALL_OLD'
        assert kwargs['ConditionExpression']=='#nonce = :nonce AND #expiry > :now'
        assert kwargs['ExpressionAttributeNames']=={'#nonce':'nonce_hash','#expiry':'expires_at'}
        key=kwargs['Key']['state_key']['S']; values=kwargs['ExpressionAttributeValues']
        with self.lock:
            item=self.items.get(key)
            if not item or item['nonce_hash'] != values[':nonce'] or int(item['expires_at']['N']) <= int(values[':now']['N']):
                raise conditional()
            del self.items[key]
            return {'Attributes':item}


def conditional():
    return ClientError({'Error':{'Code':'ConditionalCheckFailedException','Message':'private'}},'DeleteItem')


class FakeKMS:
    def __init__(self): self.payloads={};self.calls=[]
    def encrypt(self, **kwargs):
        self.calls.append(('encrypt',kwargs))
        blob=b'cipher-'+str(len(self.payloads)).encode()
        self.payloads[blob]=(kwargs['Plaintext'],kwargs['EncryptionContext'])
        return {'CiphertextBlob':blob}
    def decrypt(self, **kwargs):
        self.calls.append(('decrypt',kwargs))
        plain, context=self.payloads[kwargs['CiphertextBlob']]
        if kwargs['EncryptionContext'] != context: raise ValueError('ciphertext mismatch')
        return {'Plaintext':plain}


@pytest.fixture
def setup():
    verifier=Mock();verifier.verify.return_value=PRINCIPAL
    db=Database();kms=FakeKMS()
    return LinkAttempts(verifier,db,kms,'attempts','kms-key',CONFIG),verifier,db,kms


def state_of(result): return parse_qs(urlsplit(result.url).query)['state'][0]


@pytest.mark.parametrize('provider,origin',[('github','https://github.com/login/oauth/authorize'),('gitlab','https://gitlab.com/oauth/authorize')])
def test_start_binds_verified_principal_and_encrypts_sensitive_state(setup,provider,origin):
    store,verify,db,kms=setup
    result=store.start('Bearer example',provider)
    params=parse_qs(urlsplit(result.url).query); state=params['state'][0]
    assert result.url.startswith(origin+'?') and len(result.browser_nonce)==43
    assert params['client_id']==[CONFIG[provider][0]] and params['redirect_uri']==[CONFIG[provider][1]]
    verify.verify.assert_called_once_with('Bearer example')
    assert result.browser_nonce not in repr(result) and state not in repr(result)
    item=db.items[_digest(provider+':'+state)]
    assert item['nonce_hash']['S']==_digest(result.browser_nonce)
    assert 'subject-a' not in repr(item) and result.browser_nonce not in repr(item) and state not in repr(item)
    plain=json.loads(kms.calls[0][1]['Plaintext'])
    assert plain['subject']=='subject-a' and plain['issuer']==ISSUER and plain['provider']==provider
    assert plain['nonce']==_digest(result.browser_nonce) and plain['state']==_digest(provider+':'+state)
    assert kms.calls[0][1]['KeyId']=='kms-key'
    assert kms.calls[0][1]['EncryptionContext']=={'purpose':'team-context-account-link','state':_digest(provider+':'+state)}
    challenge=base64.urlsafe_b64encode(hashlib.sha256(plain['verifier'].encode()).digest()).rstrip(b'=').decode()
    assert params['code_challenge']==[challenge] and params['code_challenge_method']==['S256']


@pytest.mark.parametrize('principal',[None,{},'subject',Principal('', ''),Principal('', 'valid'),Principal('https://issuer.example','')])
def test_missing_identity_fails_closed(setup,principal):
    store,verify,db,kms=setup
    verify.verify.return_value=principal
    with pytest.raises(ValueError,match='^Unable to start account link$'): store.start('Bearer fake','github')
    assert not db.items and not kms.calls


def test_unknown_provider_rejected_before_storage(setup):
    store,_,db,kms=setup
    with pytest.raises(ValueError,match='^Unable to start account link$'): store.start('Bearer fake','evil')
    assert not db.items and not kms.calls


def test_callback_consumes_once_and_returns_only_server_internal_state(setup):
    store,_,db,kms=setup
    result=store.start('Bearer example','github');state=state_of(result)
    attempt=store.consume('Bearer example','github',state,result.browser_nonce)
    assert attempt.principal==PRINCIPAL and attempt.provider=='github'
    assert attempt.client_id=='client-id' and attempt.redirect_uri==CONFIG['github'][1]
    assert len(attempt.verifier)==64 and attempt.verifier not in repr(attempt)
    assert not db.items
    assert kms.calls[-1][1]['KeyId']=='kms-key'
    with pytest.raises(ValueError,match='Invalid or expired account link'): store.consume('Bearer example','github',state,result.browser_nonce)


def test_wrong_browser_nonce_does_not_consume_correct_users_attempt(setup):
    store,_,db,_=setup
    first=store.start('Bearer user-a','github');second=store.start('Bearer user-b','github')
    with pytest.raises(ValueError): store.consume('Bearer example','github',state_of(first),second.browser_nonce)
    assert len(db.items)==2
    assert store.consume('Bearer example','github',state_of(first),first.browser_nonce).principal==PRINCIPAL


def test_expired_attempt_rejected_even_before_ttl_cleanup(setup):
    store,_,db,_=setup
    with patch('auth.link_attempts.time.time',return_value=1000): result=store.start('Bearer user','gitlab')
    with patch('auth.link_attempts.time.time',return_value=1300):
        with pytest.raises(ValueError): store.consume('Bearer example','gitlab',state_of(result),result.browser_nonce)
    assert len(db.items)==1


def test_ciphertext_expiry_is_rechecked_after_conditional_delete(setup):
    store,_,db,_=setup
    with patch('auth.link_attempts.time.time',return_value=1000): result=store.start('Bearer user','gitlab')
    state=state_of(result)
    db.items[_digest('gitlab:'+state)]['expires_at']['N']='9999'
    with patch('auth.link_attempts.time.time',return_value=1300):
        with pytest.raises(ValueError): store.consume('Bearer example','gitlab',state,result.browser_nonce)
    assert not db.items


def test_concurrent_callback_has_one_winner(setup):
    store,_,_,_=setup
    result=store.start('Bearer user','github');state=state_of(result)
    def consume(_):
        try: return store.consume('Bearer example','github',state,result.browser_nonce)
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes=list(pool.map(consume,range(16)))
    assert sum(item is not None for item in outcomes)==1


@pytest.mark.parametrize('state,nonce',[(None,'a'*43),('a'*43,None),('a'*42,'a'*43),('a'*43,'a'*44),('!'*43,'a'*43)])
def test_bad_state_or_nonce_never_reads_storage(setup,state,nonce):
    store,_,db,kms=setup
    with pytest.raises(ValueError): store.consume('Bearer example','github',state,nonce)
    assert not db.items and not kms.calls


@pytest.mark.parametrize('provider,registration',[
    ('evil',('id','https://link.example/oauth/evil/callback')),
    ('github',['id','https://link.example/oauth/github/callback']),
    ('github',('','https://link.example/oauth/github/callback')),
    ('github',('id','http://link.example/oauth/github/callback')),
    ('github',('id','https://link.example:444/oauth/github/callback')),
    ('github',('id','https://link.example/oauth/github/callback\n')),
    ('github',('id','https://link.example./oauth/github/callback')),
    ('github',('id','https://user@link.example/oauth/github/callback')),
    ('github',('id','https://link.example/oauth/github/callback?x=1')),
    ('github',('id','https://link.example/oauth/github/callback#fragment')),
    ('github',('id','https://link.example/oauth/gitlab/callback')),
])
def test_invalid_provider_configuration_rejected(provider,registration):
    with pytest.raises(ValueError,match='^Invalid provider registration$'): LinkAttempts(Mock(),Mock(),Mock(),'table','key',{provider:registration})


def test_failures_do_not_return_credentials_or_store_half_attempt(setup):
    store,_,db,kms=setup
    with patch.object(kms,'encrypt',side_effect=OSError('private')):
        with pytest.raises(OSError): store.start('Bearer token','github')
    assert not db.items
    with patch.object(db,'put_item',side_effect=OSError('private')):
        with pytest.raises(OSError): store.start('Bearer token','github')
    assert not db.items


def test_callback_on_wrong_provider_cannot_claim_attempt(setup):
    store,_,db,_=setup
    result=store.start('Bearer example','github')
    with pytest.raises(ValueError,match='Invalid or expired account link'):
        store.consume('Bearer example','gitlab',state_of(result),result.browser_nonce)
    assert len(db.items)==1
    assert store.consume('Bearer example','github',state_of(result),result.browser_nonce).principal==PRINCIPAL


@pytest.mark.parametrize('provider',[None,[],{},'unknown'])
def test_unknown_callback_provider_never_reads_storage(setup,provider):
    store,_,db,kms=setup
    with pytest.raises(ValueError): store.consume('Bearer example',provider,'a'*43,'b'*43)
    assert not db.items and not kms.calls


def test_different_verified_users_remain_distinct(setup):
    store,verifier,_,_=setup
    user_b=Principal(ISSUER,'subject-b')
    verifier.verify.side_effect=lambda token: {'Bearer first':PRINCIPAL,'Bearer second':user_b}.get(token)
    first=store.start('Bearer first','github');second=store.start('Bearer second','github')
    assert store.consume('Bearer second','github',state_of(second),second.browser_nonce).principal==user_b
    assert store.consume('Bearer first','github',state_of(first),first.browser_nonce).principal==PRINCIPAL


def test_ciphertext_cannot_be_swapped_between_attempts(setup):
    store,_,db,_=setup
    first=store.start('Bearer first','github');second=store.start('Bearer second','github')
    first_key=_digest('github:'+state_of(first));second_key=_digest('github:'+state_of(second))
    db.items[first_key]['ciphertext']=db.items[second_key]['ciphertext']
    with pytest.raises(ValueError): store.consume('Bearer example','github',state_of(first),first.browser_nonce)
    assert store.consume('Bearer example','github',state_of(second),second.browser_nonce).principal==PRINCIPAL


def test_corrupt_or_reconfigured_payload_is_never_returned(setup):
    store,_,db,kms=setup
    result=store.start('Bearer example','github');state=state_of(result)
    blob=db.items[_digest('github:'+state)]['ciphertext']['B']
    plain,context=kms.payloads[blob]; data=json.loads(plain)
    data['redirect_uri']='https://evil.example/oauth/github/callback'
    kms.payloads[blob]=(json.dumps(data).encode(),context)
    with pytest.raises(ValueError,match='Invalid or expired account link'): store.consume('Bearer example','github',state,result.browser_nonce)
    assert not db.items


def test_actual_coa_authorizer_binds_signed_subject_to_attempt():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from auth.coa_identity import CoaIdentity
    from time import time
    tenant='12345678-1234-1234-1234-123456789abc'
    user='87654321-4321-4321-4321-cba987654321'
    issuer=f'https://login.microsoftonline.com/{tenant}/v2.0'
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk.update({'kid':'test','alg':'RS256','use':'sig'})
    verifier=CoaIdentity(issuer,'resource','us-east-1','context.connect','entra',True)
    now=int(time());payload={'iss':issuer,'sub':'verified-subject','aud':'resource','tid':tenant,'oid':user,'ver':'2.0','scp':'context.connect','iat':now-1,'exp':now+300}
    token='Bearer '+jwt.encode(payload,key,algorithm='RS256',headers={'kid':'test'})
    db=Database();kms=FakeKMS();store=LinkAttempts(verifier,db,kms,'attempts','kms-key',CONFIG)
    with patch.object(verifier._authorizer,'_get_jwks',return_value={'keys':[jwk]}):
        result=store.start(token,'github')
        assert store.consume(token,'github',state_of(result),result.browser_nonce).principal==Principal(issuer,'verified-subject')
        with pytest.raises(ValueError): store.start('Bearer forged','github')


@pytest.mark.parametrize('changes',[
    {'issuer':''},{'subject':''},{'subject':'secret\nsubject'},{'verifier':'bad'},
    {'state':'other'},{'nonce':'other'},{'expires':True},{'expires':0},
    {'provider':'gitlab'},{'client_id':'other'},
])
def test_decrypted_state_requires_matching_valid_fields(setup,changes):
    store,_,db,kms=setup
    result=store.start('Bearer user','github');state=state_of(result)
    blob=db.items[_digest('github:'+state)]['ciphertext']['B']
    plain,context=kms.payloads[blob];payload=json.loads(plain);payload.update(changes)
    kms.payloads[blob]=(json.dumps(payload).encode(),context)
    with pytest.raises(ValueError,match='Invalid or expired account link'):
        store.consume('Bearer example','github',state,result.browser_nonce)
    assert not db.items


def test_kms_failure_after_consume_never_allows_replay(setup):
    store,_,db,kms=setup
    result=store.start('Bearer user','github');state=state_of(result)
    with patch.object(kms,'decrypt',side_effect=OSError('unavailable')):
        with pytest.raises(OSError): store.consume('Bearer example','github',state,result.browser_nonce)
    assert not db.items
    with pytest.raises(ValueError): store.consume('Bearer example','github',state,result.browser_nonce)


@pytest.mark.parametrize('table,key,registrations',[(None,'key',CONFIG),('', 'key',CONFIG),('table',None,CONFIG),('table','',CONFIG),('table','key',None),('table','key',{})])
def test_missing_trusted_configuration_is_rejected(table,key,registrations):
    with pytest.raises(ValueError,match='Invalid link configuration'):
        LinkAttempts(Mock(),Mock(),Mock(),table,key,registrations)


def test_callback_revalidates_same_coa_user_and_rejects_expired_identity(setup):
    store,verifier,db,_=setup
    result=store.start('Bearer original','github');state=state_of(result)
    verifier.verify.return_value=None
    with pytest.raises(ValueError): store.consume('Bearer expired','github',state,result.browser_nonce)
    assert len(db.items)==1
    verifier.verify.return_value=Principal(ISSUER,'different-user')
    with pytest.raises(ValueError): store.consume('Bearer other','github',state,result.browser_nonce)
    assert not db.items
    verifier.verify.return_value=PRINCIPAL
    with pytest.raises(ValueError): store.consume('Bearer original','github',state,result.browser_nonce)


def test_different_issuer_cannot_complete_original_users_link(setup):
    store,verifier,_,_=setup
    result=store.start('Bearer original','github')
    verifier.verify.return_value=Principal('https://other-issuer.example','subject-a')
    with pytest.raises(ValueError): store.consume('Bearer other','github',state_of(result),result.browser_nonce)


def test_uppercase_client_id_is_supported():
    store=LinkAttempts(Mock(),Database(),FakeKMS(),'attempts','kms-key',{'github':('Iv1ABC123','https://link.example/oauth/github/callback')})
    store._verifier.verify.return_value=PRINCIPAL
    assert parse_qs(urlsplit(store.start('Bearer user','github').url).query)['client_id']==['Iv1ABC123']


@pytest.mark.parametrize('values',[
    ['bad','b'*64,'c'*43],
    ['a'*43,'b'*64,'bad'],
])
def test_invalid_generated_state_or_nonce_is_rejected_before_storage(setup,values):
    store,_,db,kms=setup
    with patch('auth.link_attempts.secrets.token_urlsafe',side_effect=values):
        with pytest.raises(ValueError,match='^Unable to start account link$'): store.start('Bearer user','github')
    assert not db.items and not kms.calls


def test_browser_nonce_uses_32_bytes_of_randomness(setup):
    store,_,_,_=setup
    with patch('auth.link_attempts.secrets.token_urlsafe',side_effect=['a'*43,'b'*64,'c'*43]) as random:
        assert store.start('Bearer user','github').browser_nonce=='c'*43
    assert [call.args for call in random.call_args_list]==[(32,),(48,),(32,)]


@pytest.mark.parametrize('principal',[Principal('', 'subject'),Principal('https://issuer.example','')])
def test_invalid_callback_principal_never_consumes_state(setup,principal):
    store,verifier,db,_=setup
    result=store.start('Bearer original','github');verifier.verify.return_value=principal
    with pytest.raises(ValueError,match='^Invalid or expired account link$'):
        store.consume('Bearer invalid','github',state_of(result),result.browser_nonce)
    assert len(db.items)==1

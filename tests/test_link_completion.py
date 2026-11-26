import io
import json
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest
pytest.importorskip('coa_common')
from botocore.exceptions import ClientError
from auth.coa_identity import Principal
from auth.link_attempts import ConsumedAttempt
from auth.link_completion import LinkCompletion, _secret_name

PRINCIPAL=Principal('https://issuer.example','user-a')
GITHUB=ConsumedAttempt(PRINCIPAL,'github','client-id','https://link.example/oauth/github/callback','v'*64)
GITLAB=ConsumedAttempt(PRINCIPAL,'gitlab','client-id','https://link.example/oauth/gitlab/callback','v'*64)
TOKENS={'access_token':'short-lived-access','refresh_token':'refresh-secret','expires_in':7200,'token_type':'bearer'}


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


@pytest.fixture
def setup():
    verifier=Mock();verifier.verify.return_value=PRINCIPAL
    attempts=Mock();attempts.consume.return_value=GITHUB
    secrets=Mock();secrets.get_secret_value.return_value={'SecretString':'{"client_secret":"app-secret"}'}
    return LinkCompletion(verifier,attempts,secrets,'kms-key','team-context/links/','app-secret-id'),verifier,attempts,secrets


def test_github_code_exchange_uses_fixed_hosts_and_stores_only_owner_secret(setup):
    service,_,attempts,secrets=setup
    with patch('auth.link_completion.open_url',side_effect=[Response(json.dumps(TOKENS).encode()),Response(b'{"id":42,"login":"not stored"}')]) as send, patch('auth.link_completion.time.time',return_value=1000):
        assert service.complete('Bearer coa','github','state','nonce','one-use-code')=={'connected':True}
    attempts.consume.assert_called_once_with('Bearer coa','github','state','nonce')
    secrets.get_secret_value.assert_called_once_with(SecretId='app-secret-id')
    token_request,user_request=[call.args[0] for call in send.call_args_list]
    assert token_request.full_url=='https://github.com/login/oauth/access_token' and token_request.method=='POST'
    assert token_request.get_header('Content-type')=='application/x-www-form-urlencoded' and token_request.get_header('Accept')=='application/json'
    assert parse_qs(token_request.data.decode()) == {'grant_type':['authorization_code'],'client_id':['client-id'],'redirect_uri':['https://link.example/oauth/github/callback'],'code':['one-use-code'],'code_verifier':['v'*64],'client_secret':['app-secret']}
    assert user_request.full_url=='https://api.github.com/user' and user_request.get_header('Authorization')=='Bearer short-lived-access'
    saved=secrets.create_secret.call_args.kwargs
    assert saved['Name']==_secret_name('team-context/links/',PRINCIPAL,'github') and saved['KmsKeyId']=='kms-key'
    assert 'user-a' not in saved['Name'] and 'short-lived-access' not in saved['Name']
    assert json.loads(saved['SecretString']) == {'issuer':PRINCIPAL.issuer,'subject':'user-a','provider':'github','provider_user_id':42,'access_token':'short-lived-access','refresh_token':'refresh-secret','expires_at':8200}


def test_gitlab_exchange_uses_fixed_endpoints_without_github_app_secret(setup):
    service,_,attempts,secrets=setup;attempts.consume.return_value=GITLAB
    with patch('auth.link_completion.open_url',side_effect=[Response(json.dumps(TOKENS).encode()),Response(b'{"id":7}')]) as send:
        assert service.complete('Bearer coa','gitlab','state','nonce','code')=={'connected':True}
    secrets.get_secret_value.assert_not_called()
    token_request,user_request=[call.args[0] for call in send.call_args_list]
    assert token_request.full_url=='https://gitlab.com/oauth/token'
    assert parse_qs(token_request.data.decode()) == {'grant_type':['authorization_code'],'client_id':['client-id'],'redirect_uri':['https://link.example/oauth/gitlab/callback'],'code':['code'],'code_verifier':['v'*64]}
    assert user_request.full_url=='https://gitlab.com/api/v4/user' and user_request.get_header('Authorization')=='Bearer short-lived-access'
    assert json.loads(secrets.create_secret.call_args.kwargs['SecretString'])['provider_user_id']==7


def test_github_requires_an_app_secret_but_gitlab_does_not(setup):
    _,verifier,attempts,secrets=setup
    service=LinkCompletion(verifier,attempts,secrets,'key','links/')
    with patch('auth.link_completion.open_url') as send:
        with pytest.raises(ValueError,match='^Unable to complete account link$'):
            service.complete('Bearer coa','github','state','nonce','code')
    send.assert_not_called();secrets.get_secret_value.assert_not_called()
    attempts.consume.return_value=GITLAB
    with patch('auth.link_completion.open_url',side_effect=[Response(json.dumps(TOKENS).encode()),Response(b'{"id":7}')]):
        assert service.complete('Bearer coa','gitlab','state','nonce','code')=={'connected':True}


@pytest.mark.parametrize('code',[None,'','x'*2049,'a b','a\nb',42])
def test_bad_codes_do_not_consume_state_or_access_secrets(setup,code):
    service,_,attempts,secrets=setup
    with pytest.raises(ValueError,match='^Unable to complete account link$'): service.complete('Bearer coa','github','state','nonce',code)
    attempts.consume.assert_not_called();secrets.get_secret_value.assert_not_called();secrets.create_secret.assert_not_called()


@pytest.mark.parametrize('attempt',[None,{},replace(GITHUB,provider='gitlab'),replace(GITHUB,principal=Principal('','subject')),replace(GITHUB,principal=Principal('issuer',''))])
def test_mismatched_attempt_does_not_exchange_code(setup,attempt):
    service,_,attempts,secrets=setup;attempts.consume.return_value=attempt
    with patch('auth.link_completion.open_url') as send:
        with pytest.raises(ValueError,match='^Unable to complete account link$'): service.complete('Bearer coa','github','state','nonce','code')
    send.assert_not_called();secrets.get_secret_value.assert_not_called();secrets.create_secret.assert_not_called()


@pytest.mark.parametrize('secret',[None,'{}','{"client_secret":""}','{"client_secret":1}','{"client_secret":"bad secret"}'])
def test_invalid_app_secret_does_not_send_credentials(setup,secret):
    service,_,_,secrets=setup;secrets.get_secret_value.return_value={'SecretString':secret}
    with patch('auth.link_completion.open_url') as send:
        with pytest.raises(ValueError,match='^Unable to complete account link$'): service.complete('Bearer coa','github','state','nonce','code')
    send.assert_not_called();secrets.create_secret.assert_not_called()


@pytest.mark.parametrize('token_response',[None,[],{}, {**TOKENS,'access_token':''}, {**TOKENS,'refresh_token':''}, {**TOKENS,'token_type':'basic'}, {**TOKENS,'expires_in':0}, {**TOKENS,'expires_in':86401}, {**TOKENS,'expires_in':True}, {**TOKENS,'expires_in':'7200'}])
def test_invalid_or_nonexpiring_tokens_are_never_stored(setup,token_response):
    service,_,_,secrets=setup
    with patch('auth.link_completion.open_url',return_value=Response(json.dumps(token_response).encode())) as send:
        with pytest.raises(ValueError,match='^Unable to complete account link$'): service.complete('Bearer coa','github','state','nonce','code')
    assert send.call_count==1;secrets.create_secret.assert_not_called()


@pytest.mark.parametrize('user',[None,[],{}, {'id':0},{'id':True},{'id':'42'}])
def test_invalid_user_response_does_not_create_binding(setup,user):
    service,_,_,secrets=setup
    with patch('auth.link_completion.open_url',side_effect=[Response(json.dumps(TOKENS).encode()),Response(json.dumps(user).encode())]):
        with pytest.raises(ValueError,match='^Unable to complete account link$'): service.complete('Bearer coa','github','state','nonce','code')
    secrets.create_secret.assert_not_called()


def test_duplicate_or_provider_failure_is_sanitized_without_token_leak(setup):
    service,_,_,secrets=setup
    with patch('auth.link_completion.open_url',side_effect=HTTPError('https://github.com',500,'secret-code',{},None)):
        with pytest.raises(ValueError,match='^Unable to complete account link$') as error: service.complete('Bearer coa','github','state','nonce','code')
    assert 'secret-code' not in str(error.value);secrets.create_secret.assert_not_called()
    secrets.create_secret.side_effect=ClientError({'Error':{'Code':'ResourceExistsException','Message':'private'}},'CreateSecret')
    with patch('auth.link_completion.open_url',side_effect=[Response(json.dumps(TOKENS).encode()),Response(b'{"id":42}')]):
        with pytest.raises(ValueError,match='^Unable to complete account link$') as error: service.complete('Bearer coa','github','state','nonce','code')
    assert 'private' not in str(error.value)


def test_no_user_info_or_token_is_returned():
    assert _secret_name('prefix/',PRINCIPAL,'github') != _secret_name('prefix/',Principal(PRINCIPAL.issuer,'user-b'),'github')
    assert _secret_name('prefix/',PRINCIPAL,'github') != _secret_name('prefix/',Principal('https://other.example','user-a'),'github')
    assert _secret_name('prefix/',PRINCIPAL,'github') != _secret_name('prefix/',PRINCIPAL,'gitlab')
    assert 'user-a' not in _secret_name('prefix/',PRINCIPAL,'github')


def test_disconnect_is_scoped_to_freshly_verified_identity_and_provider(setup):
    service,verifier,_,secrets=setup
    assert service.disconnect('Bearer user','github')=={'connected':False}
    verifier.verify.assert_called_once_with('Bearer user')
    secrets.delete_secret.assert_called_once_with(SecretId=_secret_name('team-context/links/',PRINCIPAL,'github'),ForceDeleteWithoutRecovery=True)
    verifier.verify.return_value=Principal(PRINCIPAL.issuer,'another-user')
    assert service.disconnect('Bearer other','gitlab')=={'connected':False}
    assert secrets.delete_secret.call_args.kwargs['SecretId']==_secret_name('team-context/links/',Principal(PRINCIPAL.issuer,'another-user'),'gitlab')


@pytest.mark.parametrize('identity,provider',[(None,'github'),(Principal('','s'),'github'),(Principal('i',''),'github'),(PRINCIPAL,'evil')])
def test_invalid_disconnect_never_deletes_secret(setup,identity,provider):
    service,verifier,_,secrets=setup;verifier.verify.return_value=identity
    with pytest.raises(ValueError,match='^Unable to disconnect account link$'): service.disconnect('Bearer coa',provider)
    secrets.delete_secret.assert_not_called()


def test_large_response_is_rejected(setup):
    service,_,_,secrets=setup
    with patch('auth.link_completion.open_url',return_value=Response(b'x'*65537)):
        with pytest.raises(ValueError,match='^Unable to complete account link$'): service.complete('Bearer coa','github','state','nonce','code')
    secrets.create_secret.assert_not_called()

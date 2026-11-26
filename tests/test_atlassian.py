import io
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from auth import atlassian, atlassian_preview


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def test_authorize_exchange_and_replay_rejection():
    auth = atlassian.Authorization('client', atlassian.CALLBACK)
    url = urlparse(auth.url())
    assert (url.scheme, url.netloc, url.path) == ('https', 'auth.atlassian.com', '/authorize')
    assert parse_qs(url.query) == {'audience':['api.atlassian.com'], 'client_id':['client'],
        'scope':['read:jira-work read:confluence-content.summary offline_access'],
        'redirect_uri':[atlassian.CALLBACK], 'state':[auth._state], 'response_type':['code'], 'prompt':['consent']}
    with patch.dict('os.environ', {'ATLASSIAN_APP_SECRET_ID':'secret-id'}), patch('auth.atlassian.vault.read', return_value={'client_secret':'app-secret'}) as read, patch('auth.atlassian.open_url', return_value=Response(b'{"access_token":"a","refresh_token":"r"}')) as send:
        assert auth.exchange(auth._state, 'code') == {'access_token':'a', 'refresh_token':'r'}
    read.assert_called_once_with('secret-id')
    req = send.call_args.args[0]
    assert req.full_url == 'https://auth.atlassian.com/oauth/token' and req.method == 'POST'
    assert req.get_header('Content-type') == 'application/json' and req.get_header('Accept') == 'application/json'
    assert json.loads(req.data) == {'grant_type':'authorization_code', 'client_id':'client', 'redirect_uri':atlassian.CALLBACK, 'code':'code', 'client_secret':'app-secret'}
    with pytest.raises(ValueError): auth.exchange(auth._state, 'code')


def test_refresh_uses_fixed_endpoint_and_rotated_credentials():
    with patch('auth.atlassian.vault.read', return_value={'client_secret':'secret'}), patch('auth.atlassian.open_url', return_value=Response(b'{"access_token":"new","refresh_token":"next"}')) as send:
        assert atlassian.refresh('client', 'old') == {'access_token':'new', 'refresh_token':'next'}
    req = send.call_args.args[0]
    assert req.full_url == 'https://auth.atlassian.com/oauth/token'
    assert json.loads(req.data) == {'grant_type':'refresh_token', 'client_id':'client', 'refresh_token':'old', 'client_secret':'secret'}


@pytest.mark.parametrize('secret', [None, [], {}, {'client_secret':''}, {'client_secret':1}])
def test_invalid_secret_never_sends_request(secret):
    with patch('auth.atlassian.vault.read', return_value=secret), patch('auth.atlassian.open_url') as send:
        with pytest.raises(ValueError, match='Invalid Atlassian app secret'): atlassian.refresh('id', 'token')
        send.assert_not_called()


@pytest.mark.parametrize('payload', [[], {}, {'access_token':'', 'refresh_token':'r'}, {'access_token':1, 'refresh_token':'r'}, {'access_token':'a', 'refresh_token':''}, {'access_token':'a', 'refresh_token':1}])
def test_invalid_tokens_rejected(payload):
    with patch('auth.atlassian.vault.read', return_value={'client_secret':'secret'}), patch('auth.atlassian.open_url', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError): atlassian.refresh('id', 'token')


def test_preview_fixed_host_scope_and_minimal_data():
    payload = [{'id':'site', 'name':'Team', 'url':'https://evil.example', 'scopes':['read:jira-work'], 'secret':'ignore'},
               {'id':'site', 'name':'Team', 'scopes':['read:confluence-content.summary']}]
    with patch('auth.atlassian_preview.open_url', return_value=Response(json.dumps(payload).encode())) as send:
        assert atlassian_preview.sites('token') == [{'id':'site','name':'Team','jira':True,'confluence':False}, {'id':'site','name':'Team','jira':False,'confluence':True}]
    req = send.call_args.args[0]
    assert req.full_url == 'https://api.atlassian.com/oauth/token/accessible-resources'
    assert req.get_header('Authorization') == 'Bearer token' and req.get_header('Accept') == 'application/json'


@pytest.mark.parametrize('payload', [{}, None, [None], [{'id':'','name':'n','scopes':[]}], [{'id':'id','name':1,'scopes':[]}], [{'id':'id','name':'n','scopes':None}], [{'id':'id','name':'n','scopes':[3]}], [None]*1001])
def test_preview_rejects_invalid_data(payload):
    with patch('auth.atlassian_preview.open_url', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError): atlassian_preview.sites('token')


def test_large_responses_are_rejected():
    with patch('auth.atlassian.vault.read', return_value={'client_secret':'secret'}), patch('auth.atlassian.open_url', return_value=Response(b'x'*(1024*1024+1))):
        with pytest.raises(ValueError): atlassian.refresh('client', 'refresh')
    with patch('auth.atlassian_preview.open_url', return_value=Response(b'x'*(1024*1024+1))):
        with pytest.raises(ValueError): atlassian_preview.sites('token')


def test_responses_obey_exact_byte_limit():
    from unittest.mock import Mock
    for module, call in [(atlassian, lambda: atlassian.refresh('client','refresh')), (atlassian_preview, lambda: atlassian_preview.sites('token'))]:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b' '*module.MAX_BYTES
        with patch.object(module, 'open_url', return_value=response), patch('auth.atlassian.vault.read', return_value={'client_secret':'secret'}):
            with pytest.raises(json.JSONDecodeError): call()
        response.read.assert_called_once_with(module.MAX_BYTES + 1)


def test_preview_accepts_exact_site_limit_and_rejects_one_more():
    site = {'id':'id','name':'name','scopes':[]}
    with patch('auth.atlassian_preview.open_url', return_value=Response(json.dumps([site]*1000).encode())):
        assert len(atlassian_preview.sites('token')) == 1000
    with patch('auth.atlassian_preview.open_url', return_value=Response(json.dumps([site]*1001).encode())):
        with pytest.raises(ValueError): atlassian_preview.sites('token')

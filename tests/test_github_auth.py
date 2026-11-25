import base64
import hashlib
import io
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import pytest
from auth import github, github_preview


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def test_authorization_and_token_exchange():
    auth = github.Authorization('client', github.CALLBACK)
    url = urlparse(auth.url())
    assert url.scheme == 'https' and url.netloc == 'github.com' and url.path == '/login/oauth/authorize'
    assert parse_qs(url.query) == {'client_id':['client'], 'redirect_uri':[github.CALLBACK], 'response_type':['code'], 'scope':['offline_access'], 'state':[auth._state], 'code_challenge_method':['S256'], 'code_challenge':[base64.urlsafe_b64encode(hashlib.sha256(auth._verifier.encode()).digest()).rstrip(b'=').decode()]}
    with patch('auth.github.vault.read', return_value={'client_secret':'app-secret'}), patch.dict('os.environ', {'GITHUB_APP_SECRET_ID':'fake-id'}), patch('auth.github.open_url', return_value=Response(b'{"access_token":"a","refresh_token":"r"}')) as send:
        assert auth.exchange(auth._state, 'code') == {'access_token':'a', 'refresh_token':'r'}
    req = send.call_args.args[0]
    assert req.full_url == 'https://github.com/login/oauth/access_token' and req.method == 'POST'
    assert req.get_header('Content-type') == 'application/x-www-form-urlencoded' and req.get_header('Accept') == 'application/json'
    assert parse_qs(req.data.decode()) == {'grant_type':['authorization_code'], 'client_id':['client'], 'redirect_uri':[github.CALLBACK], 'code':['code'], 'code_verifier':[auth._verifier], 'client_secret':['app-secret']}
    with pytest.raises(ValueError): auth.exchange(auth._state, 'code')


def test_refresh_uses_client_secret():
    with patch('auth.github.vault.read', return_value={'client_secret':'app-secret'}), patch.dict('os.environ', {'GITHUB_APP_SECRET_ID':'fake-id'}), patch('auth.github.open_url', return_value=Response(b'{"access_token":"a","refresh_token":"r"}')) as send:
        assert github.refresh('client', 'old') == {'access_token':'a','refresh_token':'r'}
    req = send.call_args.args[0]
    assert req.full_url == 'https://github.com/login/oauth/access_token'
    assert parse_qs(req.data.decode()) == {'grant_type':['refresh_token'], 'client_id':['client'], 'refresh_token':['old'], 'client_secret':['app-secret']}


@pytest.mark.parametrize('payload', [[], {}, {'access_token':'', 'refresh_token':'r'}, {'access_token':1, 'refresh_token':'r'}, {'access_token':'a', 'refresh_token':''}, {'access_token':'a', 'refresh_token':1}])
def test_bad_tokens(payload):
    with patch('auth.github.vault.read', return_value={'client_secret':'app-secret'}), patch.dict('os.environ', {'GITHUB_APP_SECRET_ID':'fake-id'}), patch('auth.github.open_url', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError, match='Invalid token response'): github.refresh('id', 'token')


def test_private_preview_uses_fixed_provider_and_limits_data():
    payload = [{'title':'Private PR', 'html_url':'https://github.com/owner/repo/pull/1', 'description':'not returned'}]
    with patch('auth.github_preview.open_url', return_value=Response(json.dumps(payload).encode())) as send:
        assert github_preview.pull_requests('token', 'owner/repo') == [{'title':'Private PR', 'url':payload[0]['html_url']}]
    req = send.call_args.args[0]
    assert req.full_url == 'https://api.github.com/repos/owner/repo/pulls?state=all&per_page=30&page=1'
    assert req.get_header('Authorization') == 'Bearer token'
    assert req.get_header('Accept') == 'application/vnd.github+json' and req.get_header('User-agent') == 'team-context-mvp'


@pytest.mark.parametrize('project', ['', '../me', 'https://evil.example/repo', None, 'owner/repo?x=1'])
def test_project_validation_prevents_token_forwarding(project):
    with patch('auth.github_preview.open_url') as send:
        with pytest.raises(ValueError): github_preview.pull_requests('secret', project)
        send.assert_not_called()


@pytest.mark.parametrize('payload', [{}, None, [dict(title='x',web_url='https://github.com/x')]*31])
def test_invalid_or_oversized_preview_fails(payload):
    with patch('auth.github_preview.open_url', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError, match='Invalid GitHub response'): github_preview.pull_requests('token', 'a/b')


@pytest.mark.parametrize('secret', [None, [], {}, {'client_secret':''}, {'client_secret':1}])
def test_bad_app_secret_fails_without_network(secret):
    with patch('auth.github.vault.read', return_value=secret), patch.dict('os.environ', {'GITHUB_APP_SECRET_ID':'secret-id'}), patch('auth.github.open_url') as send:
        with pytest.raises(ValueError, match='Invalid GitHub app secret'): github.refresh('id', 'token')
        send.assert_not_called()


def test_app_secret_is_loaded_by_reference():
    with patch.dict('os.environ', {'GITHUB_APP_SECRET_ID':'secret-id'}), patch('auth.github.vault.read', return_value={'client_secret':'secret'}) as read, patch('auth.github.open_url', return_value=Response(b'{"access_token":"a","refresh_token":"r"}')):
        github.refresh('id','refresh')
        read.assert_called_once_with('secret-id')

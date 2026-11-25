import base64
import hashlib
import io
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import pytest
from auth import gitlab, gitlab_preview


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def test_authorization_and_token_exchange():
    auth = gitlab.Authorization('client', gitlab.CALLBACK)
    url = urlparse(auth.url())
    assert url.scheme == 'https' and url.netloc == 'gitlab.com' and url.path == '/oauth/authorize'
    assert parse_qs(url.query) == {'client_id':['client'], 'redirect_uri':[gitlab.CALLBACK], 'response_type':['code'], 'scope':['read_api'], 'state':[auth._state], 'code_challenge_method':['S256'], 'code_challenge':[base64.urlsafe_b64encode(hashlib.sha256(auth._verifier.encode()).digest()).rstrip(b'=').decode()]}
    with patch('auth.gitlab.open_url', return_value=Response(b'{"access_token":"a","refresh_token":"r"}')) as send:
        assert auth.exchange(auth._state, 'code') == {'access_token':'a', 'refresh_token':'r'}
    req = send.call_args.args[0]
    assert req.full_url == 'https://gitlab.com/oauth/token' and req.method == 'POST'
    assert req.get_header('Content-type') == 'application/x-www-form-urlencoded' and req.get_header('Accept') == 'application/json'
    assert parse_qs(req.data.decode()) == {'grant_type':['authorization_code'], 'client_id':['client'], 'redirect_uri':[gitlab.CALLBACK], 'code':['code'], 'code_verifier':[auth._verifier]}
    with pytest.raises(ValueError): auth.exchange(auth._state, 'code')


def test_refresh_uses_exact_registered_callback():
    with patch('auth.gitlab.open_url', return_value=Response(b'{"access_token":"a","refresh_token":"r"}')) as send:
        assert gitlab.refresh('client', 'old') == {'access_token':'a','refresh_token':'r'}
    req = send.call_args.args[0]
    assert req.full_url == 'https://gitlab.com/oauth/token'
    assert parse_qs(req.data.decode()) == {'grant_type':['refresh_token'], 'client_id':['client'], 'refresh_token':['old'], 'redirect_uri':['http://127.0.0.1:8765/oauth/gitlab/callback']}


@pytest.mark.parametrize('payload', [[], {}, {'access_token':'', 'refresh_token':'r'}, {'access_token':1, 'refresh_token':'r'}, {'access_token':'a', 'refresh_token':''}, {'access_token':'a', 'refresh_token':1}])
def test_bad_tokens(payload):
    with patch('auth.gitlab.open_url', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError, match='Invalid token response'): gitlab.refresh('id', 'token')


def test_private_preview_uses_fixed_provider_and_limits_data():
    payload = [{'title':'Private MR', 'web_url':'https://gitlab.com/group/sub/project/-/merge_requests/1', 'description':'not returned'}]
    with patch('auth.gitlab_preview.open_url', return_value=Response(json.dumps(payload).encode())) as send:
        assert gitlab_preview.merge_requests('token', 'group/sub/project') == [{'title':'Private MR', 'url':payload[0]['web_url']}]
    req = send.call_args.args[0]
    assert req.full_url == 'https://gitlab.com/api/v4/projects/group%2Fsub%2Fproject/merge_requests?scope=all&per_page=30&page=1'
    assert req.get_header('Authorization') == 'Bearer token'
    assert req.get_header('Accept') == 'application/json' and req.get_header('User-agent') == 'team-context-mvp'


@pytest.mark.parametrize('project', ['', '../me', 'https://evil.example/repo', None, 'group/project?x=1'])
def test_project_validation_prevents_token_forwarding(project):
    with patch('auth.gitlab_preview.open_url') as send:
        with pytest.raises(ValueError): gitlab_preview.merge_requests('secret', project)
        send.assert_not_called()


@pytest.mark.parametrize('payload', [{}, None, [dict(title='x',web_url='https://gitlab.com/x')]*31])
def test_invalid_or_oversized_preview_fails(payload):
    with patch('auth.gitlab_preview.open_url', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError, match='Invalid GitLab response'): gitlab_preview.merge_requests('token', 'a/b')

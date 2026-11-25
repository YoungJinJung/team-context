import base64
import hashlib
import io
import json
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
import pytest

from auth import teams, teams_preview
from auth.sessions import Sessions


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def test_pkce_authorize_and_exchange_once():
    auth = teams.Authorization('client', 'http://127.0.0.1/callback')
    url = urlparse(auth.url())
    assert url.scheme == 'https' and url.netloc == 'login.microsoftonline.com'
    assert url.path == '/organizations/oauth2/v2.0/authorize'
    assert parse_qs(url.query) == {'client_id': ['client'], 'redirect_uri': ['http://127.0.0.1/callback'], 'response_type': ['code'], 'scope': [teams.SCOPES], 'state': [auth._state], 'code_challenge_method': ['S256'], 'code_challenge': [base64.urlsafe_b64encode(hashlib.sha256(auth._verifier.encode()).digest()).rstrip(b'=').decode()]}
    with patch('auth.teams.open_url', return_value=Response(b'{"access_token":"a","refresh_token":"r"}')) as send:
        assert auth.exchange(auth._state, 'code') == {'access_token': 'a', 'refresh_token': 'r'}
    req = send.call_args.args[0]
    assert req.full_url == teams.BASE + '/token' and req.method == 'POST'
    assert req.get_header('Content-type') == 'application/x-www-form-urlencoded'
    assert req.get_header('Accept') == 'application/json'
    assert parse_qs(req.data.decode()) == {'grant_type': ['authorization_code'], 'client_id': ['client'], 'redirect_uri': ['http://127.0.0.1/callback'], 'code': ['code'], 'code_verifier': [auth._verifier]}
    with pytest.raises(ValueError): auth.exchange(auth._state, 'code')


def test_refresh_uses_fixed_endpoint_and_scopes():
    with patch('auth.teams.open_url', return_value=Response(b'{"access_token":"new","refresh_token":"next"}')) as send:
        assert teams.refresh('client', 'old') == {'access_token': 'new', 'refresh_token': 'next'}
    assert parse_qs(send.call_args.args[0].data.decode()) == {'grant_type': ['refresh_token'], 'client_id': ['client'], 'refresh_token': ['old'], 'scope': [teams.SCOPES]}


@pytest.mark.parametrize('payload', [[], {}, {'access_token': '', 'refresh_token': 'r'}, {'access_token': 1, 'refresh_token': 'r'}, {'access_token': 'a', 'refresh_token': ''}, {'access_token': 'a', 'refresh_token': 1}])
def test_reject_invalid_tokens(payload):
    with patch('auth.teams.open_url', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError, match='Invalid token response'): teams.refresh('client', 'r')


def test_session_namespace_is_separate():
    microsoft = Sessions(teams.Authorization, teams.refresh, 'teams')
    session = 'a' * 43
    with patch('auth.sessions.session_store.get', return_value=None) as get:
        assert not microsoft.connected(session)
    get.assert_called_once_with(session, namespace='teams')
    with patch('auth.sessions.secrets.token_urlsafe', return_value=session):
        _, url = microsoft.start('client', 'http://127.0.0.1/callback')
    assert url.startswith(teams.BASE + '/authorize?')


def test_graph_previews_use_fixed_urls_and_bearer():
    team = '12345678-1234-1234-1234-123456789abc'
    for fn, expected in [(lambda: teams_preview.teams('token'), 'https://graph.microsoft.com/v1.0/me/joinedTeams'), (lambda: teams_preview.channels('token', team), 'https://graph.microsoft.com/v1.0/teams/' + team + '/channels?$select=id,displayName')]:
        with patch('auth.teams_preview.open_url', return_value=Response(b'{"value":[{"id":"id","displayName":"name","secret":"ignored"}]}')) as send:
            assert fn() == [{'id': 'id', 'name': 'name'}]
        req = send.call_args.args[0]
        assert req.full_url == expected and req.get_header('Authorization') == 'Bearer token'
        assert req.get_header('Accept') == 'application/json'


@pytest.mark.parametrize('team', ['', 'bad', None, '12345678-1234-1234-1234-123456789ABC', '../me'])
def test_reject_team_path_injection(team):
    with patch('auth.teams_preview.open_url') as send:
        with pytest.raises(ValueError, match='Invalid team ID'): teams_preview.channels('token', team)
        send.assert_not_called()


@pytest.mark.parametrize('payload', [[], {}, {'value': None}, {'value': {}}])
def test_reject_invalid_graph_response(payload):
    with patch('auth.teams_preview.open_url', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError, match='Invalid Graph response'): teams_preview.teams('token')


def test_provider_sessions_use_provider_refresh_and_isolated_references():
    from urllib.error import HTTPError
    session = 'a' * 43
    fake_auth = Mock()
    fake_auth.url.return_value = 'url'
    fake_auth.exchange.return_value = {'access_token': 'old', 'refresh_token': 'old-refresh'}
    fake_auth.client_id = 'client'
    renew = Mock(return_value={'access_token': 'new', 'refresh_token': 'next'})
    sessions = Sessions(lambda *_: fake_auth, renew, 'teams')
    with patch('auth.sessions.secrets.token_urlsafe', return_value=session), patch('auth.sessions.session_store.put') as put, patch('auth.sessions.session_store.get', return_value='id') as get, patch('auth.sessions.session_store.remove') as remove, patch('auth.sessions.vault.create', return_value='id'), patch('auth.sessions.vault.read', return_value={'client_id':'client', 'access_token':'old', 'refresh_token':'old-refresh'}), patch('auth.sessions.vault.replace') as replace, patch('auth.sessions.vault.delete') as delete:
        sessions.start('client', 'uri')
        sessions.complete(session, 'state', 'code')
        put.assert_called_once_with(session, 'id', namespace='teams')
        op = Mock(side_effect=[HTTPError('https://graph.microsoft.com', 401, 'expired', {}, None), 'ok'])
        assert sessions.use(session, op) == 'ok'
        renew.assert_called_once_with('client', 'old-refresh')
        replace.assert_called_once_with('id', {'client_id':'client', 'access_token':'new', 'refresh_token':'next'})
        assert [c.args for c in op.call_args_list] == [('old',), ('new',)]
        sessions.disconnect(session)
        assert all(c.args == (session,) and c.kwargs == {'namespace':'teams'} for c in get.call_args_list)
        remove.assert_called_once_with(session, namespace='teams')
        delete.assert_called_once_with('id')


def test_teams_asks_for_only_preview_permissions():
    assert teams.SCOPES.split() == ['offline_access', 'https://graph.microsoft.com/Team.ReadBasic.All', 'https://graph.microsoft.com/Channel.ReadBasic.All']

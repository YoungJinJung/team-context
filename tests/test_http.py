import http.client
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import pytest
import app


@pytest.fixture
def client():
    server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
    yield conn
    conn.close()
    server.shutdown()
    server.server_close()
    thread.join()


def request(conn, method, path, body=None, headers=None):
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    return response, data


def test_get_routes(client):
    response, data = request(client, 'GET', '/')
    assert response.status == 200 and b'<html' in data
    assert response.getheader('Content-Type') == 'text/html; charset=utf-8'
    assert response.getheader('X-Content-Type-Options') == 'nosniff'
    response, data = request(client, 'GET', '/api/connectors')
    assert response.status == 200 and json.loads(data)[0]['id'] == 'github'
    response, _ = request(client, 'GET', '/bad')
    assert response.status == 404


def test_post_success(client):
    body = json.dumps({'connector': 'github', 'config': {'repository': 'a/b'}})
    with patch('connectors.github.collect', return_value=[{'id': 'abc'}]), patch('app.persist') as persist:
        response, data = request(client, 'POST', '/api/collect', body)
    assert response.status == 200 and json.loads(data) == {'records': [{'id': 'abc'}]}
    persist.assert_called_once_with('github:a/b', [{'id': 'abc'}])


def test_post_failures(client):
    cases = [('/unknown', '{}', {}, 404), ('/api/collect', '{}', {'Origin': 'https://evil.example'}, 403), ('/api/collect', '', {}, 400), ('/api/collect', '{', {}, 400), ('/api/collect', json.dumps({'connector': 'missing', 'config': {}}), {}, 400), ('/api/collect', json.dumps({'connector': 'github', 'config': []}), {}, 400), ('/api/collect', 'a' * 4097, {}, 400)]
    for path, body, headers, status in cases:
        response, data = request(client, 'POST', path, body, headers)
        assert response.status == status and 'error' in json.loads(data)
    with patch('connectors.github.collect', side_effect=OSError('failure')):
        response, data = request(client, 'POST', '/api/collect', json.dumps({'connector': 'github', 'config': {'repository': 'a/b'}}))
    assert response.status == 502 and 'error' in json.loads(data)


def test_discovery_rejects_duplicate_ids():
    from types import SimpleNamespace
    with patch('app.pkgutil.iter_modules', return_value=[SimpleNamespace(name='one'), SimpleNamespace(name='two')]), patch('app.importlib.import_module', return_value=SimpleNamespace(MANIFEST={'id': 'same'})):
        with pytest.raises(ValueError, match='Duplicate connector: same'):
            app.discover()


def test_response_headers_and_error_messages(client):
    response, data = request(client, 'GET', '/missing')
    assert response.status == 404
    assert response.getheader('Content-Type') == 'application/json; charset=utf-8'
    assert json.loads(data) == {'error': 'Not found'}
    response, data = request(client, 'POST', '/api/collect', '{}', {'Origin': 'https://evil.example'})
    assert response.status == 403 and json.loads(data) == {'error': 'Invalid origin'}
    response, data = request(client, 'POST', '/api/collect', '')
    assert response.status == 400 and json.loads(data) == {'error': 'Invalid request size'}
    response, data = request(client, 'POST', '/api/collect', json.dumps({'connector': 'missing', 'config': {}}))
    assert response.status == 400 and json.loads(data) == {'error': 'Invalid connector or configuration'}
    with patch('connectors.github.collect', side_effect=OSError('failure')):
        response, data = request(client, 'POST', '/api/collect', json.dumps({'connector': 'github', 'config': {'repository': 'a/b'}}))
    assert response.status == 502 and json.loads(data) == {'error': 'Unable to fetch or store data. Check the source service and local AWS emulator.'}


def test_allowed_origins_and_connector_config(client):
    config = {'repository': 'example/project'}
    body = json.dumps({'connector': 'github', 'config': config})
    for origin in ('http://127.0.0.1:8765', 'http://localhost:8765'):
        with patch('connectors.github.collect', return_value=[]) as collect, patch('app.persist'):
            response, data = request(client, 'POST', '/api/collect', body, {'Origin': origin})
        assert response.status == 200 and json.loads(data) == {'records': []}
        collect.assert_called_once_with(config)


def test_response_content_length(client):
    for path in ('/', '/api/connectors', '/missing'):
        response, data = request(client, 'GET', path)
        assert response.getheader('Content-Length') == str(len(data))


def test_post_missing_route_message(client):
    response, data = request(client, 'POST', '/missing', '{}')
    assert response.status == 404 and json.loads(data) == {'error': 'Not found'}


def test_request_size_boundaries(client):
    # Exactly 4096 bytes is permitted, 4097 is not.
    payload = json.dumps({'connector': 'github', 'config': {'repository': 'a/b'}})
    with patch('connectors.github.collect', return_value=[]), patch('app.persist'):
        response, _ = request(client, 'POST', '/api/collect', payload.ljust(4096))
    assert response.status == 200
    response, data = request(client, 'POST', '/api/collect', payload.ljust(4097))
    assert response.status == 400 and json.loads(data)['error'] == 'Invalid request size'


def test_another_connector_can_persist_without_repository(client):
    from types import SimpleNamespace
    module = SimpleNamespace(collect=lambda config: [{'id': 'example:one'}], connection_id=lambda config: 'example:' + config['space'])
    body = json.dumps({'connector': 'example', 'config': {'space': 'one'}})
    with patch('app.discover', return_value={'example': module}), patch('app.persist') as persist:
        response, data = request(client, 'POST', '/api/collect', body)
    assert response.status == 200 and json.loads(data)['records'] == [{'id': 'example:one'}]
    persist.assert_called_once_with('example:one', [{'id': 'example:one'}])


def test_linear_authorization_and_safe_callback_logging(client, capsys):
    from unittest.mock import Mock
    sessions = Mock()
    sessions.start.return_value = ('safe-cookie', 'https://linear.app/oauth/authorize?state=state')
    sessions.complete.return_value = 'secret-id'
    sessions.connected.side_effect = [False, True]
    headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'}
    with patch('app.SESSIONS', sessions), patch.dict('os.environ', {'LINEAR_CLIENT_ID': 'public-id'}):
        response, data = request(client, 'POST', '/api/linear/start', '', headers)
        assert response.status == 200
        assert response.getheader('Content-Type') == 'application/json; charset=utf-8'
        assert response.getheader('Content-Length') == str(len(data))
        assert json.loads(data) == {'url': 'https://linear.app/oauth/authorize?state=state'}
        assert response.getheader('Cache-Control') == 'no-store'
        cookie = response.getheader('Set-Cookie')
        assert cookie == 'team_context_session=safe-cookie; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000'
        sessions.start.assert_called_once_with('public-id', app.CALLBACK)
        response, data = request(client, 'GET', '/oauth/linear/callback?code=private-code&state=state', headers={'Host': '127.0.0.1:8765', 'Cookie': cookie})
        assert response.status == 303 and response.getheader('Location') == '/'
        assert response.getheader('Content-Length') == '0'
        assert response.getheader('Cache-Control') == 'no-store'
        assert data == b'' and 'secret-id' not in str(response.getheaders())
        sessions.complete.assert_called_once_with('safe-cookie', 'state', 'private-code')
        response, data = request(client, 'GET', '/api/linear/status', headers={'Cookie': cookie})
        assert response.status == 200 and json.loads(data) == {'connected': True}
    logs = capsys.readouterr().err
    assert 'private-code' not in logs
    assert 'GET /oauth/linear/callback' in logs and '303' in logs


def test_linear_start_and_callback_reject_invalid_requests(client):
    with patch.dict('os.environ', {}, clear=True):
        response, _ = request(client, 'POST', '/api/linear/start', '', {'Host': '127.0.0.1:8765', 'Origin': 'http://evil.example'})
        assert response.status == 403
        response, data = request(client, 'POST', '/api/linear/start', '', {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'})
        assert response.status == 503 and json.loads(data) == {'error': 'Set LINEAR_CLIENT_ID to your Linear OAuth app client ID.'}
    response, data = request(client, 'GET', '/oauth/linear/callback?code=x&state=y', headers={'Host': 'evil.example'})
    assert response.status == 403 and json.loads(data) == {'error': 'Invalid host'}
    for query in ('', '?code=x', '?state=y', '?code=x&state=a&state=b'):
        response, _ = request(client, 'GET', '/oauth/linear/callback' + query, headers={'Host': '127.0.0.1:8765'})
        assert response.status == 400
    with patch('app.SESSIONS.complete', side_effect=ValueError('invalid')):
        response, _ = request(client, 'GET', '/oauth/linear/callback?code=x&state=y', headers={'Host': '127.0.0.1:8765'})
        assert response.status == 400
    with patch('app.SESSIONS.complete', side_effect=OSError('failure')):
        response, _ = request(client, 'GET', '/oauth/linear/callback?code=x&state=y', headers={'Host': '127.0.0.1:8765'})
        assert response.status == 502


def test_linear_full_flow_uses_session_and_never_returns_tokens(client):
    from auth.sessions import Sessions
    from urllib.parse import parse_qs, urlsplit
    sessions = Sessions()
    headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'}
    tokens = {'access_token': 'private-access', 'refresh_token': 'private-refresh'}
    with patch('app.SESSIONS', sessions), patch.dict('os.environ', {'LINEAR_CLIENT_ID': 'public-id'}), patch('auth.linear._token', return_value=tokens) as exchange, patch('auth.sessions.vault.create', return_value='private-secret-id') as store, patch('auth.sessions.session_store.put'), patch('auth.sessions.session_store.get', side_effect=[None, None, 'private-secret-id']):
        response, body = request(client, 'POST', '/api/linear/start', '', headers)
        cookie = response.getheader('Set-Cookie').split(';', 1)[0]
        state = parse_qs(urlsplit(json.loads(body)['url']).query)['state'][0]
        response, body = request(client, 'GET', '/api/linear/status', headers={'Cookie': cookie})
        assert json.loads(body) == {'connected': False}
        response, body = request(client, 'GET', '/oauth/linear/callback?code=abc&state=' + state, headers={'Host': '127.0.0.1:8765', 'Cookie': cookie})
        assert response.status == 303 and body == b''
        assert all(secret not in str(response.getheaders()) for secret in ('private-access', 'private-refresh', 'private-secret-id'))
        store.assert_called_once_with({**tokens, 'client_id': 'public-id'})
        assert exchange.call_args.args[0]['code'] == 'abc'
        response, body = request(client, 'GET', '/api/linear/status', headers={'Cookie': cookie})
        assert json.loads(body) == {'connected': True}
        response, body = request(client, 'GET', '/oauth/linear/callback?code=abc&state=' + state, headers={'Host': '127.0.0.1:8765', 'Cookie': cookie})
        assert response.status == 400


def test_linear_callback_failure_is_safe_and_start_requires_host(client):
    with patch.dict('os.environ', {'LINEAR_CLIENT_ID': 'public-id'}):
        for headers in ({'Host': 'evil.example', 'Origin': 'http://127.0.0.1:8765'}, {'Host': '127.0.0.1:8765'}, {'Host': '127.0.0.1:8765', 'Origin': 'http://localhost:8765'}):
            response, data = request(client, 'POST', '/api/linear/start', '', headers)
            assert response.status == 403 and json.loads(data) == {'error': 'Invalid origin'}
    for failure, status, message in [(ValueError('private'), 400, 'Invalid or expired authorization'), (OSError('private'), 502, 'Authorization failed')]:
        with patch('app.SESSIONS.complete', side_effect=failure):
            response, data = request(client, 'GET', '/oauth/linear/callback?code=private-code&state=state', headers={'Host': '127.0.0.1:8765'})
        assert response.status == status and json.loads(data) == {'error': message}
        assert b'private' not in data


def test_callback_rejects_multiple_or_missing_parameters(client):
    for query in ('?code=a&code=b&state=s', '?code=&state=s', '?code=c&state=', '?code=c&state=s&state=t'):
        response, data = request(client, 'GET', '/oauth/linear/callback' + query, headers={'Host': '127.0.0.1:8765'})
        assert response.status == 400 and json.loads(data) == {'error': 'Invalid callback'}


def test_linear_disconnect_endpoint(client):
    headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765', 'Cookie': 'team_context_session=safe-cookie'}
    with patch('app.SESSIONS.disconnect') as disconnect:
        response, data = request(client, 'POST', '/api/linear/disconnect', '', headers)
        assert response.status == 200 and json.loads(data) == {'connected': False}
        disconnect.assert_called_once_with('safe-cookie')
    for failure, status, message in [(ValueError('private'), 404, 'No connection'), (OSError('private'), 502, 'Disconnect failed')]:
        with patch('app.SESSIONS.disconnect', side_effect=failure):
            response, data = request(client, 'POST', '/api/linear/disconnect', '', headers)
        assert response.status == status and json.loads(data) == {'error': message}
    response, data = request(client, 'POST', '/api/linear/disconnect', '', {'Host': '127.0.0.1:8765', 'Origin': 'https://evil.example'})
    assert response.status == 403 and json.loads(data) == {'error': 'Invalid origin'}


def test_linear_cannot_replace_existing_connection(client):
    with patch('app.SESSIONS.connected', return_value=True), patch('app.SESSIONS.start') as start:
        response, data = request(client, 'POST', '/api/linear/start', '', {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'})
    assert response.status == 409 and json.loads(data) == {'error': 'Disconnect the existing connection first.'}
    start.assert_not_called()


def test_search_http_api(client):
    records = [{'title': 'one', 'url': 'https://example.com', 'visibility': 'public'}]
    with patch('app.search', return_value=records) as search:
        response, body = request(client, 'GET', '/api/search?q=team%20context')
    assert response.status == 200 and json.loads(body) == {'records': records}
    search.assert_called_once_with('team context')
    with patch('app.search', side_effect=ValueError('bad query')):
        response, body = request(client, 'GET', '/api/search?q=x')
    assert response.status == 400 and json.loads(body) == {'error': 'bad query'}
    for error in (OSError('private error'), KeyError('private error'), TypeError('private error')):
        with patch('app.search', side_effect=error):
            response, body = request(client, 'GET', '/api/search?q=word')
        assert response.status == 502 and json.loads(body) == {'error': 'Unable to search local storage.'}


def test_linear_connection_store_failure_returns_safe_error(client):
    for method, path, headers in [
        ('GET', '/api/linear/status', {}),
        ('POST', '/api/linear/start', {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'}),
    ]:
        with patch('app.SESSIONS.connected', side_effect=OSError('secret-details')):
            response, data = request(client, method, path, '' if method == 'POST' else None, headers)
        assert response.status == 502
        assert json.loads(data) == {'error': 'Unable to read connection status'}
        assert b'secret-details' not in data


def test_linear_preview_requires_local_host_and_session(client):
    from unittest.mock import Mock
    headers = {'Host': '127.0.0.1:8765', 'Cookie': 'team_context_session=valid'}
    with patch('app.SESSIONS.use', return_value=[{'id': 'id', 'name': 'team'}]) as use:
        response, body = request(client, 'GET', '/api/linear/teams', headers=headers)
        assert response.status == 200 and json.loads(body) == {'items': [{'id': 'id', 'name': 'team'}]}
        assert response.getheader('Cache-Control') == 'no-store'
        assert use.call_args.args == ('valid', app.linear_preview.teams)
    response, _ = request(client, 'GET', '/api/linear/teams', headers={'Host': 'evil.example'})
    assert response.status == 403
    for error, status, message in [(ValueError('private'), 400, 'Linear preview unavailable'), (OSError('private'), 502, 'Unable to load Linear data'), (KeyError('private'), 502, 'Unable to load Linear data')]:
        with patch('app.SESSIONS.use', side_effect=error):
            response, body = request(client, 'GET', '/api/linear/teams', headers=headers)
        assert response.status == status and json.loads(body) == {'error': message} and b'private' not in body


def test_linear_preview_passes_selected_team_and_session(client):
    headers = {'Host': '127.0.0.1:8765', 'Cookie': 'team_context_session=valid'}
    def use(session, operation):
        assert session == 'valid'
        return operation('short-lived')
    with patch('app.SESSIONS.use', side_effect=use), patch('app.linear_preview.issues', return_value=[{'title': 'sample'}]) as issues:
        response, body = request(client, 'GET', '/api/linear/issues?team=selected-team', headers=headers)
    assert response.status == 200 and json.loads(body) == {'items': [{'title': 'sample'}]}
    issues.assert_called_once_with('short-lived', 'selected-team')


def test_remove_source_requires_allowed_origin_and_validated_source(client):
    body = json.dumps({'connector': 'github', 'config': {'repository': 'A/B'}})
    with patch('app.remove_public_source', return_value=2) as remove:
        response, data = request(client, 'POST', '/api/remove-source', body, {'Origin': 'http://127.0.0.1:8765'})
    assert response.status == 200 and json.loads(data) == {'removed': 2}
    remove.assert_called_once_with('github:a/b')
    response, _ = request(client, 'POST', '/api/remove-source', body, {'Origin': 'https://evil.example'})
    assert response.status == 403
    with patch('app.remove_public_source', side_effect=OSError('failed')):
        response, _ = request(client, 'POST', '/api/remove-source', body)
    assert response.status == 502


def test_collection_and_removal_do_not_race(client):
    import time
    entered = threading.Event()
    release = threading.Event()
    removal_started = threading.Event()
    body = json.dumps({'connector': 'github', 'config': {'repository': 'a/b'}})
    def collect(config):
        entered.set()
        assert release.wait(3)
        return []
    def remove(connection):
        removal_started.set()
        return 0
    second = http.client.HTTPConnection(client.host, client.port, timeout=5)
    with patch('connectors.github.collect', side_effect=collect), patch('app.persist'), patch('app.remove_public_source', side_effect=remove):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(request, client, 'POST', '/api/collect', body)
            assert entered.wait(2)
            other = pool.submit(request, second, 'POST', '/api/remove-source', body)
            time.sleep(0.05)
            assert not removal_started.is_set()
            release.set()
            assert first.result()[0].status == 200
            assert other.result()[0].status == 200
            assert removal_started.is_set()
    second.close()


def test_cleanup_requires_local_origin_and_reports_failures(client):
    headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'}
    with patch('app.cleanup_unavailable', return_value=2) as cleanup:
        response, data = request(client, 'POST', '/api/cleanup-unavailable', '', headers)
    assert response.status == 200 and json.loads(data) == {'removed': 2}
    cleanup.assert_called_once_with()
    for bad in ({'Host': 'evil.example', 'Origin': 'http://127.0.0.1:8765'}, {'Host': '127.0.0.1:8765', 'Origin': 'https://evil.example'}, {'Host': '127.0.0.1:8765'}):
        response, data = request(client, 'POST', '/api/cleanup-unavailable', '', bad)
        assert response.status == 403 and json.loads(data) == {'error': 'Invalid origin'}
    for error in (OSError('secret'), ValueError('secret'), KeyError('secret'), TypeError('secret')):
        with patch('app.cleanup_unavailable', side_effect=error):
            response, data = request(client, 'POST', '/api/cleanup-unavailable', '', headers)
        assert response.status == 502 and json.loads(data) == {'error': 'Unable to verify or clean local records'}
        assert b'secret' not in data


def test_source_list_local_host_and_failure(client):
    sources = [{'connector': 'github', 'source': 'a/b', 'count': 2}]
    with patch('app.list_public_sources', return_value=sources):
        response, data = request(client, 'GET', '/api/sources', headers={'Host': '127.0.0.1:8765'})
    assert response.status == 200 and json.loads(data) == {'sources': sources}
    assert response.getheader('Cache-Control') == 'no-store'
    response, data = request(client, 'GET', '/api/sources', headers={'Host': 'evil.example'})
    assert response.status == 403 and json.loads(data) == {'error': 'Invalid host'}
    with patch('app.list_public_sources', side_effect=OSError('secret')):
        response, data = request(client, 'GET', '/api/sources', headers={'Host': '127.0.0.1:8765'})
    assert response.status == 502 and json.loads(data) == {'error': 'Unable to list local sources'} and b'secret' not in data


def test_teams_oauth_and_preview_routes(client):
    host = {'Host': '127.0.0.1:8765'}
    post = {**host, 'Origin': 'http://127.0.0.1:8765'}
    with patch('app.TEAMS_SESSIONS') as sessions, patch.dict('os.environ', {'TEAMS_CLIENT_ID': 'client'}):
        sessions.connected.return_value = False
        sessions.start.return_value = ('cookie-value', 'https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize')
        response, data = request(client, 'POST', '/api/teams/start', headers=post)
        assert response.status == 200 and json.loads(data)['url'].startswith('https://login.microsoftonline.com/')
        assert response.getheader('Set-Cookie') == 'team_context_teams_session=cookie-value; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000'
        assert response.getheader('Cache-Control') == 'no-store'
        sessions.start.assert_called_once_with('client', app.TEAMS_CALLBACK)
        cookie = {**host, 'Cookie': 'team_context_teams_session=cookie-value; team_context_session=linear'}
        response, _ = request(client, 'GET', '/oauth/teams/callback?state=s&code=c', headers=cookie)
        assert response.status == 303 and response.getheader('Location') == '/'
        assert response.getheader('Cache-Control') == 'no-store' and response.getheader('Content-Length') == '0'
        sessions.complete.assert_called_once_with('cookie-value', 's', 'c')
        sessions.connected.return_value = True
        response, data = request(client, 'GET', '/api/teams/status', headers=cookie)
        assert response.status == 200 and json.loads(data) == {'connected': True}
        sessions.use.return_value = [{'id': '1', 'name': 'team'}]
        response, data = request(client, 'GET', '/api/teams/list', headers=cookie)
        assert response.status == 200 and json.loads(data) == {'items': [{'id': '1', 'name': 'team'}]}
        sessions.use.assert_called_with('cookie-value', app.teams_preview.teams)
        response, data = request(client, 'GET', '/api/teams/channels?team=abc', headers=cookie)
        assert response.status == 200 and json.loads(data)['items'][0]['name'] == 'team'
        operation = sessions.use.call_args.args[1]
        with patch('app.teams_preview.channels', return_value=[]) as channels:
            assert operation('token') == []
            channels.assert_called_once_with('token', 'abc')
        response, data = request(client, 'POST', '/api/teams/start', headers={**post, **cookie})
        assert response.status == 409 and json.loads(data) == {'error': 'Disconnect the existing connection first.'}
        response, data = request(client, 'POST', '/api/teams/disconnect', headers={**post, **cookie})
        assert response.status == 200 and json.loads(data) == {'connected': False}
        sessions.disconnect.assert_called_once_with('cookie-value')


def test_teams_rejects_invalid_requests_and_reports_failures(client):
    host = {'Host': '127.0.0.1:8765'}
    post = {**host, 'Origin': 'http://127.0.0.1:8765'}
    for path in ('/oauth/teams/callback?state=s&code=c', '/api/teams/status', '/api/teams/list', '/api/teams/channels'):
        response, data = request(client, 'GET', path, headers={'Host': 'evil.example'})
        assert response.status == 403 and json.loads(data) == {'error': 'Invalid host'}
    for path in ('/api/teams/start', '/api/teams/disconnect'):
        response, data = request(client, 'POST', path, headers={**host, 'Origin': 'https://evil.example'})
        assert response.status == 403 and json.loads(data) == {'error': 'Invalid origin'}
    for query in ('', '?state=s', '?state=s&state=t&code=c', '?error=access_denied&state=s'):
        response, data = request(client, 'GET', '/oauth/teams/callback' + query, headers=host)
        assert response.status == 400 and json.loads(data) == {'error': 'Invalid callback'}
    with patch('app.TEAMS_SESSIONS') as sessions:
        sessions.complete.side_effect = ValueError('private details')
        response, data = request(client, 'GET', '/oauth/teams/callback?state=s&code=c', headers=host)
        assert response.status == 400 and json.loads(data) == {'error': 'Invalid or expired authorization'}
        sessions.complete.side_effect = OSError('private details')
        response, data = request(client, 'GET', '/oauth/teams/callback?state=s&code=c', headers=host)
        assert response.status == 502 and json.loads(data) == {'error': 'Authorization failed'}
        sessions.connected.return_value = False
        with patch.dict('os.environ', {}, clear=True):
            response, data = request(client, 'POST', '/api/teams/start', headers=post)
            assert response.status == 503 and json.loads(data) == {'error': 'Set TEAMS_CLIENT_ID to your Entra app client ID.'}
        sessions.disconnect.side_effect = ValueError('private details')
        response, data = request(client, 'POST', '/api/teams/disconnect', headers=post)
        assert response.status == 404 and json.loads(data) == {'error': 'No connection'}
        sessions.disconnect.side_effect = OSError('private details')
        response, data = request(client, 'POST', '/api/teams/disconnect', headers=post)
        assert response.status == 502 and json.loads(data) == {'error': 'Unable to manage Teams connection'}
        sessions.use.side_effect = ValueError('private details')
        response, data = request(client, 'GET', '/api/teams/list', headers=host)
        assert response.status == 400 and json.loads(data) == {'error': 'Teams preview unavailable'}
        sessions.use.side_effect = OSError('private details')
        response, data = request(client, 'GET', '/api/teams/channels', headers=host)
        assert response.status == 502 and json.loads(data) == {'error': 'Unable to load Teams data'}


def test_gitlab_oauth_and_private_preview_routes(client):
    host = {'Host':'127.0.0.1:8765'}
    post = {**host, 'Origin':'http://127.0.0.1:8765'}
    cookie = {**host, 'Cookie':'team_context_gitlab_session=gitlab-cookie; team_context_session=linear'}
    with patch('app.GITLAB_SESSIONS') as sessions, patch.dict('os.environ', {'GITLAB_CLIENT_ID':'client'}):
        sessions.connected.return_value = False
        sessions.start.return_value = ('gitlab-cookie', 'https://gitlab.com/oauth/authorize')
        response, data = request(client, 'POST', '/api/gitlab/start', headers=post)
        assert response.status == 200 and json.loads(data) == {'url':'https://gitlab.com/oauth/authorize'}
        assert response.getheader('Set-Cookie') == 'team_context_gitlab_session=gitlab-cookie; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000'
        assert response.getheader('Cache-Control') == 'no-store'
        sessions.start.assert_called_once_with('client', app.gitlab.CALLBACK)
        response, _ = request(client, 'GET', '/oauth/gitlab/callback?state=s&code=c', headers=cookie)
        assert response.status == 303 and response.getheader('Location') == '/'
        assert response.getheader('Cache-Control') == 'no-store' and response.getheader('Content-Length') == '0'
        sessions.complete.assert_called_once_with('gitlab-cookie', 's', 'c')
        sessions.connected.return_value = True
        response, data = request(client, 'GET', '/api/gitlab/status', headers=cookie)
        assert response.status == 200 and json.loads(data) == {'connected': True}
        sessions.use.return_value = [{'title':'private','url':'https://gitlab.com/a/b'}]
        response, data = request(client, 'GET', '/api/gitlab/preview?project=a%2Fb', headers=cookie)
        assert response.status == 200 and json.loads(data) == {'items':[{'title':'private','url':'https://gitlab.com/a/b'}]}
        assert response.getheader('Cache-Control') == 'no-store'
        operation = sessions.use.call_args.args[1]
        assert sessions.use.call_args.args[0] == 'gitlab-cookie'
        with patch('app.gitlab_preview.merge_requests', return_value=[]) as preview:
            assert operation('token') == []
            preview.assert_called_once_with('token', 'a/b')
        response, data = request(client, 'POST', '/api/gitlab/start', headers={**post, **cookie})
        assert response.status == 409 and json.loads(data) == {'error':'Disconnect the existing connection first.'}
        response, data = request(client, 'POST', '/api/gitlab/disconnect', headers={**post, **cookie})
        assert response.status == 200 and json.loads(data) == {'connected':False}
        sessions.disconnect.assert_called_once_with('gitlab-cookie')


def test_gitlab_oauth_rejects_invalid_requests_and_hides_errors(client):
    host = {'Host':'127.0.0.1:8765'}
    post = {**host, 'Origin':'http://127.0.0.1:8765'}
    for path in ('/oauth/gitlab/callback?state=s&code=c', '/api/gitlab/status', '/api/gitlab/preview'):
        response, data = request(client, 'GET', path, headers={'Host':'evil.example'})
        assert response.status == 403 and json.loads(data) == {'error':'Invalid host'}
    for path in ('/api/gitlab/start', '/api/gitlab/disconnect'):
        response, data = request(client, 'POST', path, headers={**host, 'Origin':'https://evil.example'})
        assert response.status == 403 and json.loads(data) == {'error':'Invalid origin'}
    for query in ('', '?state=s', '?state=s&state=t&code=c', '?error=access_denied&state=s'):
        response, data = request(client, 'GET', '/oauth/gitlab/callback' + query, headers=host)
        assert response.status == 400 and json.loads(data) == {'error':'Invalid callback'}
    with patch('app.GITLAB_SESSIONS') as sessions:
        sessions.complete.side_effect = ValueError('private')
        response, data = request(client, 'GET', '/oauth/gitlab/callback?state=s&code=c', headers=host)
        assert response.status == 400 and json.loads(data) == {'error':'Invalid or expired authorization'}
        sessions.complete.side_effect = OSError('private')
        response, data = request(client, 'GET', '/oauth/gitlab/callback?state=s&code=c', headers=host)
        assert response.status == 502 and json.loads(data) == {'error':'Authorization failed'}
        sessions.connected.return_value = False
        with patch.dict('os.environ', {}, clear=True):
            response, data = request(client, 'POST', '/api/gitlab/start', headers=post)
            assert response.status == 503 and json.loads(data) == {'error':'Set GITLAB_CLIENT_ID to your GitLab OAuth app ID.'}
        sessions.disconnect.side_effect = ValueError('private')
        response, data = request(client, 'POST', '/api/gitlab/disconnect', headers=post)
        assert response.status == 404 and json.loads(data) == {'error':'No connection'}
        sessions.disconnect.side_effect = OSError('private')
        response, data = request(client, 'POST', '/api/gitlab/disconnect', headers=post)
        assert response.status == 502 and json.loads(data) == {'error':'Unable to manage GitLab connection'}
        sessions.use.side_effect = ValueError('private')
        response, data = request(client, 'GET', '/api/gitlab/preview', headers=host)
        assert response.status == 400 and json.loads(data) == {'error':'GitLab preview unavailable'}
        sessions.use.side_effect = OSError('private')
        response, data = request(client, 'GET', '/api/gitlab/preview', headers=host)
        assert response.status == 502 and json.loads(data) == {'error':'Unable to load GitLab data'}


def test_gitlab_preview_never_persists_private_data(client):
    headers = {'Host':'127.0.0.1:8765', 'Cookie':'team_context_gitlab_session=gitlab-cookie'}
    with patch('app.GITLAB_SESSIONS.use', return_value=[{'title':'private','url':'https://gitlab.com/a/b'}]), patch('app.persist') as persist, patch('app.search') as search:
        response, data = request(client, 'GET', '/api/gitlab/preview?project=a%2Fb', headers=headers)
    assert response.status == 200 and json.loads(data)['items'][0]['title'] == 'private'
    persist.assert_not_called()
    search.assert_not_called()


def test_github_oauth_and_private_preview_routes(client):
    host = {'Host':'127.0.0.1:8765'}
    post = {**host, 'Origin':'http://127.0.0.1:8765'}
    cookie = {**host, 'Cookie':'team_context_github_session=github-cookie; team_context_session=linear'}
    with patch('app.GITHUB_SESSIONS') as sessions, patch.dict('os.environ', {'GITHUB_CLIENT_ID':'client', 'GITHUB_APP_SECRET_ID':'secret-id'}):
        sessions.connected.return_value = False
        sessions.start.return_value = ('github-cookie', 'https://github.com/login/oauth/authorize')
        response, data = request(client, 'POST', '/api/github/start', headers=post)
        assert response.status == 200 and json.loads(data) == {'url':'https://github.com/login/oauth/authorize'}
        assert response.getheader('Set-Cookie') == 'team_context_github_session=github-cookie; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000'
        assert response.getheader('Cache-Control') == 'no-store'
        sessions.start.assert_called_once_with('client', app.github.CALLBACK)
        response, _ = request(client, 'GET', '/oauth/github/callback?state=s&code=c', headers=cookie)
        assert response.status == 303 and response.getheader('Location') == '/'
        assert response.getheader('Cache-Control') == 'no-store' and response.getheader('Content-Length') == '0'
        sessions.complete.assert_called_once_with('github-cookie', 's', 'c')
        sessions.connected.return_value = True
        response, data = request(client, 'GET', '/api/github/status', headers=cookie)
        assert response.status == 200 and json.loads(data) == {'connected': True}
        sessions.use.return_value = [{'title':'private','url':'https://github.com/a/b'}]
        response, data = request(client, 'GET', '/api/github/preview?repository=a%2Fb', headers=cookie)
        assert response.status == 200 and json.loads(data) == {'items':[{'title':'private','url':'https://github.com/a/b'}]}
        assert response.getheader('Cache-Control') == 'no-store'
        operation = sessions.use.call_args.args[1]
        assert sessions.use.call_args.args[0] == 'github-cookie'
        with patch('app.github_preview.pull_requests', return_value=[]) as preview:
            assert operation('token') == []
            preview.assert_called_once_with('token', 'a/b')
        response, data = request(client, 'POST', '/api/github/start', headers={**post, **cookie})
        assert response.status == 409 and json.loads(data) == {'error':'Disconnect the existing connection first.'}
        response, data = request(client, 'POST', '/api/github/disconnect', headers={**post, **cookie})
        assert response.status == 200 and json.loads(data) == {'connected':False}
        sessions.disconnect.assert_called_once_with('github-cookie')


def test_github_oauth_rejects_invalid_requests_and_hides_errors(client):
    host = {'Host':'127.0.0.1:8765'}
    post = {**host, 'Origin':'http://127.0.0.1:8765'}
    for path in ('/oauth/github/callback?state=s&code=c', '/api/github/status', '/api/github/preview'):
        response, data = request(client, 'GET', path, headers={'Host':'evil.example'})
        assert response.status == 403 and json.loads(data) == {'error':'Invalid host'}
    for path in ('/api/github/start', '/api/github/disconnect'):
        response, data = request(client, 'POST', path, headers={**host, 'Origin':'https://evil.example'})
        assert response.status == 403 and json.loads(data) == {'error':'Invalid origin'}
    for query in ('', '?state=s', '?state=s&state=t&code=c', '?error=access_denied&state=s'):
        response, data = request(client, 'GET', '/oauth/github/callback' + query, headers=host)
        assert response.status == 400 and json.loads(data) == {'error':'Invalid callback'}
    with patch('app.GITHUB_SESSIONS') as sessions:
        sessions.complete.side_effect = ValueError('private')
        response, data = request(client, 'GET', '/oauth/github/callback?state=s&code=c', headers=host)
        assert response.status == 400 and json.loads(data) == {'error':'Invalid or expired authorization'}
        sessions.complete.side_effect = OSError('private')
        response, data = request(client, 'GET', '/oauth/github/callback?state=s&code=c', headers=host)
        assert response.status == 502 and json.loads(data) == {'error':'Authorization failed'}
        sessions.connected.return_value = False
        with patch.dict('os.environ', {}, clear=True):
            response, data = request(client, 'POST', '/api/github/start', headers=post)
            assert response.status == 503 and json.loads(data) == {'error':'Set GITHUB_CLIENT_ID and GITHUB_APP_SECRET_ID.'}
        sessions.disconnect.side_effect = ValueError('private')
        response, data = request(client, 'POST', '/api/github/disconnect', headers=post)
        assert response.status == 404 and json.loads(data) == {'error':'No connection'}
        sessions.disconnect.side_effect = OSError('private')
        response, data = request(client, 'POST', '/api/github/disconnect', headers=post)
        assert response.status == 502 and json.loads(data) == {'error':'Unable to manage GitHub connection'}
        sessions.use.side_effect = ValueError('private')
        response, data = request(client, 'GET', '/api/github/preview', headers=host)
        assert response.status == 400 and json.loads(data) == {'error':'GitHub preview unavailable'}
        sessions.use.side_effect = OSError('private')
        response, data = request(client, 'GET', '/api/github/preview', headers=host)
        assert response.status == 502 and json.loads(data) == {'error':'Unable to load GitHub data'}


def test_github_preview_never_persists_private_data(client):
    headers = {'Host':'127.0.0.1:8765', 'Cookie':'team_context_github_session=github-cookie'}
    with patch('app.GITHUB_SESSIONS.use', return_value=[{'title':'private','url':'https://github.com/a/b'}]), patch('app.persist') as persist, patch('app.search') as search:
        response, data = request(client, 'GET', '/api/github/preview?repository=a%2Fb', headers=headers)
    assert response.status == 200 and json.loads(data)['items'][0]['title'] == 'private'
    persist.assert_not_called()
    search.assert_not_called()


def test_github_start_requires_both_app_settings(client):
    headers = {'Host':'127.0.0.1:8765', 'Origin':'http://127.0.0.1:8765'}
    for config in ({'GITHUB_CLIENT_ID':'id'}, {'GITHUB_APP_SECRET_ID':'ref'}):
        with patch.dict('os.environ', config, clear=True), patch('app.GITHUB_SESSIONS') as sessions:
            sessions.connected.return_value = False
            response, data = request(client, 'POST', '/api/github/start', headers=headers)
            assert response.status == 503 and json.loads(data) == {'error':'Set GITHUB_CLIENT_ID and GITHUB_APP_SECRET_ID.'}
            sessions.start.assert_not_called()


def test_private_gitlab_collection_and_search_are_session_bound(client):
    host={'Host':'127.0.0.1:8765', 'Cookie':'team_context_gitlab_session=gitlab-cookie'}
    post={**host, 'Origin':'http://127.0.0.1:8765'}
    with patch('app.GITLAB_SESSIONS.use') as use, patch('app.gitlab_private.collect', return_value=2) as collect, patch('app.gitlab_private.search', return_value=[{'title':'secret'}]) as search:
        use.side_effect=lambda session, operation: operation('viewer-token')
        response,data=request(client,'POST','/api/gitlab/private/collect',json.dumps({'project':'a/b'}),post)
        assert response.status == 200 and json.loads(data) == {'count':2}
        assert response.getheader('Cache-Control') == 'no-store'
        collect.assert_called_once_with('viewer-token','a/b')
        assert use.call_args.args[0] == 'gitlab-cookie'
        response,data=request(client,'GET','/api/gitlab/private/search?project=a%2Fb&q=secret',headers=host)
        assert response.status == 200 and json.loads(data) == {'items':[{'title':'secret'}]}
        assert response.getheader('Cache-Control') == 'no-store'
        search.assert_called_once_with('viewer-token','a/b','secret')
        assert use.call_args.args[0] == 'gitlab-cookie'


def test_private_gitlab_rejects_bad_request_and_hides_errors(client):
    host={'Host':'127.0.0.1:8765'}
    post={**host, 'Origin':'http://127.0.0.1:8765'}
    for method,path,headers in [('GET','/api/gitlab/private/search',{'Host':'evil.example'}),('POST','/api/gitlab/private/collect',{**host,'Origin':'https://evil.example'})]:
        response,data=request(client,method,path,headers=headers)
        assert response.status == 403 and 'error' in json.loads(data)
    for body in ('', '{', '[]', '{}', json.dumps({'project':1}), 'x'*4097):
        response,data=request(client,'POST','/api/gitlab/private/collect',body,post)
        assert response.status == 400 and json.loads(data) == {'error':'Private collection unavailable'}
    with patch('app.GITLAB_SESSIONS.use') as use:
        for error,code,message in [(ValueError('secret'),400,'Private collection unavailable'),(OSError('secret'),502,'Unable to collect private data')]:
            use.side_effect=error
            response,data=request(client,'POST','/api/gitlab/private/collect',json.dumps({'project':'a/b'}),post)
            assert response.status == code and json.loads(data) == {'error':message}
        for error,code,message in [(ValueError('secret'),400,'Private search unavailable'),(OSError('secret'),502,'Unable to search private data')]:
            use.side_effect=error
            response,data=request(client,'GET','/api/gitlab/private/search?project=a%2Fb&q=secret',headers=host)
            assert response.status == code and json.loads(data) == {'error':message}


@pytest.mark.parametrize('provider,session_name,client_env,extra_env,origin', [
    ('linear', 'SESSIONS', 'LINEAR_CLIENT_ID', {}, 'https://linear.app'),
    ('teams', 'TEAMS_SESSIONS', 'TEAMS_CLIENT_ID', {}, 'https://login.microsoftonline.com'),
    ('gitlab', 'GITLAB_SESSIONS', 'GITLAB_CLIENT_ID', {}, 'https://gitlab.com'),
    ('github', 'GITHUB_SESSIONS', 'GITHUB_CLIENT_ID', {'GITHUB_APP_SECRET_ID': 'fake'}, 'https://github.com'),
])
def test_oauth_provider_rejects_untrusted_authorization_destination(client, provider, session_name, client_env, extra_env, origin):
    headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'}
    with patch('app.' + session_name) as sessions, patch.dict('os.environ', {client_env: 'fake', **extra_env}):
        sessions.connected.return_value = False
        for url in ('https://attacker.example', 'http://' + origin.split('//')[1], origin + '@attacker.example'):
            sessions.start.return_value = ('session', url)
            response, data = request(client, 'POST', f'/api/{provider}/start', headers=headers)
            assert response.status == 404 and b'attacker' not in data
            assert response.getheader('Set-Cookie') is None


def test_callback_rejects_another_providers_cookie(client):
    from auth.sessions import Sessions
    sessions = Sessions(app.github.Authorization, app.github.refresh, 'github')
    headers = {'Host': '127.0.0.1:8765', 'Cookie': 'team_context_gitlab_session=' + 'a' * 43}
    with patch('app.GITHUB_SESSIONS', sessions), patch('app.GITLAB_SESSIONS.complete') as gitlab_complete:
        response, data = request(client, 'GET', '/oauth/github/callback?state=s&code=c', headers=headers)
    assert response.status == 400 and json.loads(data) == {'error': 'Invalid or expired authorization'}
    gitlab_complete.assert_not_called()


def test_registered_provider_uses_shared_oauth_routes(client):
    from dataclasses import replace
    from auth.providers import PROVIDERS, register
    from unittest.mock import Mock
    provider = replace(PROVIDERS['gitlab'], id='example', cookie='team_context_example_session',
                       namespace='example', session_name='EXAMPLE_SESSIONS', display_name='Example',
                       client_env='EXAMPLE_CLIENT_ID', callback='http://127.0.0.1:8765/oauth/example/callback',
                       authorize_origin='https://example.com')
    sessions = Mock()
    sessions.connected.return_value = False
    sessions.start.return_value = ('safe-session', 'https://example.com/authorize')
    headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'}
    with patch('app.PROVIDERS', register([*PROVIDERS.values(), provider])), patch('app.EXAMPLE_SESSIONS', sessions, create=True), patch.dict('os.environ', {'EXAMPLE_CLIENT_ID': 'fake'}):
        response, data = request(client, 'POST', '/api/example/start', headers=headers)
        assert response.status == 200 and json.loads(data) == {'url': 'https://example.com/authorize'}
        cookie = response.getheader('Set-Cookie')
        assert cookie.startswith('team_context_example_session=safe-session;')
        response, _ = request(client, 'GET', '/oauth/example/callback?state=state&code=code', headers={**headers, 'Cookie': cookie})
        assert response.status == 303
        sessions.complete.assert_called_once_with('safe-session', 'state', 'code')
        response, data = request(client, 'GET', '/api/example/status', headers={**headers, 'Cookie': cookie})
        assert response.status == 200 and json.loads(data) == {'connected': False}
        response, _ = request(client, 'POST', '/api/example/disconnect', headers={**headers, 'Cookie': cookie})
        assert response.status == 200
        sessions.disconnect.assert_called_once_with('safe-session')


@pytest.mark.parametrize('provider,session_name,cookie_name,client_env,extra_env', [
    ('linear', 'SESSIONS', 'team_context_session', 'LINEAR_CLIENT_ID', {}),
    ('teams', 'TEAMS_SESSIONS', 'team_context_teams_session', 'TEAMS_CLIENT_ID', {}),
    ('gitlab', 'GITLAB_SESSIONS', 'team_context_gitlab_session', 'GITLAB_CLIENT_ID', {}),
    ('github', 'GITHUB_SESSIONS', 'team_context_github_session', 'GITHUB_CLIENT_ID', {'GITHUB_APP_SECRET_ID': 'fake'}),
])
def test_start_checks_the_current_providers_session_before_replacement(client, provider, session_name, cookie_name, client_env, extra_env):
    headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765', 'Cookie': cookie_name + '=existing'}
    with patch('app.' + session_name) as sessions, patch.dict('os.environ', {client_env: 'fake', **extra_env}):
        sessions.connected.side_effect = lambda cookie: cookie == 'existing'
        response, data = request(client, 'POST', f'/api/{provider}/start', headers=headers)
        assert response.status == 409 and json.loads(data) == {'error': 'Disconnect the existing connection first.'}
        sessions.connected.assert_called_once_with('existing')
        sessions.start.assert_not_called()


def test_namespaced_status_allows_query_but_linear_preserves_exact_path(client):
    with patch('app.GITHUB_SESSIONS.connected', return_value=True):
        response, data = request(client, 'GET', '/api/github/status?unused=1', headers={'Host': '127.0.0.1:8765'})
        assert response.status == 200 and json.loads(data) == {'connected': True}
    response, _ = request(client, 'GET', '/api/linear/status?unused=1')
    assert response.status == 404


def test_atlassian_site_preview_is_session_bound_and_not_persisted(client):
    from unittest.mock import Mock
    sessions = Mock()
    sessions.use.return_value = [{'id':'site', 'name':'Team', 'jira':True, 'confluence':False}]
    with patch('app.ATLASSIAN_SESSIONS', sessions), patch('app.persist') as persist:
        response, data = request(client, 'GET', '/api/atlassian/sites', headers={'Host':'127.0.0.1:8765', 'Cookie':'team_context_atlassian_session=own'})
        assert response.status == 200 and json.loads(data)['items'][0]['name'] == 'Team'
        sessions.use.assert_called_once_with('own', app.atlassian_preview.sites)
        persist.assert_not_called()
        response, _ = request(client, 'GET', '/api/atlassian/sites', headers={'Host':'evil.example'})
        assert response.status == 403
        sessions.use.side_effect = ValueError('private')
        response, data = request(client, 'GET', '/api/atlassian/sites', headers={'Host':'127.0.0.1:8765'})
        assert response.status == 400 and b'private' not in data
        sessions.use.side_effect = OSError('private')
        response, data = request(client, 'GET', '/api/atlassian/sites', headers={'Host':'127.0.0.1:8765'})
        assert response.status == 502 and b'private' not in data


def test_atlassian_start_callback_status_disconnect_with_fake_credentials(client):
    from urllib.parse import parse_qs, urlsplit
    from auth.sessions import Sessions
    from auth import atlassian
    sessions = Sessions(atlassian.Authorization, atlassian.refresh, 'atlassian')
    headers = {'Host':'127.0.0.1:8765', 'Origin':'http://127.0.0.1:8765'}
    with patch('app.ATLASSIAN_SESSIONS', sessions), patch.dict('os.environ', {'ATLASSIAN_CLIENT_ID':'client','ATLASSIAN_APP_SECRET_ID':'fake'}), patch('auth.sessions.session_store.get', side_effect=[None, 'reference', 'reference']), patch('auth.sessions.session_store.put') as put, patch('auth.sessions.session_store.remove') as remove, patch('auth.sessions.vault.create', return_value='reference'), patch('auth.sessions.vault.delete'), patch('auth.atlassian._token', return_value={'access_token':'fake-access','refresh_token':'fake-refresh'}):
        response, body = request(client, 'POST', '/api/atlassian/start', headers=headers)
        assert response.status == 200
        cookie = response.getheader('Set-Cookie').split(';',1)[0]
        state = parse_qs(urlsplit(json.loads(body)['url']).query)['state'][0]
        response, _ = request(client, 'GET', '/oauth/atlassian/callback?code=fake&state=' + state, headers={**headers,'Cookie':cookie})
        assert response.status == 303
        assert put.call_args.kwargs == {'namespace':'atlassian'}
        response, body = request(client, 'GET', '/api/atlassian/status', headers={**headers,'Cookie':cookie})
        assert response.status == 200 and json.loads(body) == {'connected':True}
        response, body = request(client, 'POST', '/api/atlassian/disconnect', headers={**headers,'Cookie':cookie})
        assert response.status == 200 and json.loads(body) == {'connected':False}
        assert remove.call_args.kwargs == {'namespace':'atlassian'}


def test_atlassian_oauth_config_and_denial_fail_closed(client):
    headers = {'Host':'127.0.0.1:8765', 'Origin':'http://127.0.0.1:8765'}
    for config in ({}, {'ATLASSIAN_CLIENT_ID':'client'}, {'ATLASSIAN_APP_SECRET_ID':'secret'}):
        with patch.dict('os.environ', config, clear=True), patch('app.ATLASSIAN_SESSIONS') as sessions:
            sessions.connected.return_value = False
            response, data = request(client, 'POST', '/api/atlassian/start', headers=headers)
            assert response.status == 503 and json.loads(data) == {'error':'Set ATLASSIAN_CLIENT_ID and ATLASSIAN_APP_SECRET_ID.'}
            sessions.start.assert_not_called()
    with patch('app.ATLASSIAN_SESSIONS.connected', side_effect=OSError('secret')):
        response, data = request(client, 'GET', '/api/atlassian/status', headers=headers)
        assert response.status == 502 and json.loads(data) == {'error':'Unable to load Atlassian data'}
        assert b'secret' not in data
    for query in ('?error=access_denied&state=s','?code=x&state=s&state=t','?code=x&code=y&state=s'):
        with patch('app.ATLASSIAN_SESSIONS.complete') as complete:
            response, _ = request(client, 'GET', '/oauth/atlassian/callback' + query, headers=headers)
            assert response.status == 400
            complete.assert_not_called()


@pytest.mark.parametrize('provider,session_name,cookie_name', [
    ('atlassian','ATLASSIAN_SESSIONS','team_context_atlassian_session'),
    ('github','GITHUB_SESSIONS','team_context_github_session'),
    ('gitlab','GITLAB_SESSIONS','team_context_gitlab_session'),
    ('teams','TEAMS_SESSIONS','team_context_teams_session'),
    ('linear','SESSIONS','team_context_session'),
])
def test_status_uses_own_provider_cookie(client, provider, session_name, cookie_name):
    with patch('app.' + session_name + '.connected', side_effect=lambda value: value == 'own') as connected:
        response, data = request(client, 'GET', f'/api/{provider}/status', headers={'Host':'127.0.0.1:8765', 'Cookie':cookie_name + '=own'})
        assert response.status == 200 and json.loads(data) == {'connected':True}
        connected.assert_called_once_with('own')

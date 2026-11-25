import http.client
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
        assert cookie == 'team_context_session=safe-cookie; HttpOnly; SameSite=Lax; Path=/; Max-Age=600'
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
    with patch('app.SESSIONS', sessions), patch.dict('os.environ', {'LINEAR_CLIENT_ID': 'public-id'}), patch('auth.linear._token', return_value=tokens) as exchange, patch('auth.sessions.vault.create', return_value='private-secret-id') as store:
        response, body = request(client, 'POST', '/api/linear/start', '', headers)
        cookie = response.getheader('Set-Cookie').split(';', 1)[0]
        state = parse_qs(urlsplit(json.loads(body)['url']).query)['state'][0]
        response, body = request(client, 'GET', '/api/linear/status', headers={'Cookie': cookie})
        assert json.loads(body) == {'connected': False}
        response, body = request(client, 'GET', '/oauth/linear/callback?code=abc&state=' + state, headers={'Host': '127.0.0.1:8765', 'Cookie': cookie})
        assert response.status == 303 and body == b''
        assert all(secret not in str(response.getheaders()) for secret in ('private-access', 'private-refresh', 'private-secret-id'))
        store.assert_called_once_with(tokens)
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
    with patch('app.search', side_effect=OSError('private error')):
        response, body = request(client, 'GET', '/api/search?q=word')
    assert response.status == 502 and json.loads(body) == {'error': 'Unable to search local storage.'}

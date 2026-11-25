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

from unittest.mock import Mock, patch
from auth.transport import NoRedirect, open_url


def test_does_not_follow_redirect():
    assert NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://evil.example') is None


def test_credential_request_has_timeout_and_redirect_block():
    opener = Mock()
    request = Mock()
    with patch('auth.transport.urllib.request.build_opener', return_value=opener) as build:
        assert open_url(request) == opener.open.return_value
    build.assert_called_once_with(NoRedirect)
    opener.open.assert_called_once_with(request, timeout=15)


def test_redirect_does_not_forward_bearer_token():
    import threading
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import pytest
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get('Authorization')))
            self.send_response(302)
            self.send_header('Location', '/other')
            self.end_headers()
        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            self.send_response(200)
            self.end_headers()
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/start', data=b'body', headers={'Authorization': 'Bearer test-secret'})
        with pytest.raises(urllib.error.HTTPError): open_url(req)
        assert seen == [('/start', 'Bearer test-secret')]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()

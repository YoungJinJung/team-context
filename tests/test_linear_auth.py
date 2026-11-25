import base64
import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import pytest
from auth.linear import Authorization, refresh


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def test_authorization_url_and_pkce():
    auth = Authorization('client', 'http://localhost/callback')
    parsed = urlparse(auth.url())
    args = parse_qs(parsed.query)
    assert parsed.scheme == 'https' and parsed.netloc == 'linear.app' and parsed.path == '/oauth/authorize'
    assert args == {'client_id': ['client'], 'redirect_uri': ['http://localhost/callback'], 'response_type': ['code'], 'scope': ['read'], 'state': [auth._state], 'code_challenge_method': ['S256'], 'code_challenge': [base64.urlsafe_b64encode(hashlib.sha256(auth._verifier.encode()).digest()).rstrip(b'=').decode()]}
    assert Authorization('client', 'http://localhost/callback')._state != auth._state
    assert len(auth._verifier) >= 43


def test_exchange_and_replay():
    auth = Authorization('client', 'http://localhost/callback')
    with patch('auth.linear.urllib.request.urlopen', return_value=Response(b'{"access_token":"access","refresh_token":"refresh"}')) as urlopen:
        assert auth.exchange(auth._state, 'code') == {'access_token': 'access', 'refresh_token': 'refresh'}
    request = urlopen.call_args.args[0]
    assert request.full_url == 'https://api.linear.app/oauth/token'
    assert request.get_method() == 'POST'
    assert request.get_header('Content-type') == 'application/x-www-form-urlencoded'
    assert request.get_header('Accept') == 'application/json'
    assert parse_qs(request.data.decode()) == {'grant_type': ['authorization_code'], 'client_id': ['client'], 'redirect_uri': ['http://localhost/callback'], 'code': ['code'], 'code_verifier': [auth._verifier]}
    assert urlopen.call_args.kwargs == {'timeout': 15}
    with pytest.raises(ValueError): auth.exchange(auth._state, 'code')


def test_invalid_and_expired():
    auth = Authorization('client', 'http://localhost/callback')
    for state, code in [('wrong', 'code'), ('한글', 'code'), (None, 'code'), (auth._state, ''), (auth._state, None)]:
        with pytest.raises(ValueError, match='^Invalid or expired authorization$'): auth.exchange(state, code)
    with patch('auth.linear.time.monotonic', return_value=auth._expires):
        with pytest.raises(ValueError): auth.exchange(auth._state, 'code')


def test_concurrent_exchange_only_once():
    auth = Authorization('client', 'http://localhost/callback')
    barrier = threading.Barrier(2)
    def call():
        barrier.wait()
        try: return auth.exchange(auth._state, 'code')
        except ValueError: return None
    with patch('auth.linear._token', return_value={'access_token': 'ok'}):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: call(), range(2)))
    assert results.count(None) == 1
    assert results.count({'access_token': 'ok'}) == 1


def test_refresh():
    with patch('auth.linear.urllib.request.urlopen', return_value=Response(b'{"access_token":"new","refresh_token":"next"}')) as urlopen:
        assert refresh('client', 'old') == {'access_token': 'new', 'refresh_token': 'next'}
    assert parse_qs(urlopen.call_args.args[0].data.decode()) == {'grant_type': ['refresh_token'], 'client_id': ['client'], 'refresh_token': ['old']}


@pytest.mark.parametrize('payload', [{}, {'access_token': '', 'refresh_token': 'r'}, {'access_token': 'a', 'refresh_token': ''}, {'access_token': 1, 'refresh_token': 'r'}, {'access_token': 'a', 'refresh_token': 1}])
def test_invalid_token_response(payload):
    with patch('auth.linear.urllib.request.urlopen', return_value=Response(json.dumps(payload).encode())):
        with pytest.raises(ValueError, match='^Invalid token response$'): refresh('client', 'old')


def test_secure_random_inputs_and_expiry():
    with patch('auth.linear.secrets.token_urlsafe', side_effect=['state', 'verifier']) as random, patch('auth.linear.time.monotonic', return_value=100):
        auth = Authorization('client', 'http://localhost/callback')
    assert [call.args for call in random.call_args_list] == [(32,), (48,)]
    assert auth._expires == 400
    assert auth._used is False


def test_failed_exchange_cannot_be_replayed():
    auth = Authorization('client', 'http://localhost/callback')
    with patch('auth.linear._token', side_effect=OSError('network')):
        with pytest.raises(OSError): auth.exchange(auth._state, 'code')
    with pytest.raises(ValueError, match='^Invalid or expired authorization$'):
        auth.exchange(auth._state, 'code')

"""Linear authorization-code PKCE primitives; not a web session implementation."""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from auth.transport import open_url


class Authorization:
    """One-use authorization state. Callers must bind each instance to one user session."""

    def __init__(self, client_id, redirect_uri):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self._state = secrets.token_urlsafe(32)
        self._verifier = secrets.token_urlsafe(48)
        self._expires = time.monotonic() + 300
        self._lock = threading.Lock()
        self._used = False

    def url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code', 'scope': 'read', 'state': self._state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        return 'https://linear.app/oauth/authorize?' + urllib.parse.urlencode(params)

    def exchange(self, state, code):
        with self._lock:
            if self._used or time.monotonic() >= self._expires or not isinstance(state, str) or not state.isascii() or not secrets.compare_digest(self._state, state) or not isinstance(code, str) or not code:
                raise ValueError('Invalid or expired authorization')
            self._used = True
        return _token({'grant_type': 'authorization_code', 'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'code': code, 'code_verifier': self._verifier})


def refresh(client_id, refresh_token):
    return _token({'grant_type': 'refresh_token', 'client_id': client_id, 'refresh_token': refresh_token})


def _token(fields):
    request = urllib.request.Request('https://api.linear.app/oauth/token', data=urllib.parse.urlencode(fields).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, method='POST')
    with open_url(request) as response:
        tokens = json.load(response)
    if not isinstance(tokens.get('access_token'), str) or not tokens['access_token'] or not isinstance(tokens.get('refresh_token'), str) or not tokens['refresh_token']:
        raise ValueError('Invalid token response')
    return tokens

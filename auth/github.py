"""GitHub App OAuth authorization for the local private-repository preview."""
import base64
import hashlib
import json
import os
import urllib.parse
import urllib.request
from auth.linear import Authorization as BaseAuthorization
from auth.transport import open_url
from auth import vault

CALLBACK = 'http://127.0.0.1:8765/oauth/github/callback'


class Authorization(BaseAuthorization):
    def url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code', 'scope': 'offline_access', 'state': self._state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        return 'https://github.com/login/oauth/authorize?' + urllib.parse.urlencode(params)

    def _token(self, fields):
        return _token(fields)


def refresh(client_id, refresh_token):
    return _token({'client_id': client_id, 'refresh_token': refresh_token, 'grant_type': 'refresh_token'})


def _token(fields):
    secret = vault.read(os.environ.get('GITHUB_APP_SECRET_ID'))
    if not isinstance(secret, dict) or not isinstance(secret.get('client_secret'), str) or not secret['client_secret']:
        raise ValueError('Invalid GitHub app secret')
    request = urllib.request.Request('https://github.com/login/oauth/access_token', data=urllib.parse.urlencode({**fields, 'client_secret': secret['client_secret']}).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, method='POST')
    with open_url(request) as response:
        tokens = json.load(response)
    if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str) or not tokens['access_token'] or not isinstance(tokens.get('refresh_token'), str) or not tokens['refresh_token']:
        raise ValueError('Invalid token response')
    return tokens

"""GitLab.com OAuth authorization for the local private-repository preview."""
import base64
import hashlib
import json
import urllib.parse
import urllib.request
from auth.linear import Authorization as BaseAuthorization
from auth.transport import open_url

CALLBACK = 'http://127.0.0.1:8765/oauth/gitlab/callback'


class Authorization(BaseAuthorization):
    def url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code', 'scope': 'read_api', 'state': self._state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        return 'https://gitlab.com/oauth/authorize?' + urllib.parse.urlencode(params)

    def _token(self, fields):
        return _token(fields)


def refresh(client_id, refresh_token):
    return _token({'client_id': client_id, 'refresh_token': refresh_token, 'grant_type': 'refresh_token', 'redirect_uri': CALLBACK})


def _token(fields):
    request = urllib.request.Request('https://gitlab.com/oauth/token', data=urllib.parse.urlencode(fields).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, method='POST')
    with open_url(request) as response:
        tokens = json.load(response)
    if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str) or not tokens['access_token'] or not isinstance(tokens.get('refresh_token'), str) or not tokens['refresh_token']:
        raise ValueError('Invalid token response')
    return tokens

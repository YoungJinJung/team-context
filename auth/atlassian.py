"""Atlassian Cloud 3LO authorization for the local site preview."""
import json
import os
import urllib.parse
import urllib.request

from auth import vault
from auth.linear import Authorization as BaseAuthorization
from auth.transport import open_url

MAX_BYTES = 1_048_576

CALLBACK = 'http://127.0.0.1:8765/oauth/atlassian/callback'


class Authorization(BaseAuthorization):
    def url(self):
        params = {'audience': 'api.atlassian.com', 'client_id': self.client_id,
                  'scope': 'read:jira-work read:confluence-content.summary offline_access',
                  'redirect_uri': self.redirect_uri, 'state': self._state,
                  'response_type': 'code', 'prompt': 'consent'}
        return 'https://auth.atlassian.com/authorize?' + urllib.parse.urlencode(params)

    def _token(self, fields):
        # The documented 3LO exchange uses a client secret and does not document PKCE.
        return _token({key: value for key, value in fields.items() if key != 'code_verifier'})


def refresh(client_id, refresh_token):
    return _token({'grant_type': 'refresh_token', 'client_id': client_id, 'refresh_token': refresh_token})


def _token(fields):
    secret = vault.read(os.environ.get('ATLASSIAN_APP_SECRET_ID'))
    if not isinstance(secret, dict) or not isinstance(secret.get('client_secret'), str) or not secret['client_secret']:
        raise ValueError('Invalid Atlassian app secret')
    request = urllib.request.Request('https://auth.atlassian.com/oauth/token',
        data=json.dumps({**fields, 'client_secret': secret['client_secret']}).encode(),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
    with open_url(request) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('Token response too large')
    tokens = json.loads(body)
    if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str) or not tokens['access_token'] or not isinstance(tokens.get('refresh_token'), str) or not tokens['refresh_token']:
        raise ValueError('Invalid token response')
    return tokens

"""Microsoft Entra public-client authorization with PKCE for local Teams previews."""
import base64
import hashlib
import json
import urllib.parse
import urllib.request

from auth.linear import Authorization as LinearAuthorization
from auth.transport import open_url

BASE = 'https://login.microsoftonline.com/organizations/oauth2/v2.0'
SCOPES = 'offline_access https://graph.microsoft.com/Team.ReadBasic.All https://graph.microsoft.com/Channel.ReadBasic.All'


class Authorization(LinearAuthorization):
    def url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code', 'scope': SCOPES, 'state': self._state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        return BASE + '/authorize?' + urllib.parse.urlencode(params)

    def _token(self, fields):
        return _token(fields)


def refresh(client_id, refresh_token):
    return _token({'grant_type': 'refresh_token', 'client_id': client_id, 'refresh_token': refresh_token, 'scope': SCOPES})


def _token(fields):
    request = urllib.request.Request(BASE + '/token', data=urllib.parse.urlencode(fields).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, method='POST')
    with open_url(request) as response:
        tokens = json.load(response)
    if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str) or not tokens['access_token'] or not isinstance(tokens.get('refresh_token'), str) or not tokens['refresh_token']:
        raise ValueError('Invalid token response')
    return tokens

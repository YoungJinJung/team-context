"""Complete a verified account link without exposing provider credentials."""
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

from auth.coa_identity import Principal, _nonempty
from auth.link_attempts import ConsumedAttempt
from auth.transport import open_url

MAX_RESPONSE = 65536


def _valid_secret(value):
    return isinstance(value, str) and 1 <= len(value) <= 16384 and value.isascii() and all(33 <= ord(c) <= 126 for c in value)


def _secret_name(prefix, principal, provider):
    data = json.dumps([principal.issuer, principal.subject, provider], ensure_ascii=False, separators=(',', ':')).encode()
    return prefix + hashlib.sha256(data).hexdigest()


def _read_response(request):
    with open_url(request) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError('Provider response too large')
    return json.loads(body)


class LinkCompletion:
    """Internal service; no route may return credentials or accept a principal argument."""

    def __init__(self, verifier, attempts, secrets_manager, key_id, prefix, github_app_secret=None):
        if not _nonempty(key_id) or not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9/_+=.@-]{1,300}', prefix) or (github_app_secret is not None and not _nonempty(github_app_secret)):
            raise ValueError('Invalid link configuration')
        self._verifier = verifier
        self._attempts = attempts
        self._secrets = secrets_manager
        self._key_id = key_id
        self._prefix = prefix
        self._github_app_secret = github_app_secret

    def _exchange(self, attempt, code):
        fields = {'grant_type': 'authorization_code', 'client_id': attempt.client_id,
                  'redirect_uri': attempt.redirect_uri, 'code': code, 'code_verifier': attempt.verifier}
        if attempt.provider == 'github':
            if self._github_app_secret is None:
                raise ValueError('Missing application credential')
            response = self._secrets.get_secret_value(SecretId=self._github_app_secret)
            secret = json.loads(response['SecretString'])
            if not isinstance(secret, dict) or not _valid_secret(secret.get('client_secret')):
                raise ValueError('Invalid application credential')
            fields['client_secret'] = secret['client_secret']
            url = 'https://github.com/login/oauth/access_token'
        elif attempt.provider == 'gitlab':
            url = 'https://gitlab.com/oauth/token'
        else:
            raise ValueError('Unsupported provider')
        request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
            headers={'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}, method='POST')
        tokens = _read_response(request)
        if not isinstance(tokens, dict) or not _valid_secret(tokens.get('access_token')) or not _valid_secret(tokens.get('refresh_token')) or not isinstance(tokens.get('token_type'), str) or tokens['token_type'].lower() != 'bearer' or type(tokens.get('expires_in')) is not int or not 0 < tokens['expires_in'] <= 86400:
            raise ValueError('Invalid provider token response')
        return tokens

    def _provider_user(self, provider, token):
        endpoint = {'github':'https://api.github.com/user', 'gitlab':'https://gitlab.com/api/v4/user'}[provider]
        request = urllib.request.Request(endpoint, headers={'Authorization':'Bearer '+token,'Accept':'application/json','User-Agent':'team-context-mvp'})
        user = _read_response(request)
        if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0:
            raise ValueError('Invalid provider user response')
        return user['id']

    def complete(self, authorization, provider, state, browser_nonce, code):
        """Finish a consumed link and store credentials; return no secrets."""
        if not _valid_secret(code) or len(code) > 2048:
            raise ValueError('Unable to complete account link')
        try:
            attempt = self._attempts.consume(authorization, provider, state, browser_nonce)
            if not isinstance(attempt, ConsumedAttempt) or attempt.provider != provider or not isinstance(attempt.principal, Principal) or not _nonempty(attempt.principal.issuer) or not _nonempty(attempt.principal.subject):
                raise ValueError('Invalid account link')
            tokens = self._exchange(attempt, code)
            expires_at = int(time.time()) + tokens['expires_in']
            user_id = self._provider_user(provider, tokens['access_token'])
            payload = {'issuer':attempt.principal.issuer, 'subject':attempt.principal.subject,
                       'provider':provider, 'provider_user_id':user_id,
                       'access_token':tokens['access_token'], 'refresh_token':tokens['refresh_token'],
                       'expires_at':expires_at}
            self._secrets.create_secret(Name=_secret_name(self._prefix, attempt.principal, provider),
                SecretString=json.dumps(payload), KmsKeyId=self._key_id)
        except Exception:
            raise ValueError('Unable to complete account link') from None
        return {'connected': True}

    def disconnect(self, authorization, provider):
        """Delete only the verified user's local binding for one provider."""
        principal = self._verifier.verify(authorization)
        if not isinstance(principal, Principal) or not _nonempty(principal.issuer) or not _nonempty(principal.subject) or provider not in ('github', 'gitlab'):
            raise ValueError('Unable to disconnect account link')
        try:
            self._secrets.delete_secret(SecretId=_secret_name(self._prefix, principal, provider), ForceDeleteWithoutRecovery=True)
        except Exception:
            raise ValueError('Unable to disconnect account link') from None
        return {'connected': False}

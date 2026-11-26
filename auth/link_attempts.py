"""One-use COA account-link attempts; no HTTP handler or token storage."""
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from auth import github, gitlab
from auth.coa_identity import Principal, _nonempty


@dataclass(frozen=True)
class StartResult:
    url: str = field(repr=False)
    browser_nonce: str = field(repr=False)


@dataclass(frozen=True)
class ConsumedAttempt:
    principal: Principal
    provider: str
    client_id: str
    redirect_uri: str
    verifier: str = field(repr=False)


def _digest(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def _random_value(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', value) is not None


def _registration(provider, registration):
    if provider not in ('github', 'gitlab') or not isinstance(registration, tuple) or len(registration) != 2:
        raise ValueError('Invalid provider registration')
    client_id, callback = registration
    if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', client_id) or not _nonempty(callback):
        raise ValueError('Invalid provider registration')
    url = urlsplit(callback)
    if url.scheme != 'https' or not url.hostname or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*[a-z0-9]', url.hostname) or url.username or url.password or url.port or url.query or url.fragment or callback != url.geturl() or url.path != f'/oauth/{provider}/callback':
        raise ValueError('Invalid provider registration')
    return client_id, callback


class LinkAttempts:
    """Bind a verified COA user to an encrypted, one-use OAuth attempt.

    Clients, key, registrations, and verifier are trusted service configuration.
    The caller must return the nonce only as a secure, HttpOnly browser cookie.
    """

    def __init__(self, verifier, dynamodb, kms, table, key_id, registrations):
        if not isinstance(table, str) or not table or not isinstance(key_id, str) or not key_id or not isinstance(registrations, dict) or not registrations:
            raise ValueError('Invalid link configuration')
        self._verifier = verifier
        self._db = dynamodb
        self._kms = kms
        self._table = table
        self._key_id = key_id
        self._registrations = {provider: _registration(provider, item) for provider, item in registrations.items()}

    def start(self, authorization, provider):
        principal = self._verifier.verify(authorization)
        if not isinstance(principal, Principal) or not _nonempty(principal.issuer) or not _nonempty(principal.subject) or not isinstance(provider, str) or provider not in self._registrations:
            raise ValueError('Unable to start account link')
        client_id, redirect_uri = self._registrations[provider]
        oauth = (github.Authorization if provider == 'github' else gitlab.Authorization)(client_id, redirect_uri)
        state, verifier = oauth._state, oauth._verifier
        nonce = secrets.token_urlsafe(32)
        if not _random_value(state) or not _random_value(nonce):
            raise ValueError('Unable to start account link')
        state_key, nonce_key = _digest(provider + ':' + state), _digest(nonce)
        expires = int(time.time()) + 300
        context = {'purpose': 'team-context-account-link', 'state': state_key}
        payload = {'issuer': principal.issuer, 'subject': principal.subject, 'provider': provider,
                   'client_id': client_id, 'redirect_uri': redirect_uri, 'verifier': verifier,
                   'state': state_key, 'nonce': nonce_key, 'expires': expires}
        ciphertext = self._kms.encrypt(KeyId=self._key_id, Plaintext=json.dumps(payload).encode(), EncryptionContext=context)['CiphertextBlob']
        self._db.put_item(TableName=self._table, Item={'state_key': {'S': state_key}, 'nonce_hash': {'S': nonce_key},
            'expires_at': {'N': str(expires)}, 'ciphertext': {'B': ciphertext}}, ConditionExpression='attribute_not_exists(state_key)')
        return StartResult(oauth.url(), nonce)

    def consume(self, provider, state, browser_nonce):
        if not isinstance(provider, str) or provider not in self._registrations or not _random_value(state) or not _random_value(browser_nonce):
            raise ValueError('Invalid or expired account link')
        state_key, nonce_key = _digest(provider + ':' + state), _digest(browser_nonce)
        now = int(time.time())
        try:
            response = self._db.delete_item(TableName=self._table, Key={'state_key': {'S': state_key}},
                ConditionExpression='#nonce = :nonce AND #expiry > :now',
                ExpressionAttributeNames={'#nonce': 'nonce_hash', '#expiry': 'expires_at'},
                ExpressionAttributeValues={':nonce': {'S': nonce_key}, ':now': {'N': str(now)}}, ReturnValues='ALL_OLD')
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
                raise ValueError('Invalid or expired account link') from None
            raise
        ciphertext = response['Attributes']['ciphertext']['B']
        context = {'purpose': 'team-context-account-link', 'state': state_key}
        payload = json.loads(self._kms.decrypt(KeyId=self._key_id, CiphertextBlob=ciphertext, EncryptionContext=context)['Plaintext'])
        if not isinstance(payload, dict) or payload.get('state') != state_key or payload.get('nonce') != nonce_key or type(payload.get('expires')) is not int or payload['expires'] <= time.time():
            raise ValueError('Invalid or expired account link')
        if payload.get('provider') != provider or (payload.get('client_id'), payload.get('redirect_uri')) != self._registrations[provider]:
            raise ValueError('Invalid or expired account link')
        if not _nonempty(payload.get('issuer')) or not _nonempty(payload.get('subject')) or not isinstance(payload.get('verifier'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{64}', payload['verifier']):
            raise ValueError('Invalid or expired account link')
        return ConsumedAttempt(Principal(payload['issuer'], payload['subject']), provider, payload['client_id'], payload['redirect_uri'], payload['verifier'])

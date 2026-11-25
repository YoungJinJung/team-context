"""Bind a one-use OAuth attempt to a local browser session."""
import secrets
import threading
import time
import urllib.error

from auth.linear import Authorization, refresh
from auth import vault, session_store


class Sessions:
    def __init__(self, authorization=None, renew=None, namespace=None):
        self._authorization = authorization
        self._renew = renew
        self._store_options = {'namespace': namespace} if namespace is not None else {}
        self._items = {}
        self._lock = threading.Lock()

    def start(self, client_id, redirect_uri):
        session = secrets.token_urlsafe(32)
        attempt = (self._authorization or Authorization)(client_id, redirect_uri)
        with self._lock:
            now = time.monotonic()
            self._items = {key: value for key, value in self._items.items() if value[1] > now}
            self._items[session] = (attempt, now + 300)
        return session, attempt.url()

    def complete(self, session, state, code):
        with self._lock:
            entry = self._items.pop(session, None)
        if entry is None or entry[1] <= time.monotonic():
            raise ValueError('Invalid or expired session')
        tokens = entry[0].exchange(state, code)
        secret_id = vault.create({**tokens, "client_id": entry[0].client_id})
        try:
            session_store.put(session, secret_id, **self._store_options)
        except Exception:
            vault.delete(secret_id)
            raise
        return secret_id

    def connected(self, session):
        with self._lock:
            return session_store.get(session, **self._store_options) is not None

    def disconnect(self, session):
        with self._lock:
            secret_id = session_store.get(session, **self._store_options)
            if secret_id is None:
                raise ValueError('No connection')
            vault.delete(secret_id)
            session_store.remove(session, **self._store_options)

    def use(self, session, operation):
        with self._lock:
            secret_id = session_store.get(session, **self._store_options)
            if secret_id is None:
                raise ValueError('No connection')
            tokens = vault.read(secret_id)
            try:
                return operation(tokens['access_token'])
            except urllib.error.HTTPError as error:
                if error.code != 401 or not tokens.get('client_id') or not tokens.get('refresh_token'):
                    raise
            renewed = (self._renew or refresh)(tokens['client_id'], tokens['refresh_token'])
            vault.replace(secret_id, {**renewed, 'client_id': tokens['client_id']})
            return operation(renewed['access_token'])

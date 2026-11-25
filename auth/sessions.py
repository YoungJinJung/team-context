"""Bind a one-use OAuth attempt to a local browser session."""
import secrets
import threading
import time

from auth.linear import Authorization
from auth import vault


class Sessions:
    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()
        self._completed = {}

    def start(self, client_id, redirect_uri):
        session = secrets.token_urlsafe(32)
        attempt = Authorization(client_id, redirect_uri)
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
        secret_id = vault.create(tokens)
        with self._lock:
            self._completed[session] = secret_id
        return secret_id

    def connected(self, session):
        with self._lock:
            return session in self._completed

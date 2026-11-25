from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
import pytest
from auth.sessions import Sessions


def test_session_binds_attempt_and_stores_tokens():
    attempt = Mock()
    attempt.url.return_value = 'https://linear.app/oauth/authorize?state=sample'
    attempt.exchange.return_value = {'access_token': 'a', 'refresh_token': 'r'}
    with patch('auth.sessions.Authorization', return_value=attempt) as factory, patch('auth.sessions.secrets.token_urlsafe', return_value='cookie') as random, patch('auth.sessions.time.monotonic', return_value=100), patch('auth.sessions.vault.create', return_value='opaque-id') as create:
        sessions = Sessions()
        assert sessions.start('client', 'http://127.0.0.1/callback') == ('cookie', attempt.url())
        assert sessions.complete('cookie', 'state', 'code') == 'opaque-id'
        with pytest.raises(ValueError): sessions.complete('cookie', 'state', 'code')
    factory.assert_called_once_with('client', 'http://127.0.0.1/callback')
    random.assert_called_once_with(32)
    attempt.exchange.assert_called_once_with('state', 'code')
    create.assert_called_once_with({'access_token': 'a', 'refresh_token': 'r'})


def test_missing_or_expired_session():
    sessions = Sessions()
    with patch('auth.sessions.time.monotonic', return_value=100):
        session, _ = sessions.start('client', 'http://127.0.0.1/callback')
    with pytest.raises(ValueError, match='Invalid or expired session'): sessions.complete('other', 'state', 'code')
    with patch('auth.sessions.time.monotonic', return_value=400):
        with pytest.raises(ValueError, match='Invalid or expired session'): sessions.complete(session, 'state', 'code')


def test_invalid_state_does_not_reach_vault():
    sessions = Sessions()
    session, _ = sessions.start('client', 'http://127.0.0.1/callback')
    with patch('auth.sessions.vault.create') as create:
        with pytest.raises(ValueError): sessions.complete(session, 'wrong', 'code')
        create.assert_not_called()
    with pytest.raises(ValueError): sessions.complete(session, 'wrong', 'code')


def test_concurrent_completions_consume_once():
    sessions = Sessions()
    session, _ = sessions.start('client', 'http://127.0.0.1/callback')
    def call(_):
        try: return sessions.complete(session, 'state', 'code')
        except ValueError: return None
    with patch('auth.sessions.Authorization.exchange', return_value={'access_token': 'a', 'refresh_token': 'r'}), patch('auth.sessions.vault.create', return_value='id') as create:
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(call, range(2)))
    assert sorted(str(v) for v in results) == ['None', 'id']
    create.assert_called_once()


def test_completed_status_is_session_specific():
    sessions = Sessions()
    session, _ = sessions.start('client', 'http://127.0.0.1/callback')
    assert sessions.connected(session) is False
    with patch('auth.sessions.Authorization.exchange', return_value={'access_token': 'a', 'refresh_token': 'r'}), patch('auth.sessions.vault.create', return_value='secret'):
        sessions.complete(session, 'state', 'code')
    assert sessions.connected(session) is True
    assert sessions.connected('different') is False


def test_expired_attempts_are_purged_when_starting():
    sessions = Sessions()
    with patch('auth.sessions.time.monotonic', return_value=100):
        expired, _ = sessions.start('client', 'http://127.0.0.1/callback')
    with patch('auth.sessions.time.monotonic', return_value=400):
        current, _ = sessions.start('client', 'http://127.0.0.1/callback')
    assert expired not in sessions._items
    assert current in sessions._items

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
import pytest
from auth.sessions import Sessions


@pytest.fixture(autouse=True)
def fake_store():
    values = {}
    with patch('auth.sessions.session_store.put', side_effect=lambda session, secret: values.__setitem__(session, secret)), patch('auth.sessions.session_store.get', side_effect=lambda session: values.get(session)), patch('auth.sessions.session_store.remove', side_effect=lambda session: values.pop(session)):
        yield values


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


def test_disconnect_deletes_secret_only_for_session():
    sessions = Sessions()
    one, _ = sessions.start('client', 'http://127.0.0.1/callback')
    two, _ = sessions.start('client', 'http://127.0.0.1/callback')
    with patch('auth.sessions.Authorization.exchange', return_value={}), patch('auth.sessions.vault.create', side_effect=['one-secret', 'two-secret']), patch('auth.sessions.vault.delete') as delete:
        sessions.complete(one, 'state', 'code')
        sessions.complete(two, 'state', 'code')
        sessions.disconnect(one)
        delete.assert_called_once_with('one-secret')
        assert not sessions.connected(one) and sessions.connected(two)
        with pytest.raises(ValueError): sessions.disconnect(one)


def test_disconnect_failure_can_be_retried():
    sessions = Sessions()
    session, _ = sessions.start('client', 'http://127.0.0.1/callback')
    with patch('auth.sessions.Authorization.exchange', return_value={}), patch('auth.sessions.vault.create', return_value='secret'), patch('auth.sessions.vault.delete', side_effect=[OSError('failed'), None]) as delete:
        sessions.complete(session, 'state', 'code')
        with pytest.raises(OSError): sessions.disconnect(session)
        assert sessions.connected(session)
        sessions.disconnect(session)
        assert not sessions.connected(session)
        assert delete.call_count == 2


def test_failed_reference_write_deletes_new_secret():
    sessions = Sessions()
    session, _ = sessions.start('client', 'http://127.0.0.1/callback')
    with patch('auth.sessions.Authorization.exchange', return_value={}), patch('auth.sessions.vault.create', return_value='secret'), patch('auth.sessions.session_store.put', side_effect=OSError('failed')), patch('auth.sessions.vault.delete') as delete:
        with pytest.raises(OSError): sessions.complete(session, 'state', 'code')
    delete.assert_called_once_with('secret')


def test_disconnect_missing_session_error():
    sessions = Sessions()
    with pytest.raises(ValueError, match='^No connection$'):
        sessions.disconnect('missing')


def test_use_only_reads_current_session_secret():
    sessions = Sessions()
    session, _ = sessions.start('client', 'http://127.0.0.1/callback')
    with patch('auth.sessions.Authorization.exchange', return_value={}), patch('auth.sessions.vault.create', return_value='secret'), patch('auth.sessions.vault.read', return_value={'access_token': 'private'}) as read:
        sessions.complete(session, 'state', 'code')
        assert sessions.use(session, lambda token: token + '-result') == 'private-result'
        read.assert_called_once_with('secret')
        with pytest.raises(ValueError, match='No connection'): sessions.use('other', lambda token: token)

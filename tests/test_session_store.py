import hashlib
from unittest.mock import Mock, patch
import pytest
from auth import session_store


SESSION = 'a' * 43
KEY = 'session:' + hashlib.sha256(SESSION.encode()).hexdigest()


def test_put_only_hashes_cookie_and_reference():
    db = Mock()
    with patch('auth.session_store.clients', return_value=(Mock(), db)), patch('auth.session_store.time.time', return_value=100):
        session_store.put(SESSION, 'secret-id')
    db.put_item.assert_called_once_with(TableName='team-context-sync', Item={'connection_id': {'S': KEY}, 'secret_id': {'S': 'secret-id'}, 'expires_at': {'N': '2592100'}})
    assert SESSION not in str(db.put_item.call_args)


def test_get_requires_live_unexpired_session():
    db = Mock()
    db.get_item.return_value = {'Item': {'secret_id': {'S': 'secret-id'}, 'expires_at': {'N': '200'}}}
    with patch('auth.session_store.clients', return_value=(Mock(), db)), patch('auth.session_store.time.time', return_value=199):
        assert session_store.get(SESSION) == 'secret-id'
    db.get_item.assert_called_once_with(TableName='team-context-sync', Key={'connection_id': {'S': KEY}}, ConsistentRead=True)
    with patch('auth.session_store.clients', return_value=(Mock(), db)), patch('auth.session_store.time.time', return_value=200):
        assert session_store.get(SESSION) is None
    db.get_item.return_value = {}
    with patch('auth.session_store.clients', return_value=(Mock(), db)):
        assert session_store.get(SESSION) is None


def test_remove():
    db = Mock()
    with patch('auth.session_store.clients', return_value=(Mock(), db)):
        session_store.remove(SESSION)
    db.delete_item.assert_called_once_with(TableName='team-context-sync', Key={'connection_id': {'S': KEY}})


@pytest.mark.parametrize('value', [None, '', 'x' * 42, 'x' * 44, '!' * 43, 2])
def test_invalid_session_has_no_database_access(value):
    with patch('auth.session_store.clients') as clients:
        assert session_store.get(value) is None
        with pytest.raises(ValueError): session_store.put(value, 'secret')
        with pytest.raises(ValueError): session_store.remove(value)
        clients.assert_not_called()


def test_invalid_session_error_contract():
    with pytest.raises(ValueError, match='^Invalid session$'): session_store.put('bad', 'secret')
    with pytest.raises(ValueError, match='^Invalid session$'): session_store.remove('bad')


def test_valid_session_alphabet():
    valid = 'a' * 41 + '_-'
    db = Mock()
    db.get_item.return_value = {}
    with patch('auth.session_store.clients', return_value=(Mock(), db)):
        assert session_store.get(valid) is None
    assert db.get_item.call_count == 1


def test_namespaced_sessions_cannot_alias_linear_sessions():
    digest = hashlib.sha256(SESSION.encode()).hexdigest()
    db = Mock()
    db.get_item.return_value = {}
    with patch('auth.session_store.clients', return_value=(Mock(), db)), patch('auth.session_store.time.time', return_value=100):
        session_store.put(SESSION, 'teams-secret', namespace='teams')
        session_store.get(SESSION, namespace='teams')
        session_store.remove(SESSION, namespace='teams')
    assert db.put_item.call_args.kwargs['Item']['connection_id']['S'] == 'session:teams:' + digest
    assert db.get_item.call_args.kwargs['Key']['connection_id']['S'] == 'session:teams:' + digest
    assert db.delete_item.call_args.kwargs['Key']['connection_id']['S'] == 'session:teams:' + digest
    assert session_store._key(SESSION) == 'session:' + digest
    with pytest.raises(ValueError, match='Invalid namespace'): session_store.get(SESSION, namespace='other')


def test_gitlab_namespace_is_distinct():
    digest = hashlib.sha256(SESSION.encode()).hexdigest()
    assert session_store._key(SESSION, 'gitlab') == 'session:gitlab:' + digest
    assert session_store._key(SESSION, 'teams') == 'session:teams:' + digest

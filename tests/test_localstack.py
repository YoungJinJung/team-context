"""Run with TEAM_CONTEXT_INTEGRATION=1 against the local emulator."""
import hashlib
import json
import os
import pytest
from storage import clients, persist
from init_local import initialize

pytestmark = pytest.mark.skipif(os.environ.get('TEAM_CONTEXT_INTEGRATION') != '1', reason='requires local emulator')


def test_s3_and_dynamodb_roundtrip():
    initialize()
    initialize()
    record = {'id': 'integration:pr:1', 'type': 'pull_request', 'title': 'Local test'}
    persist('github:example/test', [record])
    s3, db = clients()
    key = hashlib.sha256(record['id'].encode()).hexdigest() + '.json'
    assert json.loads(s3.get_object(Bucket='team-context-records', Key=key)['Body'].read()) == record
    item = db.get_item(TableName='team-context-sync', Key={'connection_id': {'S': 'github:example/test'}})['Item']
    assert item['count']['N'] == '1'
    s3.delete_object(Bucket='team-context-records', Key=key)
    db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': 'github:example/test'}})


def test_secrets_roundtrip():
    from auth import vault
    tokens = {'access_token': 'test-access', 'refresh_token': 'test-refresh'}
    secret = vault.create(tokens)
    try:
        assert vault.read(secret) == tokens
        replacement = {'access_token': 'new-access', 'refresh_token': 'new-refresh'}
        vault.replace(secret, replacement)
        assert vault.read(secret) == replacement
    finally:
        vault.delete(secret)


def test_browser_session_with_real_local_secrets_manager():
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlsplit
    from auth.sessions import Sessions
    from auth import vault
    sessions = Sessions()
    cookie, url = sessions.start('public-client', 'http://127.0.0.1:8765/oauth/linear/callback')
    state = parse_qs(urlsplit(url).query)['state'][0]
    pair = {'access_token': 'local-example-access', 'refresh_token': 'local-example-refresh'}
    with patch('auth.linear._token', return_value=pair):
        secret_id = sessions.complete(cookie, state, 'one-time-code')
    try:
        assert vault.read(secret_id) == pair
        assert sessions.connected(cookie)
        assert Sessions().connected(cookie)
        _, db = clients()
        key = 'session:' + hashlib.sha256(cookie.encode()).hexdigest()
        entry = db.get_item(TableName='team-context-sync', Key={'connection_id': {'S': key}}, ConsistentRead=True)['Item']
        assert entry['secret_id']['S'] == secret_id
        assert cookie not in str(entry) and 'local-example-access' not in str(entry)
        with pytest.raises(ValueError): sessions.complete(cookie, state, 'one-time-code')
    finally:
        sessions.disconnect(cookie)
    assert not sessions.connected(cookie)
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError): vault.read(secret_id)


def test_search_returns_public_records_from_s3():
    import storage
    initialize()
    unique = 'integration-search-example'
    records = [{'id': unique + '-public', 'title': unique, 'body': 'hello', 'visibility': 'public'}, {'id': unique + '-private', 'title': unique, 'body': 'secret', 'visibility': 'private'}]
    persist('search:test', records)
    try:
        assert storage.search(unique) == records[:1]
    finally:
        s3, db = clients()
        for record in records:
            key = hashlib.sha256(record['id'].encode()).hexdigest() + '.json'
            s3.delete_object(Bucket='team-context-records', Key=key)
        db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': 'search:test'}})


def test_disconnect_recovers_after_secret_already_deleted():
    from auth.sessions import Sessions
    from auth import session_store, vault
    sessions = Sessions()
    session = 'a' * 43
    secret = vault.create({'access_token': 'local-only'})
    session_store.put(session, secret)
    vault.delete(secret)
    sessions.disconnect(session)
    assert not sessions.connected(session)

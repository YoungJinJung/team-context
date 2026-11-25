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
        with pytest.raises(ValueError): sessions.complete(cookie, state, 'one-time-code')
    finally:
        vault.delete(secret_id)

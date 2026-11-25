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
        assert vault.read(secret_id) == {**pair, 'client_id': 'public-client'}
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
        from unittest.mock import patch
        with patch("storage.current", return_value=True):
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


def test_local_token_refresh_persists_replacement_pair():
    from unittest.mock import patch
    from urllib.error import HTTPError
    from auth.sessions import Sessions
    from auth import vault
    sessions = Sessions()
    session, _ = sessions.start('public-client', 'http://127.0.0.1:8765/oauth/linear/callback')
    with patch('auth.linear._token', return_value={'access_token': 'old', 'refresh_token': 'old-refresh'}):
        from urllib.parse import parse_qs, urlsplit
        state = parse_qs(urlsplit(sessions._items[session][0].url()).query)['state'][0]
        secret = sessions.complete(session, state, 'code')
    def operation(token):
        if token == 'old': raise HTTPError('https://api.linear.app/graphql', 401, 'expired', {}, None)
        return token
    try:
        with patch('auth.sessions.refresh', return_value={'access_token': 'new', 'refresh_token': 'new-refresh'}):
            assert sessions.use(session, operation) == 'new'
        assert vault.read(secret) == {'access_token': 'new', 'refresh_token': 'new-refresh', 'client_id': 'public-client'}
    finally:
        sessions.disconnect(session)


def test_search_revalidates_local_record_before_return():
    from unittest.mock import patch
    import io
    import storage
    initialize()
    record = {'id': 'github:local/example:pr:123456789', 'visibility': 'public', 'type': 'pull_request', 'title': 'Local unique verification example', 'url': 'https://github.com/local/example/pull/123456789', 'updated_at': 'time', 'body': 'body'}
    provider = {'number': 123456789, 'title': record['title'], 'html_url': record['url'], 'updated_at': record['updated_at'], 'body': record['body']}
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    persist('github:local/example', [record])
    try:
        with patch('source_verification.open_url', return_value=Response(json.dumps(provider).encode())):
            assert storage.search('Local unique verification example') == [record]
        provider['title'] = 'Changed'
        with patch('source_verification.open_url', return_value=Response(json.dumps(provider).encode())):
            assert storage.search('Local unique verification example') == []
    finally:
        s3, db = clients()
        key = hashlib.sha256(record['id'].encode()).hexdigest() + '.json'
        s3.delete_object(Bucket='team-context-records', Key=key)
        db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': 'github:local/example'}})


def test_remove_only_selected_public_source():
    import storage
    initialize()
    ids = ['github:local/remove-this:pr:888888', 'github:local/keep-this:pr:888888']
    records = [{'id': identifier, 'title': 'test'} for identifier in ids]
    persist('github:local/remove-this', records[:1])
    persist('github:local/keep-this', records[1:])
    s3, db = clients()
    try:
        assert storage.remove_public_source('github:local/remove-this') == 1
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError) as error:
            s3.get_object(Bucket='team-context-records', Key=hashlib.sha256(ids[0].encode()).hexdigest() + '.json')
        assert error.value.response['Error']['Code'] == 'NoSuchKey'
        assert json.loads(s3.get_object(Bucket='team-context-records', Key=hashlib.sha256(ids[1].encode()).hexdigest() + '.json')['Body'].read()) == records[1]
        assert 'Item' not in db.get_item(TableName='team-context-sync', Key={'connection_id': {'S': 'github:local/remove-this'}}, ConsistentRead=True)
    finally:
        for identifier in ids:
            s3.delete_object(Bucket='team-context-records', Key=hashlib.sha256(identifier.encode()).hexdigest() + '.json')
        for connection in ('github:local/remove-this', 'github:local/keep-this'):
            db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': connection}})


def test_cleanup_unavailable_only_removes_matching_local_record():
    from unittest.mock import patch
    import storage
    initialize()
    records = [{'id': 'github:local/cleanup:pr:999999', 'visibility': 'public'}, {'id': 'github:local/keep:pr:999999', 'visibility': 'public'}]
    persist('github:local/cleanup', records[:1])
    persist('github:local/keep', records[1:])
    s3, db = clients()
    def status(record):
        return 'unavailable' if record['id'] == records[0]['id'] else 'current'
    try:
        with patch('storage.status', side_effect=status):
            assert storage.cleanup_unavailable() == 1
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError): s3.get_object(Bucket='team-context-records', Key=hashlib.sha256(records[0]['id'].encode()).hexdigest() + '.json')
        assert s3.get_object(Bucket='team-context-records', Key=hashlib.sha256(records[1]['id'].encode()).hexdigest() + '.json')
    finally:
        for record in records:
            s3.delete_object(Bucket='team-context-records', Key=hashlib.sha256(record['id'].encode()).hexdigest() + '.json')
        for connection in ('github:local/cleanup', 'github:local/keep'):
            db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': connection}})


def test_list_public_sources_does_not_expose_session_metadata():
    import storage
    from auth import session_store
    initialize()
    session = 'b' * 43
    session_store.put(session, 'private-reference')
    persist('github:local/list-source', [{'id': 'github:local/list-source:pr:9999'}])
    try:
        sources = storage.list_public_sources()
        assert {'connector': 'github', 'source': 'local/list-source', 'count': 1} in sources
        assert session not in str(sources) and 'private-reference' not in str(sources)
    finally:
        session_store.remove(session)
        s3, db = clients()
        s3.delete_object(Bucket='team-context-records', Key=hashlib.sha256(b'github:local/list-source:pr:9999').hexdigest() + '.json')
        db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': 'github:local/list-source'}})


def test_teams_session_is_isolated_from_linear_session():
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlsplit
    from auth import teams, vault
    from auth.sessions import Sessions
    initialize()
    teams_sessions = Sessions(teams.Authorization, teams.refresh, 'teams')
    linear_sessions = Sessions()
    cookie, url = teams_sessions.start('client', 'http://127.0.0.1/callback')
    state = parse_qs(urlsplit(url).query)['state'][0]
    with patch('auth.teams._token', return_value={'access_token': 'local-access', 'refresh_token': 'local-refresh'}):
        secret = teams_sessions.complete(cookie, state, 'code')
    try:
        assert teams_sessions.connected(cookie)
        assert Sessions(teams.Authorization, teams.refresh, 'teams').connected(cookie)
        assert not linear_sessions.connected(cookie)
        assert teams_sessions.use(cookie, lambda token: token) == 'local-access'
    finally:
        teams_sessions.disconnect(cookie)
    assert not teams_sessions.connected(cookie)
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError): vault.read(secret)

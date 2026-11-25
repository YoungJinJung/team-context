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
    records = [{'id': identifier, 'title': 'test', 'visibility': 'public'} for identifier in ids]
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


def test_gitlab_session_remains_private_and_isolated():
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlsplit
    from auth import gitlab, teams, vault
    from auth.sessions import Sessions
    initialize()
    gitlab_sessions = Sessions(gitlab.Authorization, gitlab.refresh, 'gitlab')
    cookie, url = gitlab_sessions.start('client', gitlab.CALLBACK)
    state = parse_qs(urlsplit(url).query)['state'][0]
    with patch('auth.gitlab._token', return_value={'access_token':'example-access', 'refresh_token':'example-refresh'}):
        secret = gitlab_sessions.complete(cookie, state, 'code')
    try:
        assert gitlab_sessions.connected(cookie)
        assert Sessions(gitlab.Authorization, gitlab.refresh, 'gitlab').connected(cookie)
        assert not Sessions().connected(cookie)
        assert not Sessions(teams.Authorization, teams.refresh, 'teams').connected(cookie)
        assert gitlab_sessions.use(cookie, lambda token: token) == 'example-access'
    finally:
        gitlab_sessions.disconnect(cookie)
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError): vault.read(secret)


def test_github_app_secret_and_session_are_isolated():
    import io
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlsplit
    from auth import github, gitlab, teams, vault
    from auth.sessions import Sessions
    initialize()
    app_secret = vault.create({'client_secret':'fake-application-secret'})
    sessions = Sessions(github.Authorization, github.refresh, 'github')
    cookie, url = sessions.start('client', github.CALLBACK)
    state = parse_qs(urlsplit(url).query)['state'][0]
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    try:
        with patch.dict('os.environ', {'GITHUB_APP_SECRET_ID':app_secret}), patch('auth.github.open_url', return_value=Response(b'{"access_token":"example-access","refresh_token":"example-refresh"}')) as send:
            user_secret = sessions.complete(cookie, state, 'code')
        try:
            assert b'client_secret=fake-application-secret' in send.call_args.args[0].data
            assert sessions.connected(cookie)
            assert not Sessions().connected(cookie)
            assert not Sessions(gitlab.Authorization, gitlab.refresh, 'gitlab').connected(cookie)
            assert not Sessions(teams.Authorization, teams.refresh, 'teams').connected(cookie)
            assert sessions.use(cookie, lambda token: token) == 'example-access'
        finally:
            sessions.disconnect(cookie)
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError): vault.read(user_secret)
    finally:
        vault.delete(app_secret)


def test_public_source_removal_preserves_nonpublic_records():
    import storage
    initialize()
    ids = ['github:local/boundary:pr:188881', 'github:local/boundary:pr:188882', 'github:local/boundary:pr:188883']
    records = [{'id':ids[0], 'visibility':'public'}, {'id':ids[1], 'visibility':'private'}, {'id':ids[2]}]
    persist('github:local/boundary', records)
    s3, db = clients()
    keys = [hashlib.sha256(identifier.encode()).hexdigest() + '.json' for identifier in ids]
    try:
        assert storage.remove_public_source('github:local/boundary') == 1
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError): s3.get_object(Bucket='team-context-records', Key=keys[0])
        for record, key in zip(records[1:], keys[1:]):
            assert json.loads(s3.get_object(Bucket='team-context-records', Key=key)['Body'].read()) == record
    finally:
        for key in keys: s3.delete_object(Bucket='team-context-records', Key=key)
        db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S':'github:local/boundary'}})


def test_private_gitlab_snapshot_requires_current_requester_access():
    from unittest.mock import patch
    from urllib.error import HTTPError
    from auth import gitlab_private
    import storage
    import uuid
    initialize()
    project_id = uuid.uuid4().int % 1000000000000000000 + 1
    project = f'local-test-{uuid.uuid4().hex}/fixture'
    encoded_project = project.replace('/', '%2F')
    record = {'iid':1, 'project_id':project_id, 'title':'local private phrase', 'web_url':'https://gitlab.com/a/b/-/merge_requests/1', 'updated_at':'2026-01-01', 'description':'local private body'}
    state = {'allowed':True, 'title':record['title']}
    def provider(token, path):
        if token != 'allowed' or not state['allowed']:
            raise HTTPError('https://gitlab.com', 403, 'denied', {}, None)
        if path == 'projects/' + encoded_project: return {'id':project_id}
        if path.endswith('?scope=all&per_page=100&page=1'): return [record]
        if path.endswith('?scope=all&per_page=100&page=2'): return []
        if path.endswith('/merge_requests/1'): return {**record, 'title':state['title']}
        raise AssertionError(path)
    s3,_ = clients()
    try:
        with patch('auth.gitlab_private._get', side_effect=provider):
            assert gitlab_private.collect('allowed',project) == 1
            assert gitlab_private.search('allowed',project,'private phrase')[0]['title'] == record['title']
            with pytest.raises(HTTPError): gitlab_private.search('other',project,'private phrase')
            state['allowed']=False
            with pytest.raises(HTTPError): gitlab_private.search('allowed',project,'private phrase')
            state['allowed']=True
            state['title']='changed'
            assert gitlab_private.search('allowed',project,'private phrase') == []
        assert storage.search('local private phrase') == []
        assert storage.remove_public_source('gitlab:' + project) == 0
        assert json.loads(s3.get_object(Bucket='team-context-records',Key=f'private/gitlab/{project_id}.json')['Body'].read())['visibility'] == 'private'
    finally:
        s3.delete_object(Bucket='team-context-records',Key=f'private/gitlab/{project_id}.json')


def test_failed_private_collection_preserves_previous_snapshot():
    from unittest.mock import patch
    from urllib.error import HTTPError
    from auth import gitlab_private
    import uuid
    initialize()
    project_id=uuid.uuid4().int % 1000000000000000000 + 1
    first={'iid':1,'project_id':project_id,'title':'previous','web_url':'https://gitlab.com/a/b/-/merge_requests/1','updated_at':'2026-01-01','description':'body'}
    full=[{**first,'iid':n} for n in range(1,101)]
    s3,_=clients()
    key=f'private/gitlab/{project_id}.json'
    try:
        with patch('auth.gitlab_private._get', side_effect=[{'id':project_id},[first],[]]):
            assert gitlab_private.collect('viewer','a/b') == 1
        before=s3.get_object(Bucket='team-context-records',Key=key)['Body'].read()
        with patch('auth.gitlab_private._get', side_effect=[{'id':project_id},full,HTTPError('https://gitlab.com',500,'failed',{},None)]):
            with pytest.raises(HTTPError): gitlab_private.collect('viewer','a/b')
        assert s3.get_object(Bucket='team-context-records',Key=key)['Body'].read() == before
    finally:
        s3.delete_object(Bucket='team-context-records',Key=key)


def test_local_operator_removes_only_selected_private_snapshot():
    import uuid
    from scripts import private_snapshots
    initialize()
    first=uuid.uuid4().int % 1000000000000000000 + 1
    second=first + 1
    identifier='github:local-' + uuid.uuid4().hex + '/fixture:pr:1'
    public_key=hashlib.sha256(identifier.encode()).hexdigest()+'.json'
    first_key=f'private/gitlab/{first}.json'
    second_key=f'private/gitlab/{second}.json'
    s3,_=clients()
    s3.put_object(Bucket='team-context-records',Key=first_key,Body=b'{"visibility":"private"}')
    s3.put_object(Bucket='team-context-records',Key=second_key,Body=b'{"visibility":"private"}')
    s3.put_object(Bucket='team-context-records',Key=public_key,Body=b'{"visibility":"public"}')
    try:
        assert first in private_snapshots.list_ids() and second in private_snapshots.list_ids()
        private_snapshots.remove(first)
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError): s3.get_object(Bucket='team-context-records',Key=first_key)
        assert s3.get_object(Bucket='team-context-records',Key=second_key)['Body'].read() == b'{"visibility":"private"}'
        assert s3.get_object(Bucket='team-context-records',Key=public_key)['Body'].read() == b'{"visibility":"public"}'
        private_snapshots.remove(first)
    finally:
        for key in (first_key,second_key,public_key): s3.delete_object(Bucket='team-context-records',Key=key)

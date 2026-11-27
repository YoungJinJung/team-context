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
        if path == 'projects/' + encoded_project: return {'id':project_id,'path_with_namespace':'a/b'}
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
    full=[{**first,'iid':n,'web_url':f'https://gitlab.com/a/b/-/merge_requests/{n}'} for n in range(1,101)]
    s3,_=clients()
    key=f'private/gitlab/{project_id}.json'
    try:
        with patch('auth.gitlab_private._get', side_effect=[{'id':project_id,'path_with_namespace':'a/b'},[first],[]]):
            assert gitlab_private.collect('viewer','a/b') == 1
        before=s3.get_object(Bucket='team-context-records',Key=key)['Body'].read()
        with patch('auth.gitlab_private._get', side_effect=[{'id':project_id,'path_with_namespace':'a/b'},full,HTTPError('https://gitlab.com',500,'failed',{},None)]):
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


def test_atlassian_session_is_isolated_in_local_emulator():
    from auth import session_store
    from uuid import uuid4
    import secrets
    session = secrets.token_urlsafe(32)
    reference = 'fake-' + uuid4().hex
    try:
        session_store.put(session, reference, namespace='atlassian')
        assert session_store.get(session, namespace='atlassian') == reference
        assert session_store.get(session, namespace='github') is None
        assert session_store.get(session) is None
    finally:
        session_store.remove(session, namespace='atlassian')


def test_mcp_search_excludes_private_snapshots_and_revalidates_public():
    from unittest.mock import patch
    from uuid import uuid4
    from mcp_server import search_context
    marker = 'mcp' + uuid4().hex
    s3, _ = clients()
    public = {'id':marker, 'visibility':'public', 'title':marker, 'url':'https://github.com/a/b/pull/1', 'type':'pull_request', 'updated_at':'2026-01-01', 'body':'public'}
    key = hashlib.sha256(marker.encode()).hexdigest() + '.json'
    private_key = 'private/gitlab/' + str(int(uuid4().hex[:10],16)) + '.json'
    try:
        s3.put_object(Bucket='team-context-records', Key=key, Body=json.dumps(public).encode())
        s3.put_object(Bucket='team-context-records', Key=private_key, Body=json.dumps({'visibility':'private','title':marker}).encode())
        with patch('storage.current', return_value=True) as current:
            result = search_context(marker)
        assert len(result['items']) == 1 and result['items'][0]['snippet'] == 'public'
        current.assert_called_once_with(public)
        with patch('storage.current', return_value=False):
            assert search_context(marker) == {'items':[]}
        with patch('storage.current', side_effect=OSError('secret')):
            assert search_context(marker) == {'error':'Unable to search public sources'}
    finally:
        s3.delete_object(Bucket='team-context-records', Key=key)
        s3.delete_object(Bucket='team-context-records', Key=private_key)


def test_github_private_snapshot_requires_requester_and_revalidates_original():
    from unittest.mock import patch
    from urllib.error import HTTPError
    from uuid import uuid4
    from auth import github_private
    project_id = int(uuid4().hex[:10],16) or 1
    record = {'number':1,'base':{'repo':{'id':project_id,'path_with_namespace':'a/b'}},'title':'local secret phrase','html_url':'https://github.com/a/b/pull/1','updated_at':'2026-01-01','body':'private body'}
    meta = {'id':project_id,'full_name':'a/b'}
    key = f'private/github/{project_id}.json'
    s3, _ = clients()
    def provider(token,path):
        if token != 'allowed':
            raise HTTPError('https://api.github.com',404,'hidden',{},None)
        if path == 'repos/a/b': return meta
        if path.endswith('page=1'): return [record]
        if path.endswith('page=2'): return []
        if path == 'repos/a/b/pulls/1': return record
        raise AssertionError(path)
    try:
        with patch('auth.github_private._get',side_effect=provider):
            assert github_private.collect('allowed','a/b') == 1
            assert github_private.search('allowed','a/b','secret phrase')[0]['title'] == 'local secret phrase'
            with pytest.raises(HTTPError): github_private.search('other','a/b','secret phrase')
        with patch('auth.github_private._get',side_effect=[meta,{**record,'title':'changed'}]):
            assert github_private.search('allowed','a/b','secret phrase') == []
        assert json.loads(s3.get_object(Bucket='team-context-records',Key=key)['Body'].read())['visibility'] == 'private'
    finally:
        s3.delete_object(Bucket='team-context-records',Key=key)


def test_github_operator_cleanup_keeps_gitlab_and_public_records():
    from uuid import uuid4
    from botocore.exceptions import ClientError
    from scripts import private_snapshots
    identifier = int(uuid4().hex[:10],16) or 1
    github_key = f'private/github/{identifier}.json'
    gitlab_key = f'private/gitlab/{identifier}.json'
    public_key = uuid4().hex + '.json'
    s3, _ = clients()
    for key in (github_key,gitlab_key,public_key):
        s3.put_object(Bucket='team-context-records',Key=key,Body=b'{"fixture":true}')
    try:
        assert identifier in private_snapshots.list_ids('github')
        assert identifier in private_snapshots.list_ids('gitlab')
        private_snapshots.remove(identifier,'github')
        with pytest.raises(ClientError): s3.get_object(Bucket='team-context-records',Key=github_key)
        assert s3.get_object(Bucket='team-context-records',Key=gitlab_key)['Body'].read() == b'{"fixture":true}'
        assert s3.get_object(Bucket='team-context-records',Key=public_key)['Body'].read() == b'{"fixture":true}'
        private_snapshots.remove(identifier,'github')
    finally:
        for key in (github_key,gitlab_key,public_key): s3.delete_object(Bucket='team-context-records',Key=key)


def test_link_attempts_use_kms_and_one_use_dynamodb_condition():
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock
    from urllib.parse import parse_qs, urlsplit
    from uuid import uuid4
    pytest.importorskip('coa_common')
    from auth.coa_identity import Principal
    from auth.link_attempts import LinkAttempts, _digest
    initialize()
    from local_aws import client
    db=client('dynamodb');kms=client('kms')
    table='tc-link-test-'+uuid4().hex
    db.create_table(TableName=table,KeySchema=[{'AttributeName':'state_key','KeyType':'HASH'}],AttributeDefinitions=[{'AttributeName':'state_key','AttributeType':'S'}],BillingMode='PAY_PER_REQUEST')
    db.get_waiter('table_exists').wait(TableName=table, WaiterConfig={'Delay': 1, 'MaxAttempts': 30})
    key=kms.create_key(Description='fake test key')['KeyMetadata']['KeyId']
    verifier=Mock();verifier.verify.return_value=Principal('https://example.test','subject-a')
    store=LinkAttempts(verifier,db,kms,table,key,{'github':('client-id','https://link.example/oauth/github/callback')})
    try:
        result=store.start('Bearer fake','github')
        state=parse_qs(urlsplit(result.url).query)['state'][0]
        row=db.get_item(TableName=table,Key={'state_key':{'S':_digest('github:'+state)}},ConsistentRead=True)['Item']
        assert state not in repr(row) and result.browser_nonce not in repr(row) and 'subject-a' not in repr(row)
        with pytest.raises(ValueError): store.consume('Bearer fake','github',state,'x'*43)
        assert 'Item' in db.get_item(TableName=table,Key={'state_key':{'S':_digest('github:'+state)}},ConsistentRead=True)
        def consume(_):
            try: return store.consume('Bearer fake','github',state,result.browser_nonce)
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(consume,range(8)))
        assert sum(value is not None for value in results)==1
        assert next(value for value in results if value is not None).principal.subject=='subject-a'
        assert 'Item' not in db.get_item(TableName=table,Key={'state_key':{'S':_digest('github:'+state)}},ConsistentRead=True)
    finally:
        db.delete_table(TableName=table)
        kms.schedule_key_deletion(KeyId=key,PendingWindowInDays=7)


def test_link_completion_is_bound_to_identity_and_uses_kms_secrets():
    import io
    import json
    from unittest.mock import Mock, patch
    from urllib.parse import parse_qs, urlsplit
    from uuid import uuid4
    from botocore.exceptions import ClientError
    pytest.importorskip('coa_common')
    from auth.coa_identity import Principal
    from auth.link_attempts import LinkAttempts
    from auth.link_completion import LinkCompletion, _secret_name
    from auth.source_credentials import SourceCredentials
    from auth.source_provenance import SourceProvenance, content_digest

    from local_aws import client
    db=client('dynamodb');kms=client('kms');secrets=client('secretsmanager')
    table='tc-link-test-'+uuid4().hex;prefix='tc-test/'+uuid4().hex+'/'
    db.create_table(TableName=table,KeySchema=[{'AttributeName':'state_key','KeyType':'HASH'}],AttributeDefinitions=[{'AttributeName':'state_key','AttributeType':'S'}],BillingMode='PAY_PER_REQUEST')
    db.get_waiter('table_exists').wait(TableName=table, WaiterConfig={'Delay': 1, 'MaxAttempts': 30})
    key=kms.create_key(Description='fake test key')['KeyMetadata']['KeyId']
    alice=Principal('https://issuer.example','alice');bob=Principal('https://issuer.example','bob')
    verifier=Mock();verifier.verify.side_effect=lambda auth: {'Bearer alice':alice,'Bearer bob':bob}[auth]
    attempts=LinkAttempts(verifier,db,kms,table,key,{'gitlab':('client-id','https://link.example/oauth/gitlab/callback')})
    service=LinkCompletion(verifier,attempts,secrets,key,prefix)
    name=_secret_name(prefix,alice,'gitlab')
    try:
        start=attempts.start('Bearer alice','gitlab');state=parse_qs(urlsplit(start.url).query)['state'][0]
        with pytest.raises(ValueError): service.complete('Bearer bob','gitlab',state,start.browser_nonce,'code')
        with pytest.raises(ClientError): secrets.get_secret_value(SecretId=name)
        active = [True]
        verifier.verify.side_effect = lambda auth: alice if auth == 'Bearer alice' and active[0] else None
        start=attempts.start('Bearer alice','gitlab');state=parse_qs(urlsplit(start.url).query)['state'][0]
        token=json.dumps({'access_token':'fake-access','refresh_token':'fake-refresh','expires_in':3600,'token_type':'bearer'}).encode()

        def expire_during_user_lookup(request):
            if request.full_url.endswith('/oauth/token'):
                return io.BytesIO(token)
            active[0] = False
            return io.BytesIO(b'{"id":1}')

        with patch('auth.link_completion.open_url',side_effect=expire_during_user_lookup):
            with pytest.raises(ValueError, match='^Unable to complete account link$'):
                service.complete('Bearer alice','gitlab',state,start.browser_nonce,'code')
        with pytest.raises(ClientError): secrets.get_secret_value(SecretId=name)
        verifier.verify.side_effect=lambda auth: {'Bearer alice':alice,'Bearer bob':bob}[auth]
        start=attempts.start('Bearer alice','gitlab');state=parse_qs(urlsplit(start.url).query)['state'][0]
        clock = [1000.0]
        short_token = json.dumps({'access_token':'fake-access','refresh_token':'fake-refresh','expires_in':5,'token_type':'bearer'}).encode()

        def expire_provider_token(request):
            if request.full_url.endswith('/oauth/token'):
                return io.BytesIO(short_token)
            clock[0] = 1005.0
            return io.BytesIO(b'{"id":1}')

        with patch('auth.link_completion.time.time', side_effect=lambda: clock[0]), patch('auth.link_completion.open_url',side_effect=expire_provider_token):
            with pytest.raises(ValueError, match='^Unable to complete account link$'):
                service.complete('Bearer alice','gitlab',state,start.browser_nonce,'code')
        with pytest.raises(ClientError): secrets.get_secret_value(SecretId=name)
        start=attempts.start('Bearer alice','gitlab');state=parse_qs(urlsplit(start.url).query)['state'][0]
        with patch('auth.link_completion.open_url',side_effect=[io.BytesIO(token),io.BytesIO(b'{"id":1}')]):
            assert service.complete('Bearer alice','gitlab',state,start.browser_nonce,'code')=={'connected':True}
        stored=secrets.get_secret_value(SecretId=name)
        assert json.loads(stored['SecretString'])['subject']=='alice'
        assert secrets.describe_secret(SecretId=name)['KmsKeyId']==key
        resolver=SourceCredentials(verifier,secrets,prefix)
        assert resolver.resolve('Bearer alice','gitlab').access_token=='fake-access'
        with pytest.raises(ValueError): resolver.resolve('Bearer bob','gitlab')
        guard=SourceProvenance(resolver)
        record={'iid':5,'project_id':17,'title':'fixture','body':'text','url':'https://gitlab.com/acme/repo/-/merge_requests/5','updated_at':'2026-01-01'}
        item={'iid':5,'project_id':17,'title':'fixture','description':'text','web_url':record['url'],'updated_at':record['updated_at']}
        provenance={'provider':'gitlab','project':'acme/repo','scope_id':17,'number':5,'digest':content_digest(record)}
        with patch('auth.gitlab_private.open_url',side_effect=[io.BytesIO(b'{"id":17,"path_with_namespace":"acme/repo"}'),io.BytesIO(json.dumps(item).encode())]):
            assert guard.allows('Bearer alice',provenance) is True
        forged_url = 'https://gitlab.com/other/project/-/merge_requests/5'
        forged_record = {**record, 'url':forged_url}
        forged_item = {**item, 'web_url':forged_url}
        forged_provenance = {**provenance, 'digest':content_digest(forged_record)}
        with patch('auth.gitlab_private.open_url',side_effect=[io.BytesIO(b'{"id":17,"path_with_namespace":"acme/repo"}'),io.BytesIO(json.dumps(forged_item).encode())]):
            assert guard.allows('Bearer alice',forged_provenance) is False
        assert guard.allows('Bearer bob',provenance) is False
        with pytest.raises(ValueError): service.complete('Bearer alice','gitlab',state,start.browser_nonce,'code')
        assert service.disconnect('Bearer bob','gitlab')=={'connected':False}
        assert secrets.get_secret_value(SecretId=name)['SecretString']==stored['SecretString']
        assert service.disconnect('Bearer alice','gitlab')=={'connected':False}
        with pytest.raises(ValueError): resolver.resolve('Bearer alice','gitlab')
        assert guard.allows('Bearer alice',provenance) is False
        with pytest.raises(ClientError): secrets.get_secret_value(SecretId=name)
    finally:
        try: secrets.delete_secret(SecretId=name,ForceDeleteWithoutRecovery=True)
        except ClientError: pass
        db.delete_table(TableName=table)
        kms.schedule_key_deletion(KeyId=key,PendingWindowInDays=7)

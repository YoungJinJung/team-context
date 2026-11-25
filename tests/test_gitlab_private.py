import io
import json
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import pytest
from botocore.exceptions import ClientError
from auth import gitlab_private as private


def mr(iid=1, **changes):
    return {'iid':iid, 'project_id':42, 'title':'Secret title', 'web_url':'https://gitlab.com/a/b/-/merge_requests/1', 'updated_at':'2026-01-01', 'description':'Secret body', **changes}


def snapshot(items=None):
    return {'visibility':'private', 'project_id':42, 'records':[{'iid':1, 'project_id':42, 'title':'Secret title', 'url':'https://gitlab.com/a/b/-/merge_requests/1', 'updated_at':'2026-01-01', 'body':'Secret body'}] if items is None else items}


def client_error(code):
    return ClientError({'Error':{'Code':code, 'Message':'private'}}, 'GetObject')


def http_error(code):
    return HTTPError('https://gitlab.com', code, 'private', {}, None)


def test_network_request_is_bound_to_gitlab_and_bearer():
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    with patch('auth.gitlab_private.open_url', return_value=Response(b'{"id":42}')) as send:
        assert private._project('token', 'group/sub/repo') == 42
    req = send.call_args.args[0]
    assert req.full_url == 'https://gitlab.com/api/v4/projects/group%2Fsub%2Frepo'
    assert req.get_header('Authorization') == 'Bearer token' and req.get_header('Accept') == 'application/json'
    assert req.get_header('User-agent') == 'team-context-mvp'
    with patch('auth.gitlab_private.MAX_BYTES', 3), patch('auth.gitlab_private.open_url', return_value=Response(b'1234')):
        with pytest.raises(ValueError, match='GitLab response too large'): private._get('token', 'projects/42')


@pytest.mark.parametrize('project', ['', '../x', 'https://evil.example/x', None, 'x/y?x=1'])
def test_reject_project_before_network(project):
    with patch('auth.gitlab_private._get') as get:
        with pytest.raises(ValueError): private._project('token', project)
        get.assert_not_called()


@pytest.mark.parametrize('value', [None, {}, [], {'id':0}, {'id':True}, {'id':'42'}, {'id':-1}])
def test_reject_bad_project_response(value):
    with patch('auth.gitlab_private._get', return_value=value):
        with pytest.raises(ValueError, match='Invalid project'): private._project('token', 'a/b')


@pytest.mark.parametrize('item', [None, {}, mr(iid=0), mr(iid=True), mr(project_id=43), mr(project_id=True), mr(title=None), mr(web_url=None), mr(updated_at=None), mr(description=5)])
def test_reject_invalid_records(item):
    with pytest.raises(ValueError, match='Invalid merge request'): private._record(item, 42)


def test_collect_only_commits_after_complete_source():
    s3 = Mock()
    with patch('auth.gitlab_private._get', side_effect=[{'id':42}, [mr()], []]) as get, patch('auth.gitlab_private.clients', return_value=(s3, Mock())):
        assert private.collect('token','a/b') == 1
    assert get.call_args_list[0].args == ('token','projects/a%2Fb')
    assert get.call_args_list[1].args == ('token','projects/42/merge_requests?scope=all&per_page=100&page=1')
    call=s3.put_object.call_args.kwargs
    assert call['Bucket'] == 'team-context-records' and call['Key'] == 'private/gitlab/42.json' and call['ContentType'] == 'application/json'
    assert json.loads(call['Body']) == snapshot()


def test_collect_paginates_and_rejects_repeated_records():
    first = [mr(iid=n) for n in range(1, 101)]
    s3=Mock()
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},first,[mr(iid=101)],[]]), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        assert private.collect('t','a/b') == 101
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},first,[mr(iid=1)]]), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        s3.reset_mock()
        with pytest.raises(ValueError, match='Repeated merge request'): private.collect('t','a/b')
        s3.put_object.assert_not_called()


@pytest.mark.parametrize('batch', [{}, None, [mr()]*101])
def test_collect_rejects_invalid_pages_without_writing(batch):
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},batch]), patch('auth.gitlab_private.clients') as clients:
        with pytest.raises(ValueError, match='Invalid merge request page'): private.collect('t','a/b')
        clients.assert_not_called()


def test_collect_rejects_unfinished_ten_pages_without_writing():
    pages = [[mr(iid=page*100+n+1) for n in range(100)] for page in range(10)]
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},*pages,[mr(iid=1001)]]), patch('auth.gitlab_private.clients') as clients:
        with pytest.raises(ValueError, match='Collection exceeds 10 pages'): private.collect('t','a/b')
        clients.assert_not_called()


def test_collect_rejects_oversized_snapshot():
    with patch('auth.gitlab_private.MAX_BYTES', 10), patch('auth.gitlab_private._get', side_effect=[{'id':42},[mr()],[]]), patch('auth.gitlab_private.clients') as clients:
        with pytest.raises(ValueError, match='Snapshot too large'): private.collect('t','a/b')
        clients.assert_not_called()


def test_search_checks_project_and_original_with_requester_token():
    s3=Mock(); s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot()).encode())}
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},mr()]) as get, patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        assert private.search('viewer-token','a/b',' SECRET ') == snapshot()['records']
    assert [c.args for c in get.call_args_list] == [('viewer-token','projects/a%2Fb'),('viewer-token','projects/42/merge_requests/1')]
    s3.get_object.assert_called_once_with(Bucket='team-context-records', Key='private/gitlab/42.json')


@pytest.mark.parametrize('code', [403,404,410])
def test_search_hides_revoked_or_deleted_original(code):
    s3=Mock(); s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot()).encode())}
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},http_error(code)]), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        assert private.search('viewer','a/b','secret') == []


def test_search_hides_changed_record_and_aborts_provider_failure():
    s3=Mock()
    for result in (mr(title='Changed'), http_error(500)):
        s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot()).encode())}
        with patch('auth.gitlab_private._get', side_effect=[{'id':42},result]), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
            if isinstance(result, HTTPError):
                with pytest.raises(HTTPError): private.search('viewer','a/b','secret')
            else:
                assert private.search('viewer','a/b','secret') == []


def test_project_access_denied_before_storage_read():
    with patch('auth.gitlab_private._get', side_effect=http_error(403)), patch('auth.gitlab_private.clients') as clients:
        with pytest.raises(HTTPError): private.search('unauthorized','a/b','secret')
        clients.assert_not_called()


def test_missing_snapshot_is_empty_and_other_storage_errors_propagate():
    s3=Mock()
    with patch('auth.gitlab_private._get', return_value={'id':42}), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        s3.get_object.side_effect=client_error('NoSuchKey')
        assert private.search('viewer','a/b','secret') == []
        s3.get_object.side_effect=client_error('AccessDenied')
        with pytest.raises(ClientError): private.search('viewer','a/b','secret')


@pytest.mark.parametrize('query', [None, '', ' x ', 'a'*101])
def test_reject_bad_query_before_access(query):
    with patch('auth.gitlab_private._get') as get:
        with pytest.raises(ValueError, match='Invalid search query'): private.search('t','a/b',query)
        get.assert_not_called()


@pytest.mark.parametrize('bad', [[], {}, {'visibility':'public','project_id':42,'records':[]}, {'visibility':'private','project_id':True,'records':[]}, {'visibility':'private','project_id':43,'records':[]}, {'visibility':'private','project_id':42,'records':{}}, {'visibility':'private','project_id':42,'records':[None]*1001}])
def test_reject_invalid_snapshot(bad):
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(json.dumps(bad).encode())}
    with patch('auth.gitlab_private._get', return_value={'id':42}), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        with pytest.raises(ValueError, match='Invalid snapshot'): private.search('t','a/b','secret')


@pytest.mark.parametrize('record', [None, {}, {'iid':True, 'project_id':42, 'title':'secret','body':''}, {'iid':1, 'project_id':True, 'title':'secret','body':''}, {'iid':1, 'project_id':43, 'title':'secret','body':''}, {'iid':1, 'project_id':42, 'title':None,'body':''}, {'iid':1, 'project_id':42, 'title':'secret','body':None}])
def test_reject_invalid_stored_record(record):
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot([record])).encode())}
    with patch('auth.gitlab_private._get', return_value={'id':42}), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        with pytest.raises(ValueError, match='Invalid snapshot'): private.search('t','a/b','secret')


def test_oversized_stored_snapshot_is_rejected():
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(b'1234')}
    with patch('auth.gitlab_private.MAX_BYTES',3), patch('auth.gitlab_private._get', return_value={'id':42}), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        with pytest.raises(ValueError, match='Snapshot too large'): private.search('t','a/b','secret')


def test_nonmatching_record_does_not_query_original():
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot()).encode())}
    with patch('auth.gitlab_private._get', return_value={'id':42}) as get, patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        assert private.search('viewer','a/b','missing') == []
    get.assert_called_once_with('viewer','projects/a%2Fb')


def test_search_limits_verified_results():
    items=[private._record(mr(iid=n),42) for n in range(1,32)]
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot(items)).encode())}
    with patch('auth.gitlab_private._get', side_effect=[{'id':42}]+[mr(iid=n) for n in range(1,31)]) as get, patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        assert private.search('viewer','a/b','secret') == items[:30]
        assert get.call_count == 31


@pytest.mark.parametrize('project_id', [0, -1, True, '42', None])
def test_invalid_snapshot_key(project_id):
    with pytest.raises(ValueError, match='Invalid project'): private._key(project_id)


def test_collection_aborts_on_provider_failure_before_writing():
    page = [mr(iid=n) for n in range(1,101)]
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},page,http_error(500)]), patch('auth.gitlab_private.clients') as clients:
        with pytest.raises(HTTPError): private.collect('t','a/b')
        clients.assert_not_called()


def test_short_pages_do_not_end_collection():
    s3=Mock()
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},[mr(iid=1)],[mr(iid=2)],[]]), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        assert private.collect('t','a/b') == 2
    assert len(json.loads(s3.put_object.call_args.kwargs['Body'])['records']) == 2


def test_missing_body_field_fails_closed():
    item=mr()
    del item['description']
    with pytest.raises(ValueError, match='Invalid merge request'): private._record(item,42)


@pytest.mark.parametrize('url', ['http://gitlab.com/a', 'https://evil.example/a', 'https://gitlab.com.evil.example/a', 'https://user@gitlab.com/a', 'https://gitlab.com:443/a'])
def test_reject_untrusted_mr_url(url):
    with pytest.raises(ValueError, match='Invalid merge request URL'): private._record(mr(web_url=url),42)


def test_public_operations_never_read_private_snapshot_objects():
    import storage
    s3, db = Mock(), Mock()
    s3.get_paginator.return_value.paginate.return_value = [{'Contents':[{'Key':'private/gitlab/42.json'}]}]
    s3.get_object.side_effect = AssertionError('Private object must not be read')
    with patch('storage.clients', return_value=(s3,db)):
        assert storage.search('secret') == []
        assert storage.cleanup_unavailable() == 0
        assert storage.remove_public_source('gitlab:a/b') == 0
    s3.get_object.assert_not_called()
    s3.delete_object.assert_not_called()


def test_record_preserves_content_and_normalizes_null_description():
    assert private._record(mr(), 42) == snapshot()['records'][0]
    assert private._record(mr(description=None),42)['body'] == ''


@pytest.mark.parametrize('query', ['ab','a'*100])
def test_search_query_accepts_inclusive_boundaries(query):
    s3=Mock(); s3.get_object.side_effect=client_error('NoSuchKey')
    with patch('auth.gitlab_private._get', return_value={'id':42}), patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        assert private.search('t','a/b',query) == []


def test_search_continues_after_unmatched_and_inaccessible_records():
    items=[{**snapshot()['records'][0], 'iid':1, 'title':'other', 'body':''}, {**snapshot()['records'][0], 'iid':2}, {**snapshot()['records'][0], 'iid':3}]
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot(items)).encode())}
    with patch('auth.gitlab_private._get', side_effect=[{'id':42},http_error(403),mr(iid=3)]) as get, patch('auth.gitlab_private.clients', return_value=(s3,Mock())):
        assert private.search('viewer','a/b','secret') == [items[2]]
        assert [c.args[1] for c in get.call_args_list] == ['projects/a%2Fb','projects/42/merge_requests/2','projects/42/merge_requests/3']


def test_read_limits_apply_at_exact_boundary():
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self,*args): self.close()
    response = Response(b'null')
    with patch('auth.gitlab_private.MAX_BYTES',4), patch('auth.gitlab_private.open_url',return_value=response):
        assert private._get('token','projects/42') is None
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(b'{}')}
    with patch('auth.gitlab_private.MAX_BYTES',2), patch('auth.gitlab_private._get',return_value={'id':42}), patch('auth.gitlab_private.clients',return_value=(s3,Mock())):
        with pytest.raises(ValueError,match='^Invalid snapshot$'): private.search('t','a/b','ab')


def test_snapshot_record_count_boundary():
    items=[{**snapshot()['records'][0], 'iid':n, 'title':'other', 'body':''} for n in range(1,1001)]
    body=io.BytesIO(json.dumps(snapshot(items)).encode())
    s3=Mock();s3.get_object.return_value={'Body':body}
    with patch('auth.gitlab_private._get',return_value={'id':42}), patch('auth.gitlab_private.clients',return_value=(s3,Mock())):
        assert private.search('t','a/b','secret') == []


def test_network_and_storage_reads_are_bounded():
    class Response:
        def __init__(self): self.read=Mock(return_value=b'null')
        def __enter__(self): return self
        def __exit__(self,*args): pass
    response=Response()
    with patch('auth.gitlab_private.open_url',return_value=response):
        assert private._get('t','projects/42') is None
    response.read.assert_called_once_with(private.MAX_BYTES+1)
    body=Mock();body.read.return_value=json.dumps(snapshot([])).encode()
    s3=Mock();s3.get_object.return_value={'Body':body}
    with patch('auth.gitlab_private._get',return_value={'id':42}), patch('auth.gitlab_private.clients',return_value=(s3,Mock())):
        assert private.search('t','a/b','secret') == []
    body.read.assert_called_once_with(private.MAX_BYTES+1)
    body.close.assert_called_once_with()


def test_unauthorized_original_propagates_for_session_refresh():
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot()).encode())}
    with patch('auth.gitlab_private._get',side_effect=[{'id':42},http_error(401)]), patch('auth.gitlab_private.clients',return_value=(s3,Mock())):
        with pytest.raises(HTTPError) as error: private.search('viewer','a/b','secret')
    assert error.value.code == 401


def test_snapshot_stream_closes_when_read_fails():
    body=Mock();body.read.side_effect=OSError('failure')
    s3=Mock();s3.get_object.return_value={'Body':body}
    with patch('auth.gitlab_private._get',return_value={'id':42}), patch('auth.gitlab_private.clients',return_value=(s3,Mock())):
        with pytest.raises(OSError): private.search('t','a/b','secret')
    body.close.assert_called_once_with()


@pytest.mark.parametrize('changed', [{'description':'new body'}, {'updated_at':'2027-01-01'}, {'web_url':'https://gitlab.com/a/b/-/merge_requests/2'}])
def test_search_hides_changed_source_fields(changed):
    s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(json.dumps(snapshot()).encode())}
    with patch('auth.gitlab_private._get',side_effect=[{'id':42},mr(**changed)]), patch('auth.gitlab_private.clients',return_value=(s3,Mock())):
        assert private.search('viewer','a/b','secret') == []

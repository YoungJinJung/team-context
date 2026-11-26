import io
import json
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest
from botocore.exceptions import ClientError
from auth import github_private as private


def pull(number=1, **changes):
    return {'number':number, 'base':{'repo':{'id':42}}, 'title':'Secret title', 'html_url':f'https://github.com/a/b/pull/{number}', 'updated_at':'2026-01-01', 'body':'Secret body', **changes}


def snapshot(records=None):
    return {'visibility':'private', 'repository_id':42, 'records':[{'number':1,'repository_id':42,'title':'Secret title','url':'https://github.com/a/b/pull/1','updated_at':'2026-01-01','body':'Secret body'}] if records is None else records}


def http_error(code):
    return HTTPError('https://api.github.com',code,'private',{},None)


def client_error(code):
    return ClientError({'Error':{'Code':code,'Message':'private'}},'GetObject')


def s3_with(data):
    s3=Mock()
    s3.get_object.return_value={'Body':io.BytesIO(json.dumps(data).encode())}
    return s3


def test_request_uses_fixed_host_bearer_and_size_limit():
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self,*args): self.close()
    with patch('auth.github_private.open_url',return_value=Response(b'{"id":42,"full_name":"a/b"}')) as send:
        assert private._repository('token','a/b') == (42,'a/b')
    req=send.call_args.args[0]
    assert req.full_url == 'https://api.github.com/repos/a/b'
    assert req.get_header('Authorization') == 'Bearer token' and req.get_header('Accept') == 'application/vnd.github+json'
    assert req.get_header('User-agent') == 'team-context-mvp'
    with patch('auth.github_private.MAX_BYTES',3), patch('auth.github_private.open_url',return_value=Response(b'1234')):
        with pytest.raises(ValueError,match='GitHub response too large'): private._get('t','repos/a/b')


@pytest.mark.parametrize('name',['','../a','https://evil.example/a',None,'a/b?x=1'])
def test_reject_invalid_repository_before_network(name):
    with patch('auth.github_private._get') as get:
        with pytest.raises(ValueError): private._repository('token',name)
        get.assert_not_called()


@pytest.mark.parametrize('value',[None,[],{}, {'id':0,'full_name':'a/b'}, {'id':True,'full_name':'a/b'}, {'id':'42','full_name':'a/b'}, {'id':42}, {'id':42,'full_name':'../bad'}])
def test_reject_invalid_repository_response(value):
    with patch('auth.github_private._get',return_value=value):
        with pytest.raises(ValueError): private._repository('t','a/b')


@pytest.mark.parametrize('value',[None,{},pull(number=0),pull(number=True),pull(base=None),pull(base={'repo':None}),pull(base={'repo':{'id':43}}),pull(base={'repo':{'id':True}}),pull(title=None),pull(html_url=None),pull(updated_at=None),pull(body=5)])
def test_reject_invalid_record(value):
    with pytest.raises(ValueError,match='Invalid pull request'): private._record(value,42,'a/b')


@pytest.mark.parametrize('url',['http://github.com/a/b/pull/1','https://evil.example/a/b/pull/1','https://github.com/other/b/pull/1','https://github.com/a/b/pull/2','https://github.com/a/b/pull/1?x=1','https://github.com/a/b/pull/1#frag'])
def test_reject_untrusted_pull_urls(url):
    with pytest.raises(ValueError,match='Invalid pull request URL'): private._record(pull(html_url=url),42,'a/b')


def test_collect_commits_complete_snapshot_only():
    s3=Mock()
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},[pull()],[]]) as get, patch('auth.github_private.clients',return_value=(s3,Mock())):
        assert private.collect('token','a/b') == 1
    assert [call.args for call in get.call_args_list] == [('token','repos/a/b'),('token','repos/a/b/pulls?state=all&per_page=100&page=1'),('token','repos/a/b/pulls?state=all&per_page=100&page=2')]
    call=s3.put_object.call_args.kwargs
    assert call['Bucket']=='team-context-records' and call['Key']=='private/github/42.json' and call['ContentType']=='application/json'
    assert json.loads(call['Body']) == snapshot()


def test_collection_pagination_and_repeated_numbers():
    first=[pull(n) for n in range(1,101)]
    s3=Mock()
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},first,[pull(101)],[]]), patch('auth.github_private.clients',return_value=(s3,Mock())):
        assert private.collect('t','a/b') == 101
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},first,[pull(1)]]), patch('auth.github_private.clients') as clients:
        with pytest.raises(ValueError,match='Repeated pull request'): private.collect('t','a/b')
        clients.assert_not_called()


@pytest.mark.parametrize('batch',[None,{},[pull()]*101])
def test_bad_pages_do_not_write(batch):
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},batch]), patch('auth.github_private.clients') as clients:
        with pytest.raises(ValueError,match='Invalid pull request page'): private.collect('t','a/b')
        clients.assert_not_called()


def test_page_limit_provider_error_and_oversize_never_write():
    pages=[[pull(page*100+n+1) for n in range(100)] for page in range(10)]
    for batches in ([*pages,[pull(1001)]],[pages[0],http_error(500)]):
        with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},*batches]), patch('auth.github_private.clients') as clients:
            with pytest.raises((ValueError,HTTPError)): private.collect('t','a/b')
            clients.assert_not_called()
    with patch('auth.github_private.MAX_BYTES',10), patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},[pull()],[]]), patch('auth.github_private.clients') as clients:
        with pytest.raises(ValueError,match='Snapshot too large'): private.collect('t','a/b')
        clients.assert_not_called()


def test_search_uses_requester_for_repository_and_each_record():
    s3=s3_with(snapshot())
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},pull()]) as get, patch('auth.github_private.clients',return_value=(s3,Mock())):
        assert private.search('viewer','a/b',' SECRET ') == snapshot()['records']
    assert [call.args for call in get.call_args_list] == [('viewer','repos/a/b'),('viewer','repos/a/b/pulls/1')]
    s3.get_object.assert_called_once_with(Bucket='team-context-records',Key='private/github/42.json')


@pytest.mark.parametrize('code',[403,404,410])
def test_revoked_or_deleted_record_is_hidden(code):
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},http_error(code)]), patch('auth.github_private.clients',return_value=(s3_with(snapshot()),Mock())):
        assert private.search('viewer','a/b','secret') == []


def test_repo_denial_fails_before_storage_and_changed_content_is_hidden():
    with patch('auth.github_private._get',side_effect=http_error(404)), patch('auth.github_private.clients') as clients:
        with pytest.raises(HTTPError): private.search('other','a/b','secret')
        clients.assert_not_called()
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},pull(title='changed')]), patch('auth.github_private.clients',return_value=(s3_with(snapshot()),Mock())):
        assert private.search('viewer','a/b','secret') == []
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},http_error(500)]), patch('auth.github_private.clients',return_value=(s3_with(snapshot()),Mock())):
        with pytest.raises(HTTPError): private.search('viewer','a/b','secret')


def test_missing_snapshot_and_storage_failure():
    s3=Mock()
    with patch('auth.github_private._get',return_value={'id':42,'full_name':'a/b'}), patch('auth.github_private.clients',return_value=(s3,Mock())):
        s3.get_object.side_effect=client_error('NoSuchKey')
        assert private.search('viewer','a/b','secret') == []
        s3.get_object.side_effect=client_error('AccessDenied')
        with pytest.raises(ClientError): private.search('viewer','a/b','secret')


@pytest.mark.parametrize('query',[None,'',' x ','a'*101])
def test_invalid_query_before_network(query):
    with patch('auth.github_private._get') as get:
        with pytest.raises(ValueError,match='Invalid search query'): private.search('t','a/b',query)
        get.assert_not_called()


@pytest.mark.parametrize('bad',[[],{}, {'visibility':'public','repository_id':42,'records':[]}, {'visibility':'private','repository_id':True,'records':[]}, {'visibility':'private','repository_id':43,'records':[]}, {'visibility':'private','repository_id':42,'records':{}}, {'visibility':'private','repository_id':42,'records':[None]*1001}])
def test_invalid_snapshot_rejected(bad):
    with patch('auth.github_private._get',return_value={'id':42,'full_name':'a/b'}), patch('auth.github_private.clients',return_value=(s3_with(bad),Mock())):
        with pytest.raises(ValueError,match='Invalid snapshot'): private.search('t','a/b','secret')


@pytest.mark.parametrize('record',[None,{}, {'number':True,'repository_id':42,'title':'secret','body':''}, {'number':1,'repository_id':True,'title':'secret','body':''}, {'number':1,'repository_id':43,'title':'secret','body':''}, {'number':1,'repository_id':42,'title':None,'body':''}, {'number':1,'repository_id':42,'title':'secret','body':None}])
def test_bad_stored_record_is_rejected(record):
    with patch('auth.github_private._get',return_value={'id':42,'full_name':'a/b'}), patch('auth.github_private.clients',return_value=(s3_with(snapshot([record])),Mock())):
        with pytest.raises(ValueError,match='Invalid snapshot'): private.search('t','a/b','secret')


def test_nonmatching_record_does_not_call_original():
    with patch('auth.github_private._get',return_value={'id':42,'full_name':'a/b'}) as get, patch('auth.github_private.clients',return_value=(s3_with(snapshot()),Mock())):
        assert private.search('viewer','a/b','missing') == []
    get.assert_called_once_with('viewer','repos/a/b')


def test_verified_results_are_limited_to_thirty():
    records=[private._record(pull(n),42,'a/b') for n in range(1,32)]
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'}]+[pull(n) for n in range(1,31)]) as get, patch('auth.github_private.clients',return_value=(s3_with(snapshot(records)),Mock())):
        assert private.search('viewer','a/b','secret') == records[:30]
        assert get.call_count == 31


@pytest.mark.parametrize('value',[None,0,-1,True,'42'])
def test_invalid_keys(value):
    with pytest.raises(ValueError,match='Invalid repository'): private._key(value)


def test_record_preserves_expected_fields_and_none_body():
    assert private._record(pull(),42,'a/b') == snapshot()['records'][0]
    assert private._record(pull(body=None),42,'a/b')['body'] == ''
    assert private._record(pull(html_url='https://github.com/A/B/pull/1'),42,'a/b')['url'] == 'https://github.com/A/B/pull/1'


def test_search_valid_boundaries_and_nonmatches_do_not_stop_later_records():
    first={**snapshot()['records'][0], 'number':1,'title':'no match','body':'other'}
    second={**snapshot()['records'][0], 'number':2,'url':'https://github.com/a/b/pull/2'}
    for query in ('ab','a'*100):
        with patch('auth.github_private._get',return_value={'id':42,'full_name':'a/b'}), patch('auth.github_private.clients',return_value=(s3_with(snapshot([])),Mock())):
            assert private.search('viewer','a/b',query)==[]
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},pull(2)]), patch('auth.github_private.clients',return_value=(s3_with(snapshot([first,second])),Mock())):
        assert private.search('viewer','a/b','secret') == [second]
    first={**snapshot()['records'][0]}
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},http_error(403),pull(2)]), patch('auth.github_private.clients',return_value=(s3_with(snapshot([first,second])),Mock())):
        assert private.search('viewer','a/b','secret') == [second]


def test_short_pages_are_not_assumed_final():
    s3=Mock()
    with patch('auth.github_private._get',side_effect=[{'id':42,'full_name':'a/b'},[pull(1)],[pull(2)],[]]), patch('auth.github_private.clients',return_value=(s3,Mock())):
        assert private.collect('t','a/b')==2
    assert len(json.loads(s3.put_object.call_args.kwargs['Body'])['records'])==2


def test_snapshot_bytes_and_record_count_limits():
    repo={'id':42,'full_name':'a/b'}
    s3=Mock(); s3.get_object.return_value={'Body':io.BytesIO(b'1234')}
    with patch('auth.github_private.MAX_BYTES',3), patch('auth.github_private._get',return_value=repo), patch('auth.github_private.clients',return_value=(s3,Mock())):
        with pytest.raises(ValueError,match='Snapshot too large'): private.search('t','a/b','secret')
    rec=snapshot()['records'][0]
    with patch('auth.github_private._get',return_value=repo), patch('auth.github_private.clients',return_value=(s3_with(snapshot([rec]*1000)),Mock())):
        assert private.search('t','a/b','not present')==[]
    with patch('auth.github_private._get',return_value=repo), patch('auth.github_private.clients',return_value=(s3_with(snapshot([rec]*1001)),Mock())):
        with pytest.raises(ValueError,match='Invalid snapshot'): private.search('t','a/b','secret')


def test_source_response_read_is_bounded():
    response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.read.return_value=b'{}'
    with patch('auth.github_private.open_url',return_value=response):
        assert private._get('token','repos/a/b')=={}
    response.read.assert_called_once_with(private.MAX_BYTES+1)

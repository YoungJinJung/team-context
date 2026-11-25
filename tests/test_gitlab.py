import io
import json
from unittest.mock import patch
import pytest
from connectors import gitlab


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


@pytest.mark.parametrize('project', ['', 'group', '../foo', 'group/../x', 'https://gitlab.com/g/p', 'group/p?q=1', None, 5])
def test_invalid_project(project):
    with pytest.raises(ValueError, match='group/project'):
        gitlab.connection_id({'project': project})


def test_missing_project():
    with pytest.raises(ValueError):
        gitlab.connection_id({})


@patch('connectors.gitlab.open_url')
def test_collect_maps_records_and_encodes_nested_project(urlopen):
    merges = [{'iid': 12, 'title': 'Fix bug', 'web_url': 'https://gitlab.com/Group/Sub/Repo/-/merge_requests/12', 'updated_at': 'today', 'description': 'details'}, {'iid': 13, 'title': 'Empty', 'web_url': 'https://gitlab.com/Group/Sub/Repo/-/merge_requests/13', 'updated_at': 'later', 'description': None}]
    urlopen.return_value = Response(json.dumps(merges).encode())
    config = {'project': 'Group/Sub/Repo'}
    assert gitlab.connection_id(config) == 'gitlab:group/sub/repo'
    assert gitlab.collect(config) == [{'id': 'gitlab:group/sub/repo:mr:12', 'visibility': 'public', 'type': 'merge_request', 'title': 'Fix bug', 'url': merges[0]['web_url'], 'updated_at': 'today', 'body': 'details'}, {'id': 'gitlab:group/sub/repo:mr:13', 'visibility': 'public', 'type': 'merge_request', 'title': 'Empty', 'url': merges[1]['web_url'], 'updated_at': 'later', 'body': ''}]
    req = urlopen.call_args.args[0]
    assert req.full_url == 'https://gitlab.com/api/v4/projects/Group%2FSub%2FRepo/merge_requests?scope=all&per_page=100&page=1'
    assert req.get_header('Accept') == 'application/json'
    assert req.get_header('User-agent') == 'team-context-mvp'
    assert urlopen.call_args.kwargs == {}


def merge(i):
    return {'iid': i, 'title': str(i), 'web_url': f'https://gitlab.com/a/b/-/merge_requests/{i}', 'updated_at': 'today', 'description': None}


def test_paginates_using_fixed_api_urls():
    with patch('connectors.gitlab.open_url', side_effect=[Response(json.dumps([merge(i) for i in range(1, 101)]).encode()), Response(json.dumps([merge(101)]).encode())]) as open_url:
        records = gitlab.collect({'project': 'a/b'})
    assert len(records) == 101 and records[-1]['id'] == 'gitlab:a/b:mr:101'
    assert open_url.call_args_list[1].args[0].full_url == 'https://gitlab.com/api/v4/projects/a%2Fb/merge_requests?scope=all&per_page=100&page=2'


def test_empty_page_and_partial_failure():
    with patch('connectors.gitlab.open_url', return_value=Response(b'[]')):
        assert gitlab.collect({'project': 'a/b'}) == []
    with patch('connectors.gitlab.open_url', side_effect=[Response(json.dumps([merge(i) for i in range(100)]).encode()), OSError('rate limit')]):
        with pytest.raises(OSError): gitlab.collect({'project': 'a/b'})


def test_duplicate_and_invalid_page_fail():
    with patch('connectors.gitlab.open_url', return_value=Response(json.dumps([merge(1), merge(1)]).encode())):
        with pytest.raises(ValueError, match='Repeated'): gitlab.collect({'project': 'a/b'})
    with patch('connectors.gitlab.open_url', return_value=Response(b'{}')):
        with pytest.raises(ValueError, match='Invalid GitLab page'): gitlab.collect({'project': 'a/b'})


def test_full_page_limit_fails():
    count = iter(range(100))
    def page(request):
        first = next(count) * 100
        return Response(json.dumps([merge(first + i) for i in range(100)]).encode())
    with patch('connectors.gitlab.open_url', side_effect=page):
        with pytest.raises(ValueError, match='exceeds 100 pages'): gitlab.collect({'project': 'a/b'})


def test_rate_limit_on_next_page_fails_without_partial_results():
    from urllib.error import HTTPError
    with patch('connectors.gitlab.open_url', side_effect=[Response(json.dumps([merge(i) for i in range(100)]).encode()), HTTPError('https://gitlab.com', 429, 'rate limited', {}, None)]):
        with pytest.raises(HTTPError): gitlab.collect({'project': 'a/b'})

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


@patch('connectors.gitlab.urllib.request.urlopen')
def test_collect_maps_records_and_encodes_nested_project(urlopen):
    merges = [{'iid': 12, 'title': 'Fix bug', 'web_url': 'https://gitlab.com/Group/Sub/Repo/-/merge_requests/12', 'updated_at': 'today', 'description': 'details'}, {'iid': 13, 'title': 'Empty', 'web_url': 'https://gitlab.com/Group/Sub/Repo/-/merge_requests/13', 'updated_at': 'later', 'description': None}]
    urlopen.return_value = Response(json.dumps(merges).encode())
    config = {'project': 'Group/Sub/Repo'}
    assert gitlab.connection_id(config) == 'gitlab:group/sub/repo'
    assert gitlab.collect(config) == [{'id': 'gitlab:group/sub/repo:mr:12', 'visibility': 'public', 'type': 'merge_request', 'title': 'Fix bug', 'url': merges[0]['web_url'], 'updated_at': 'today', 'body': 'details'}, {'id': 'gitlab:group/sub/repo:mr:13', 'visibility': 'public', 'type': 'merge_request', 'title': 'Empty', 'url': merges[1]['web_url'], 'updated_at': 'later', 'body': ''}]
    req = urlopen.call_args.args[0]
    assert req.full_url == 'https://gitlab.com/api/v4/projects/Group%2FSub%2FRepo/merge_requests?scope=all&per_page=30'
    assert req.get_header('Accept') == 'application/json'
    assert req.get_header('User-agent') == 'team-context-mvp'
    assert urlopen.call_args.kwargs == {'timeout': 15}

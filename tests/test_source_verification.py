import io
import json
from unittest.mock import patch
from urllib.error import HTTPError
import pytest
from source_verification import current


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


GITHUB = {'id': 'github:owner/repo:pr:42', 'visibility': 'public', 'type': 'pull_request', 'title': 'Change', 'url': 'https://github.com/owner/repo/pull/42', 'updated_at': 'today', 'body': 'text'}
GITHUB_ITEM = {'number': 42, 'title': 'Change', 'html_url': GITHUB['url'], 'updated_at': 'today', 'body': 'text'}
GITLAB = {'id': 'gitlab:group/sub/repo:mr:17', 'visibility': 'public', 'type': 'merge_request', 'title': 'Change', 'url': 'https://gitlab.com/group/sub/repo/-/merge_requests/17', 'updated_at': 'today', 'body': ''}
GITLAB_ITEM = {'iid': 17, 'title': 'Change', 'web_url': GITLAB['url'], 'updated_at': 'today', 'description': None}


@pytest.mark.parametrize('record,item,url', [(GITHUB, GITHUB_ITEM, 'https://api.github.com/repos/owner/repo/pulls/42'), (GITLAB, GITLAB_ITEM, 'https://gitlab.com/api/v4/projects/group%2Fsub%2Frepo/merge_requests/17')])
def test_current_record_checks_expected_source(record, item, url):
    with patch('source_verification.open_url', return_value=Response(json.dumps(item).encode())) as open_url:
        assert current(record) is True
    req = open_url.call_args.args[0]
    assert req.full_url == url
    assert req.get_header('Accept') == 'application/json'
    assert req.get_header('User-agent') == 'team-context-mvp'


@pytest.mark.parametrize('field,new', [('title', 'changed'), ('body', 'changed'), ('updated_at', 'changed'), ('url', 'https://evil.example'), ('type', 'merge_request'), ('visibility', 'private')])
def test_modified_indexed_record_is_hidden(field, new):
    with patch('source_verification.open_url', return_value=Response(json.dumps(GITHUB_ITEM).encode())):
        assert current({**GITHUB, field: new}) is False


@pytest.mark.parametrize('identifier', [None, 10, '', 'github:../secret:pr:1', 'github:owner/repo:pr:0', 'github:owner/repo:pr:1?x=evil', 'linear:x', 'gitlab:group/../repo:mr:1'])
def test_invalid_ids_never_make_network_requests(identifier):
    with patch('source_verification.open_url') as open_url:
        assert current({'id': identifier}) is False
    open_url.assert_not_called()


@pytest.mark.parametrize('code', [404, 410])
def test_removed_or_private_is_hidden(code):
    with patch('source_verification.open_url', side_effect=HTTPError('https://api.github.com', code, 'missing', {}, None)):
        assert current(GITHUB) is False


def test_provider_outage_fails_closed():
    with patch('source_verification.open_url', side_effect=HTTPError('https://api.github.com', 429, 'rate limit', {}, None)):
        with pytest.raises(HTTPError): current(GITHUB)


def test_number_must_match():
    with patch('source_verification.open_url', return_value=Response(json.dumps({**GITHUB_ITEM, 'number': 43}).encode())):
        assert current(GITHUB) is False


def test_extra_metadata_is_not_exposed():
    with patch('source_verification.open_url', return_value=Response(json.dumps(GITHUB_ITEM).encode())):
        assert current({**GITHUB, 'unexpected': 'private'}) is False


def test_status_distinguishes_unavailable_changed_and_invalid():
    from source_verification import status
    with patch('source_verification.open_url', side_effect=HTTPError('https://api.github.com', 404, 'missing', {}, None)):
        assert status(GITHUB) == 'unavailable'
    with patch('source_verification.open_url', return_value=Response(json.dumps({**GITHUB_ITEM, 'title': 'new'}).encode())):
        assert status(GITHUB) == 'changed'
    assert status({'id': 'untrusted'}) == 'invalid'

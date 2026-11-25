import io
import json
from unittest.mock import patch
import pytest
from auth import linear_preview as preview

TEAM = '9cfb482a-81e3-4154-b5b9-2c805e70a02d'

class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def test_teams_graphql_request_and_result():
    data = {'data': {'teams': {'nodes': [{'id': TEAM, 'name': 'Platform'}]}}}
    with patch('auth.linear_preview.open_url', return_value=Response(json.dumps(data).encode())) as urlopen:
        assert preview.teams('short-lived') == [{'id': TEAM, 'name': 'Platform'}]
    req = urlopen.call_args.args[0]
    assert req.full_url == 'https://api.linear.app/graphql'
    assert req.get_method() == 'POST'
    assert req.get_header('Authorization') == 'Bearer short-lived'
    assert req.get_header('Content-type') == 'application/json'
    assert json.loads(req.data) == {'query': preview.TEAMS_QUERY, 'variables': {}}
    assert urlopen.call_args.kwargs == {}


def test_issue_query_targets_selected_team():
    issue = {'id': 'id', 'identifier': 'ABC-1', 'title': 'Fix', 'url': 'https://linear.app/a/issue/ABC-1', 'updatedAt': 'today'}
    data = {'data': {'team': {'issues': {'nodes': [issue]}}}}
    with patch('auth.linear_preview.open_url', return_value=Response(json.dumps(data).encode())) as urlopen:
        assert preview.issues('token', TEAM) == [{'id': 'id', 'identifier': 'ABC-1', 'title': 'Fix', 'url': issue['url'], 'updated_at': 'today'}]
    assert json.loads(urlopen.call_args.args[0].data) == {'query': preview.ISSUES_QUERY, 'variables': {'teamId': TEAM}}


@pytest.mark.parametrize('team', ['', 'bad', '../x', None, 1, TEAM.upper()])
def test_invalid_team_rejected_before_network(team):
    with patch('auth.linear_preview.open_url') as urlopen:
        with pytest.raises(ValueError, match='Invalid team ID'): preview.issues('token', team)
        urlopen.assert_not_called()


@pytest.mark.parametrize('data', [{'errors': [{'message': 'denied'}], 'data': {'teams': {'nodes': []}}}, {}, {'data': None}])
def test_graphql_errors_fail_closed(data):
    with patch('auth.linear_preview.open_url', return_value=Response(json.dumps(data).encode())):
        with pytest.raises(ValueError, match='Linear query failed'): preview.teams('token')


def test_missing_team_fails_closed():
    with patch('auth.linear_preview.open_url', return_value=Response(b'{"data":{"team":null}}')):
        with pytest.raises(ValueError, match='Team unavailable'): preview.issues('token', TEAM)

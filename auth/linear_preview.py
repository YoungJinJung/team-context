"""Read-only Linear previews. Private records are never persisted."""
import json
import urllib.request
from auth.transport import open_url
import uuid

TEAMS_QUERY = 'query Teams { teams(first: 50) { nodes { id name } } }'
ISSUES_QUERY = 'query Team($teamId: String!) { team(id: $teamId) { issues(first: 30) { nodes { id identifier title url updatedAt } } } }'


def _query(token, query, variables):
    body = json.dumps({'query': query, 'variables': variables}).encode()
    request = urllib.request.Request('https://api.linear.app/graphql', data=body, headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
    with open_url(request) as response:
        result = json.load(response)
    if result.get('errors') or not isinstance(result.get('data'), dict):
        raise ValueError('Linear query failed')
    return result['data']


def teams(token):
    data = _query(token, TEAMS_QUERY, {})
    return [{'id': team['id'], 'name': team['name']} for team in data['teams']['nodes']]


def issues(token, team_id):
    try:
        if str(uuid.UUID(team_id)) != team_id:
            raise ValueError('Invalid team ID')
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid team ID') from None
    data = _query(token, ISSUES_QUERY, {'teamId': team_id})
    if data.get('team') is None:
        raise ValueError('Team unavailable')
    return [{'id': item['id'], 'identifier': item['identifier'], 'title': item['title'], 'url': item['url'], 'updated_at': item['updatedAt']} for item in data['team']['issues']['nodes']]

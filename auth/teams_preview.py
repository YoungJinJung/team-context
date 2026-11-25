"""Read-only Teams and channel previews without persisting private records."""
import json
import uuid
import urllib.request
from auth.transport import open_url


def _get(token, path):
    request = urllib.request.Request('https://graph.microsoft.com/v1.0/' + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    with open_url(request) as response:
        data = json.load(response)
    if not isinstance(data, dict) or not isinstance(data.get('value'), list):
        raise ValueError('Invalid Graph response')
    return [{'id': item['id'], 'name': item['displayName']} for item in data['value']]


def teams(token):
    return _get(token, 'me/joinedTeams')


def channels(token, team_id):
    try:
        if str(uuid.UUID(team_id)) != team_id:
            raise ValueError('Invalid team ID')
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid team ID') from None
    return _get(token, 'teams/' + team_id + '/channels?$select=id,displayName')

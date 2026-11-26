"""Preview Atlassian sites without storing their private contents."""
import json
import urllib.request
from auth.transport import open_url

MAX_BYTES = 1_048_576


def sites(token):
    request = urllib.request.Request('https://api.atlassian.com/oauth/token/accessible-resources',
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    with open_url(request) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('Site response too large')
    data = json.loads(body)
    if not isinstance(data, list) or len(data) > 1000:
        raise ValueError('Invalid site response')
    result = []
    for item in data:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ('id', 'name')) or not isinstance(item.get('scopes'), list) or not all(isinstance(scope, str) for scope in item['scopes']):
            raise ValueError('Invalid site response')
        scopes = item['scopes']
        result.append({'id': item['id'], 'name': item['name'],
                       'jira': 'read:jira-work' in scopes,
                       'confluence': 'read:confluence-content.summary' in scopes})
    return result

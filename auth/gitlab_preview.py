"""Preview GitLab merge requests with the current user's delegated access."""
import json
import urllib.parse
import urllib.request
from auth.transport import open_url
from connectors.gitlab import connection_id


def merge_requests(token, project):
    connection_id({'project': project})
    encoded = urllib.parse.quote(project, safe='')
    request = urllib.request.Request(f'https://gitlab.com/api/v4/projects/{encoded}/merge_requests?scope=all&per_page=30&page=1', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': 'team-context-mvp'})
    with open_url(request) as response:
        items = json.load(response)
    if not isinstance(items, list) or len(items) > 30:
        raise ValueError('Invalid GitLab response')
    return [{'title': item['title'], 'url': item['web_url']} for item in items]

"""Preview GitHub pull requests with the current user's delegated access."""
import json
import urllib.parse
import urllib.request
from auth.transport import open_url
from connectors.github import connection_id


def pull_requests(token, repository):
    connection_id({'repository': repository})
    request = urllib.request.Request(f'https://api.github.com/repos/{repository}/pulls?state=all&per_page=30&page=1', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'team-context-mvp'})
    with open_url(request) as response:
        items = json.load(response)
    if not isinstance(items, list) or len(items) > 30:
        raise ValueError('Invalid GitHub response')
    return [{'title': item['title'], 'url': item['html_url']} for item in items]

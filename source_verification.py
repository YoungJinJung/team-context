"""Check that indexed public records still match an anonymously accessible source."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from auth.transport import open_url


def current(record):
    source_id = record.get('id')
    if not isinstance(source_id, str):
        return False
    github = re.fullmatch(r'github:([a-z0-9_.-]+/[a-z0-9_.-]+):pr:([1-9][0-9]*)', source_id)
    gitlab = re.fullmatch(r'gitlab:([a-z0-9_.-]+(?:/[a-z0-9_.-]+)+):mr:([1-9][0-9]*)', source_id)
    match = github or gitlab
    if not match or '..' in match.group(1):
        return False
    project, number = match.groups()
    if github:
        url = f'https://api.github.com/repos/{project}/pulls/{number}'
    else:
        url = f'https://gitlab.com/api/v4/projects/{urllib.parse.quote(project, safe="")}/merge_requests/{number}'
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'team-context-mvp'})
    try:
        with open_url(request) as response:
            item = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (404, 410):
            return False
        raise
    expected = {
        'id': source_id,
        'visibility': 'public',
        'type': 'pull_request' if github else 'merge_request',
        'title': item['title'],
        'url': item['html_url'] if github else item['web_url'],
        'updated_at': item['updated_at'],
        'body': (item.get('body') if github else item.get('description')) or '',
    }
    item_number = item['number'] if github else item['iid']
    return str(item_number) == number and record == expected

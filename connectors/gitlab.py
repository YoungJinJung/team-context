"""Read merge requests from a public GitLab.com project."""
import json
import re
import urllib.parse
import urllib.request

MANIFEST = {"id": "gitlab", "name": "GitLab", "category": "source_code", "fields": [{"name": "project", "label": "Project (group/project)", "required": True}]}


def connection_id(config):
    project = config.get('project', '')
    if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', project) or '..' in project:
        raise ValueError('Project must be in group/project format')
    return 'gitlab:' + project.lower()


def collect(config):
    connection_id(config)
    project = config['project']
    encoded = urllib.parse.quote(project, safe='')
    url = f'https://gitlab.com/api/v4/projects/{encoded}/merge_requests?scope=all&per_page=30'
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'team-context-mvp'})
    with urllib.request.urlopen(request, timeout=15) as response:
        merges = json.load(response)
    return [{'id': f"gitlab:{project.lower()}:mr:{item['iid']}", 'type': 'merge_request', 'title': item['title'], 'url': item['web_url'], 'updated_at': item['updated_at'], 'body': item.get('description') or ''} for item in merges]

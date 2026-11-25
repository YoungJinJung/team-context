"""Collect and revalidate a private GitLab.com project snapshot for one requester."""
from contextlib import closing
import json
import urllib.error
import urllib.parse
import urllib.request

from botocore.exceptions import ClientError
from auth.transport import open_url
from connectors.gitlab import connection_id
from storage import clients

MAX_BYTES = 2 * 1024 * 1024


def _get(token, path):
    request = urllib.request.Request('https://gitlab.com/api/v4/' + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': 'team-context-mvp'})
    with open_url(request) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('GitLab response too large')
    return json.loads(body)


def _positive(value):
    return type(value) is int and value > 0


def _project(token, project):
    connection_id({'project': project})
    data = _get(token, 'projects/' + urllib.parse.quote(project, safe=''))
    if not isinstance(data, dict) or not _positive(data.get('id')):
        raise ValueError('Invalid project')
    return data['id']


def _record(item, project_id):
    if not isinstance(item, dict) or not _positive(item.get('iid')) or item.get('project_id') != project_id or type(item.get('project_id')) is not int:
        raise ValueError('Invalid merge request')
    if not all(isinstance(item.get(field), str) for field in ('title', 'web_url', 'updated_at')) or 'description' not in item or not isinstance(item['description'], (str, type(None))):
        raise ValueError('Invalid merge request')
    parsed_url = urllib.parse.urlsplit(item['web_url'])
    if parsed_url.scheme != 'https' or parsed_url.netloc != 'gitlab.com':
        raise ValueError('Invalid merge request URL')
    return {'iid': item['iid'], 'project_id': project_id, 'title': item['title'], 'url': item['web_url'], 'updated_at': item['updated_at'], 'body': item.get('description') or ''}


def _key(project_id):
    if not _positive(project_id):
        raise ValueError('Invalid project')
    return f'private/gitlab/{project_id}.json'


def collect(token, project):
    project_id = _project(token, project)
    records = []
    seen = set()
    for page in range(1, 12):
        batch = _get(token, f'projects/{project_id}/merge_requests?scope=all&per_page=100&page={page}')
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError('Invalid merge request page')
        if not batch:
            body = json.dumps({'visibility': 'private', 'project_id': project_id, 'records': records}, ensure_ascii=False).encode()
            if len(body) > MAX_BYTES:
                raise ValueError('Snapshot too large')
            s3, _ = clients()
            s3.put_object(Bucket='team-context-records', Key=_key(project_id), Body=body, ContentType='application/json')
            return len(records)
        if page == 11:
            raise ValueError('Collection exceeds 10 pages')
        for item in batch:
            record = _record(item, project_id)
            if record['iid'] in seen:
                raise ValueError('Repeated merge request')
            seen.add(record['iid'])
            records.append(record)


def search(token, project, query):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
        raise ValueError('Invalid search query')
    query = query.strip().casefold()
    project_id = _project(token, project)
    s3, _ = clients()
    try:
        with closing(s3.get_object(Bucket='team-context-records', Key=_key(project_id))['Body']) as stream:
            body = stream.read(MAX_BYTES + 1)
    except ClientError as error:
        if error.response['Error']['Code'] == 'NoSuchKey':
            return []
        raise
    if len(body) > MAX_BYTES:
        raise ValueError('Snapshot too large')
    snapshot = json.loads(body)
    if not isinstance(snapshot, dict) or snapshot.get('visibility') != 'private' or type(snapshot.get('project_id')) is not int or snapshot['project_id'] != project_id or not isinstance(snapshot.get('records'), list) or len(snapshot['records']) > 1000:
        raise ValueError('Invalid snapshot')
    results = []
    for record in snapshot['records']:
        if not isinstance(record, dict) or not _positive(record.get('iid')) or type(record.get('project_id')) is not int or record['project_id'] != project_id or not isinstance(record.get('title'), str) or not isinstance(record.get('body'), str):
            raise ValueError('Invalid snapshot')
        if query not in (record['title'] + ' ' + record['body']).casefold():
            continue
        try:
            current = _record(_get(token, f"projects/{project_id}/merge_requests/{record['iid']}"), project_id)
        except urllib.error.HTTPError as error:
            if error.code in (403, 404, 410):
                continue
            raise
        if current == record:
            results.append(current)
            if len(results) == 30:
                return results
    return results

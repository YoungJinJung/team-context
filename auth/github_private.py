"""Collect and revalidate GitHub.com private PR snapshots for one requester."""
from contextlib import closing
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from botocore.exceptions import ClientError
from auth.transport import open_url
from connectors.github import connection_id
from storage import clients

MAX_BYTES = 2 * 1024 * 1024


def _get(token, path):
    request = urllib.request.Request('https://api.github.com/' + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'team-context-mvp'})
    with open_url(request) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('GitHub response too large')
    return json.loads(body)


def _positive(value):
    return type(value) is int and value > 0


def _repository(token, repository):
    connection_id({'repository': repository})
    data = _get(token, 'repos/' + repository)
    if not isinstance(data, dict) or not _positive(data.get('id')) or not isinstance(data.get('full_name'), str):
        raise ValueError('Invalid repository')
    connection_id({'repository': data['full_name']})
    return data['id'], data['full_name']


def _record(item, repository_id, repository):
    if not isinstance(item, dict) or not _positive(item.get('number')):
        raise ValueError('Invalid pull request')
    base = item.get('base')
    repo = base.get('repo') if isinstance(base, dict) else None
    if not isinstance(repo, dict) or type(repo.get('id')) is not int or repo['id'] != repository_id:
        raise ValueError('Invalid pull request')
    if not all(isinstance(item.get(field), str) for field in ('title', 'html_url', 'updated_at')) or 'body' not in item or not isinstance(item['body'], (str, type(None))):
        raise ValueError('Invalid pull request')
    url = urllib.parse.urlsplit(item['html_url'])
    if url.scheme != 'https' or url.netloc != 'github.com' or url.path.lower() != f"/{repository}/pull/{item['number']}".lower() or url.query or url.fragment:
        raise ValueError('Invalid pull request URL')
    return {'number': item['number'], 'repository_id': repository_id, 'title': item['title'], 'url': item['html_url'], 'updated_at': item['updated_at'], 'body': item['body'] or ''}


def _key(repository_id):
    if not _positive(repository_id):
        raise ValueError('Invalid repository')
    return f'private/github/{repository_id}.json'


def collect(token, repository):
    repository_id, name = _repository(token, repository)
    records, seen = [], set()
    for page in range(1, 12):
        batch = _get(token, f'repos/{name}/pulls?state=all&per_page=100&page={page}')
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError('Invalid pull request page')
        if not batch:
            body = json.dumps({'visibility': 'private', 'repository_id': repository_id, 'records': records}, ensure_ascii=False).encode()
            if len(body) > MAX_BYTES:
                raise ValueError('Snapshot too large')
            s3, _ = clients()
            s3.put_object(Bucket='team-context-records', Key=_key(repository_id), Body=body, ContentType='application/json')
            return len(records)
        if page == 11:
            raise ValueError('Collection exceeds 10 pages')
        for item in batch:
            record = _record(item, repository_id, name)
            if record['number'] in seen:
                raise ValueError('Repeated pull request')
            seen.add(record['number'])
            records.append(record)


def search(token, repository, query):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
        raise ValueError('Invalid search query')
    query = query.strip().casefold()
    repository_id, name = _repository(token, repository)
    s3, _ = clients()
    try:
        with closing(s3.get_object(Bucket='team-context-records', Key=_key(repository_id))['Body']) as stream:
            body = stream.read(MAX_BYTES + 1)
    except ClientError as error:
        if error.response['Error']['Code'] == 'NoSuchKey':
            return []
        raise
    if len(body) > MAX_BYTES:
        raise ValueError('Snapshot too large')
    snapshot = json.loads(body)
    if not isinstance(snapshot, dict) or snapshot.get('visibility') != 'private' or type(snapshot.get('repository_id')) is not int or snapshot['repository_id'] != repository_id or not isinstance(snapshot.get('records'), list) or len(snapshot['records']) > 1000:
        raise ValueError('Invalid snapshot')
    results = []
    for record in snapshot['records']:
        if not isinstance(record, dict) or not _positive(record.get('number')) or type(record.get('repository_id')) is not int or record['repository_id'] != repository_id or not isinstance(record.get('title'), str) or not isinstance(record.get('body'), str):
            raise ValueError('Invalid snapshot')
        if query not in (record['title'] + ' ' + record['body']).casefold():
            continue
        try:
            current = _record(_get(token, f"repos/{name}/pulls/{record['number']}"), repository_id, name)
        except urllib.error.HTTPError as error:
            if error.code in (403, 404, 410):
                continue
            raise
        if current == record:
            results.append(current)
            if len(results) == 30:
                return results
    return results

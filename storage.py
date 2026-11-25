"""Persist collected records and synchronization status to AWS-compatible services."""
import hashlib
import json
import re
import boto3
from source_verification import current, status


def clients():
    options = dict(endpoint_url='http://127.0.0.1:4568', region_name='us-east-1', aws_access_key_id='local', aws_secret_access_key='local')
    return boto3.client('s3', **options), boto3.client('dynamodb', **options)


def persist(connection, records):
    s3, dynamodb = clients()
    for record in records:
        key = hashlib.sha256(record['id'].encode()).hexdigest() + '.json'
        s3.put_object(Bucket='team-context-records', Key=key, Body=json.dumps(record, ensure_ascii=False).encode(), ContentType='application/json')
    dynamodb.put_item(TableName='team-context-sync', Item={'connection_id': {'S': connection}, 'count': {'N': str(len(records))}})


def search(query):
    query = query.strip().casefold()
    if len(query) < 2 or len(query) > 100:
        raise ValueError('Search query must contain 2 to 100 characters')
    s3, _ = clients()
    results = []
    pages = s3.get_paginator('list_objects_v2').paginate(Bucket='team-context-records')
    for page in pages:
        for item in page.get('Contents', []):
            body = s3.get_object(Bucket='team-context-records', Key=item['Key'])['Body'].read()
            record = json.loads(body)
            if record.get('visibility') != 'public':
                continue
            if query in (record.get('title', '') + ' ' + record.get('body', '')).casefold() and current(record):
                results.append(record)
                if len(results) == 30:
                    return results
    return results


def remove_public_source(connection):
    match = re.fullmatch(r'(github|gitlab):([a-z0-9_.-]+(?:/[a-z0-9_.-]+)+)', connection)
    if not match or '..' in match.group(2):
        raise ValueError('Invalid public source')
    kind = 'pr' if match.group(1) == 'github' else 'mr'
    pattern = re.compile(re.escape(connection + ':' + kind + ':') + r'[1-9][0-9]*')
    s3, db = clients()
    keys = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket='team-context-records'):
        for item in page.get('Contents', []):
            key = item['Key']
            record = json.loads(s3.get_object(Bucket='team-context-records', Key=key)['Body'].read())
            record_id = record.get('id')
            if isinstance(record_id, str) and pattern.fullmatch(record_id) and key == hashlib.sha256(record_id.encode()).hexdigest() + '.json':
                keys.append(key)
    for key in keys:
        s3.delete_object(Bucket='team-context-records', Key=key)
    db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': connection}})
    return len(keys)


def cleanup_unavailable():
    s3, _ = clients()
    candidates = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket='team-context-records'):
        for item in page.get('Contents', []):
            key = item['Key']
            record = json.loads(s3.get_object(Bucket='team-context-records', Key=key)['Body'].read())
            record_id = record.get('id')
            if record.get('visibility') != 'public' or not isinstance(record_id, str):
                continue
            if key != hashlib.sha256(record_id.encode()).hexdigest() + '.json':
                continue
            if status(record) == 'unavailable':
                candidates.append(key)
    for key in candidates:
        s3.delete_object(Bucket='team-context-records', Key=key)
    return len(candidates)

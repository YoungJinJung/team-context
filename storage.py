"""Persist collected records and synchronization status to AWS-compatible services."""
import hashlib
import json
import boto3


def clients():
    options = dict(endpoint_url='http://127.0.0.1:4568', region_name='us-east-1', aws_access_key_id='local', aws_secret_access_key='local')
    return boto3.client('s3', **options), boto3.client('dynamodb', **options)


def persist(connector, config, records):
    s3, dynamodb = clients()
    repository = config['repository'].lower()
    connection = connector + ':' + repository
    for record in records:
        key = hashlib.sha256(record['id'].encode()).hexdigest() + '.json'
        s3.put_object(Bucket='team-context-records', Key=key, Body=json.dumps(record, ensure_ascii=False).encode(), ContentType='application/json')
    dynamodb.put_item(TableName='team-context-sync', Item={'connection_id': {'S': connection}, 'count': {'N': str(len(records))}})

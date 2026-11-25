"""Run with TEAM_CONTEXT_INTEGRATION=1 against the local emulator."""
import hashlib
import json
import os
import pytest
from storage import clients, persist
from init_local import initialize

pytestmark = pytest.mark.skipif(os.environ.get('TEAM_CONTEXT_INTEGRATION') != '1', reason='requires local emulator')


def test_s3_and_dynamodb_roundtrip():
    initialize()
    initialize()
    record = {'id': 'integration:pr:1', 'type': 'pull_request', 'title': 'Local test'}
    persist('github:example/test', [record])
    s3, db = clients()
    key = hashlib.sha256(record['id'].encode()).hexdigest() + '.json'
    assert json.loads(s3.get_object(Bucket='team-context-records', Key=key)['Body'].read()) == record
    item = db.get_item(TableName='team-context-sync', Key={'connection_id': {'S': 'github:example/test'}})['Item']
    assert item['count']['N'] == '1'
    s3.delete_object(Bucket='team-context-records', Key=key)
    db.delete_item(TableName='team-context-sync', Key={'connection_id': {'S': 'github:example/test'}})

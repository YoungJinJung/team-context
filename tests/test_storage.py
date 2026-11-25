import hashlib
import json
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError
import init_local
import storage


def test_clients_use_local_credentials_and_endpoint():
    with patch('storage.boto3.client') as client:
        storage.clients()
    assert [c.args[0] for c in client.call_args_list] == ['s3', 'dynamodb']
    assert client.call_args.kwargs == dict(endpoint_url='http://127.0.0.1:4568', region_name='us-east-1', aws_access_key_id='local', aws_secret_access_key='local')


def test_persist_writes_records_before_status():
    s3, db = Mock(), Mock()
    record = {'id': 'github:a/b:pr:1', 'title': '한글'}
    with patch('storage.clients', return_value=(s3, db)):
        storage.persist('github:a/b', [record])
    args = s3.put_object.call_args.kwargs
    assert args['Bucket'] == 'team-context-records'
    assert args['Key'] == hashlib.sha256(record['id'].encode()).hexdigest() + '.json'
    assert json.loads(args['Body']) == record
    assert args['ContentType'] == 'application/json'
    db.put_item.assert_called_once_with(TableName='team-context-sync', Item={'connection_id': {'S': 'github:a/b'}, 'count': {'N': '1'}})


def test_persist_does_not_advance_after_failed_write():
    s3, db = Mock(), Mock()
    s3.put_object.side_effect = OSError('failed')
    with patch('storage.clients', return_value=(s3, db)):
        try:
            storage.persist('github:a/b', [{'id': 'x'}])
            assert False
        except OSError:
            pass
    db.put_item.assert_not_called()


def test_initialize_existing():
    s3, db = Mock(), Mock()
    with patch('init_local.clients', return_value=(s3, db)):
        init_local.initialize()
    s3.head_bucket.assert_called_once_with(Bucket='team-context-records')
    db.describe_table.assert_called_once_with(TableName='team-context-sync')
    s3.create_bucket.assert_not_called()
    db.create_table.assert_not_called()


def test_initialize_creates_missing():
    s3, db = Mock(), Mock()
    s3.head_bucket.side_effect = ClientError({'Error': {'Code': '404'}}, 'HeadBucket')
    class Missing(Exception): pass
    db.exceptions.ResourceNotFoundException = Missing
    db.describe_table.side_effect = Missing()
    with patch('init_local.clients', return_value=(s3, db)):
        init_local.initialize()
    s3.create_bucket.assert_called_once_with(Bucket='team-context-records')
    db.create_table.assert_called_once_with(TableName='team-context-sync', KeySchema=[{'AttributeName': 'connection_id', 'KeyType': 'HASH'}], AttributeDefinitions=[{'AttributeName': 'connection_id', 'AttributeType': 'S'}], BillingMode='PAY_PER_REQUEST')
    db.get_waiter.assert_called_once_with('table_exists')
    db.get_waiter.return_value.wait.assert_called_once_with(TableName='team-context-sync')


def test_initialize_propagates_permission_error():
    s3, db = Mock(), Mock()
    s3.head_bucket.side_effect = ClientError({'Error': {'Code': '403'}}, 'HeadBucket')
    with patch('init_local.clients', return_value=(s3, db)):
        try:
            init_local.initialize()
            assert False
        except ClientError as e:
            assert e.response['Error']['Code'] == '403'


def test_both_clients_receive_endpoint_configuration():
    with patch('storage.boto3.client') as client:
        storage.clients()
    for call in client.call_args_list:
        assert call.kwargs['endpoint_url'] == 'http://127.0.0.1:4568'
        assert call.kwargs['aws_access_key_id'] == 'local'
        assert call.kwargs['aws_secret_access_key'] == 'local'


def test_zero_records_updates_count():
    s3, db = Mock(), Mock()
    with patch('storage.clients', return_value=(s3, db)):
        storage.persist('github:a/b', [])
    s3.put_object.assert_not_called()
    assert db.put_item.call_args.kwargs['Item']['count'] == {'N': '0'}


def test_initialize_accepts_missing_bucket_codes():
    for code in ('404', 'NoSuchBucket', 'NotFound'):
        s3, db = Mock(), Mock()
        s3.head_bucket.side_effect = ClientError({'Error': {'Code': code}}, 'HeadBucket')
        with patch('init_local.clients', return_value=(s3, db)):
            init_local.initialize()
        s3.create_bucket.assert_called_once_with(Bucket='team-context-records')

import hashlib
import io
import json
from unittest.mock import Mock, patch
import pytest
import storage


def key(identifier): return hashlib.sha256(identifier.encode()).hexdigest() + '.json'


def backend(records):
    s3, db = Mock(), Mock()
    objects = {key(r['id']): r for r in records}
    s3.get_paginator.return_value.paginate.return_value = [{'Contents': [{'Key': k} for k in objects]}]
    s3.get_object.side_effect = lambda Bucket, Key: {'Body': io.BytesIO(json.dumps(objects[Key]).encode())}
    return s3, db


def test_cleanup_removes_only_unavailable_public_records():
    records = [{'id': 'github:a/b:pr:1', 'visibility': 'public'}, {'id': 'github:a/b:pr:2', 'visibility': 'public'}, {'id': 'github:a/b:pr:3', 'visibility': 'private'}, {'id': 'github:a/b:pr:4', 'visibility': 'public'}]
    s3, db = backend(records)
    with patch('storage.clients', return_value=(s3, db)), patch('storage.status', side_effect=['unavailable', 'changed', 'current']) as status:
        assert storage.cleanup_unavailable() == 1
    s3.delete_object.assert_called_once_with(Bucket='team-context-records', Key=key(records[0]['id']))
    assert status.call_count == 3
    db.delete_item.assert_not_called()


def test_failed_verification_deletes_nothing():
    records = [{'id': 'github:a/b:pr:1', 'visibility': 'public'}, {'id': 'github:a/b:pr:2', 'visibility': 'public'}]
    s3, db = backend(records)
    with patch('storage.clients', return_value=(s3, db)), patch('storage.status', side_effect=['unavailable', OSError('rate limited')]):
        with pytest.raises(OSError): storage.cleanup_unavailable()
    s3.delete_object.assert_not_called()


def test_corrupt_or_unsupported_records_are_not_deleted():
    records = [{'id': 'linear:one', 'visibility': 'public'}]
    s3, db = backend(records)
    s3.get_paginator.return_value.paginate.return_value[0]['Contents'].append({'Key': 'forged'})
    original = s3.get_object.side_effect
    s3.get_object.side_effect = lambda Bucket, Key: {'Body': io.BytesIO(b'{"id":"github:a/b:pr:1","visibility":"public"}')} if Key == 'forged' else original(Bucket, Key)
    with patch('storage.clients', return_value=(s3, db)), patch('storage.status', return_value='invalid') as status:
        assert storage.cleanup_unavailable() == 0
    assert status.call_count == 1
    s3.delete_object.assert_not_called()


def test_empty_bucket():
    s3 = Mock()
    s3.get_paginator.return_value.paginate.return_value = [{}]
    with patch('storage.clients', return_value=(s3, Mock())):
        assert storage.cleanup_unavailable() == 0
    s3.delete_object.assert_not_called()


def test_cleanup_delete_failure_reports_partial_result_and_allows_retry():
    records = [{'id': 'github:a/b:pr:1', 'visibility': 'public'}, {'id': 'github:a/b:pr:2', 'visibility': 'public'}]
    s3, db = backend(records)
    s3.delete_object.side_effect = [None, OSError('unavailable')]
    with patch('storage.clients', return_value=(s3, db)), patch('storage.status', return_value='unavailable'):
        with pytest.raises(OSError): storage.cleanup_unavailable()
    assert s3.delete_object.call_count == 2
    remaining, db = backend(records[1:])
    with patch('storage.clients', return_value=(remaining, db)), patch('storage.status', return_value='unavailable'):
        assert storage.cleanup_unavailable() == 1

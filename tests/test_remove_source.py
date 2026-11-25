import hashlib
import io
import json
from unittest.mock import Mock, patch
import pytest
import storage


def key(identifier): return hashlib.sha256(identifier.encode()).hexdigest() + '.json'


def sources(ids):
    s3, db = Mock(), Mock()
    objects = {key(i): {'id': i, 'visibility': 'public'} for i in ids}
    s3.get_paginator.return_value.paginate.return_value = [{'Contents': [{'Key': k} for k in objects]}]
    s3.get_object.side_effect = lambda Bucket, Key: {'Body': io.BytesIO(json.dumps(objects[Key]).encode())}
    return s3, db


def test_delete_only_exact_source_and_then_status():
    ids = ['github:a/repo:pr:1', 'github:a/repo:pr:10', 'github:a/repo2:pr:1', 'gitlab:a/repo:mr:1']
    s3, db = sources(ids)
    with patch('storage.clients', return_value=(s3, db)):
        assert storage.remove_public_source('github:a/repo') == 2
    s3.get_paginator.assert_called_once_with('list_objects_v2')
    s3.get_paginator.return_value.paginate.assert_called_once_with(Bucket='team-context-records')
    assert all(call.kwargs['Bucket'] == 'team-context-records' for call in s3.get_object.call_args_list)
    assert [call.kwargs for call in s3.delete_object.call_args_list] == [{'Bucket': 'team-context-records', 'Key': key(ids[0])}, {'Bucket': 'team-context-records', 'Key': key(ids[1])}]
    db.delete_item.assert_called_once_with(TableName='team-context-sync', Key={'connection_id': {'S': 'github:a/repo'}})


def test_failed_record_deletion_does_not_remove_status():
    s3, db = sources(['gitlab:a/repo:mr:1'])
    s3.delete_object.side_effect = OSError('failed')
    with patch('storage.clients', return_value=(s3, db)):
        with pytest.raises(OSError): storage.remove_public_source('gitlab:a/repo')
    db.delete_item.assert_not_called()


def test_never_delete_object_with_forged_hash_or_wrong_kind():
    s3, db = sources(['github:a/repo:mr:1'])
    s3.get_paginator.return_value.paginate.return_value[0]['Contents'].append({'Key': 'forged'})
    original = s3.get_object.side_effect
    s3.get_object.side_effect = lambda Bucket, Key: {'Body': io.BytesIO(b'{"id":"github:a/repo:pr:1","visibility":"public"}')} if Key == 'forged' else original(Bucket, Key)
    with patch('storage.clients', return_value=(s3, db)):
        assert storage.remove_public_source('github:a/repo') == 0
    s3.delete_object.assert_not_called()


@pytest.mark.parametrize('connection', ['', 'linear:x/y', 'github:../repo', 'gitlab:a/../repo', 'GITHUB:a/b'])
def test_invalid_source_does_not_touch_storage(connection):
    with patch('storage.clients') as clients:
        with pytest.raises(ValueError, match='^Invalid public source$'): storage.remove_public_source(connection)
        clients.assert_not_called()


def test_empty_bucket_still_removes_sync_status():
    s3, db = Mock(), Mock()
    s3.get_paginator.return_value.paginate.return_value = [{}]
    with patch('storage.clients', return_value=(s3, db)):
        assert storage.remove_public_source('github:a/repo') == 0
    s3.get_object.assert_not_called()
    s3.delete_object.assert_not_called()
    db.delete_item.assert_called_once_with(TableName='team-context-sync', Key={'connection_id': {'S': 'github:a/repo'}})


def test_delete_ignores_private_and_unknown_visibility():
    ids = ['github:a/repo:pr:1', 'github:a/repo:pr:2', 'github:a/repo:pr:3', 'github:a/repo:pr:4']
    objects = {key(ids[0]): {'id': ids[0], 'visibility': 'public'}, key(ids[1]): {'id': ids[1], 'visibility': 'private'}, key(ids[2]): {'id': ids[2]}, key(ids[3]): {'id': ids[3], 'visibility': 'unknown'}}
    s3, db = sources([])
    s3.get_paginator.return_value.paginate.return_value = [{'Contents': [{'Key': k} for k in objects]}]
    s3.get_object.side_effect = lambda Bucket, Key: {'Body': io.BytesIO(json.dumps(objects[Key]).encode())}
    with patch('storage.clients', return_value=(s3, db)):
        assert storage.remove_public_source('github:a/repo') == 1
    s3.delete_object.assert_called_once_with(Bucket='team-context-records', Key=key(ids[0]))


def test_non_object_record_is_ignored():
    s3, db = sources([])
    s3.get_paginator.return_value.paginate.return_value = [{'Contents':[{'Key':'malformed'}]}]
    s3.get_object.side_effect = lambda **_: {'Body':io.BytesIO(b'[]')}
    with patch('storage.clients', return_value=(s3, db)):
        assert storage.remove_public_source('github:a/repo') == 0
    s3.delete_object.assert_not_called()


def test_invalid_json_aborts_before_deleting_any_record():
    identifier = 'github:a/repo:pr:1'
    s3, db = sources([identifier])
    s3.get_paginator.return_value.paginate.return_value[0]['Contents'].append({'Key':'invalid'})
    original = s3.get_object.side_effect
    s3.get_object.side_effect = lambda Bucket, Key: {'Body':io.BytesIO(b'{')} if Key == 'invalid' else original(Bucket, Key)
    with patch('storage.clients', return_value=(s3, db)):
        with pytest.raises(json.JSONDecodeError): storage.remove_public_source('github:a/repo')
    s3.delete_object.assert_not_called()
    db.delete_item.assert_not_called()

from unittest.mock import Mock, patch
import pytest
import storage


def test_lists_only_public_sources_from_all_pages():
    db = Mock()
    db.get_paginator.return_value.paginate.return_value = [
        {'Items': [{'connection_id': {'S': 'session:secret-hash'}, 'secret_id': {'S': 'secret'}, 'count': {'N': '7'}}, {'connection_id': {'S': 'github:z/repo'}, 'count': {'N': '12'}}, {'connection_id': {'S': 'github:a/../b'}, 'count': {'N': '1'}}]},
        {'Items': [{'connection_id': {'S': 'gitlab:group/sub/repo'}, 'count': {'N': '4'}}, {'connection_id': {'S': 'github:a/repo'}, 'count': {'N': '0'}}]},
    ]
    with patch('storage.clients', return_value=(Mock(), db)):
        assert storage.list_public_sources() == [{'connector': 'github', 'source': 'a/repo', 'count': 0}, {'connector': 'github', 'source': 'z/repo', 'count': 12}, {'connector': 'gitlab', 'source': 'group/sub/repo', 'count': 4}]
    db.get_paginator.assert_called_once_with('scan')
    db.get_paginator.return_value.paginate.assert_called_once_with(TableName='team-context-sync', ProjectionExpression='#connection, #count', ExpressionAttributeNames={'#connection': 'connection_id', '#count': 'count'})


@pytest.mark.parametrize('item', [{}, {'connection_id': {'S': 1}, 'count': {'N': '3'}}, {'connection_id': {'S': 'github:a/b'}}, {'connection_id': {'S': 'github:a/b'}, 'count': {'N': '-1'}}, {'connection_id': {'S': 'github:a/b'}, 'count': {'N': 'bad'}}, {'connection_id': {'S': 'github:a/b/c'}, 'count': {'N': '1'}}, {'connection_id': {'S': 'linear:a/b'}, 'count': {'N': '1'}}])
def test_invalid_records_are_not_returned(item):
    db = Mock()
    db.get_paginator.return_value.paginate.return_value = [{'Items': [item]}]
    with patch('storage.clients', return_value=(Mock(), db)):
        assert storage.list_public_sources() == []


def test_empty_page_and_storage_failure():
    db = Mock()
    db.get_paginator.return_value.paginate.return_value = [{}]
    with patch('storage.clients', return_value=(Mock(), db)):
        assert storage.list_public_sources() == []
    db.get_paginator.side_effect = OSError('unavailable')
    with patch('storage.clients', return_value=(Mock(), db)):
        with pytest.raises(OSError): storage.list_public_sources()

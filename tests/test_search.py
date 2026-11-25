import io
import json
from unittest.mock import Mock, patch
import pytest
import storage


def fake_s3(records):
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [{'Contents': [{'Key': str(i)} for i in range(len(records))]}]
    client.get_object.side_effect = lambda Bucket, Key: {'Body': io.BytesIO(json.dumps(records[int(Key)]).encode())}
    return client


def test_search_only_public_and_matches_title_and_body():
    records = [{'id': 'a', 'title': 'Kubernetes fix', 'body': '', 'visibility': 'public'}, {'id': 'b', 'title': 'Other', 'body': 'KUBERNETES notes', 'visibility': 'public'}, {'id': 'c', 'title': 'Kubernetes private', 'body': '', 'visibility': 'private'}, {'id': 'd', 'title': 'Kubernetes legacy', 'body': ''}]
    s3 = fake_s3(records)
    with patch('storage.clients', return_value=(s3, Mock())):
        assert storage.search('  kubernetes  ') == records[:2]
    s3.get_paginator.assert_called_once_with('list_objects_v2')
    s3.get_paginator.return_value.paginate.assert_called_once_with(Bucket='team-context-records')
    assert s3.get_object.call_count == 4


@pytest.mark.parametrize('query', ['', ' ', 'x', 'x' * 101])
def test_invalid_queries(query):
    with pytest.raises(ValueError, match='2 to 100'): storage.search(query)


def test_search_limits_results_and_stops_fetching():
    records = [{'title': 'Match', 'body': '', 'visibility': 'public'} for _ in range(35)]
    s3 = fake_s3(records)
    with patch('storage.clients', return_value=(s3, Mock())):
        assert len(storage.search('match')) == 30
    assert s3.get_object.call_count == 30


def test_empty_page_returns_no_results():
    s3 = Mock()
    s3.get_paginator.return_value.paginate.return_value = [{}]
    with patch('storage.clients', return_value=(s3, Mock())):
        assert storage.search('word') == []
    s3.get_object.assert_not_called()


def test_search_propagates_storage_error():
    s3 = fake_s3([{'title': 'word', 'visibility': 'public'}])
    s3.get_object.side_effect = OSError('unavailable')
    with patch('storage.clients', return_value=(s3, Mock())):
        with pytest.raises(OSError): storage.search('word')


@pytest.mark.parametrize('query', ['ab', 'a' * 100])
def test_search_accepts_boundary_lengths(query):
    s3 = fake_s3([{'title': query, 'visibility': 'public'}])
    with patch('storage.clients', return_value=(s3, Mock())):
        assert len(storage.search(query)) == 1
    s3.get_object.assert_called_once_with(Bucket='team-context-records', Key='0')


def test_search_handles_missing_title_or_body():
    records = [{'body': 'needle', 'visibility': 'public'}, {'title': 'needle', 'visibility': 'public'}]
    s3 = fake_s3(records)
    with patch('storage.clients', return_value=(s3, Mock())):
        assert storage.search('needle') == records

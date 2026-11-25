import io
import json
import unittest
from unittest.mock import patch
from connectors import github
from app import discover


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class ConnectorTest(unittest.TestCase):
    def test_discovery(self):
        self.assertIn('github', discover())

    def test_rejects_invalid_repository(self):
        for repository in ['https://example.com/a', '../private', 'owner/name?x=y']:
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                github.collect({'repository': repository})

    @patch('connectors.github.urllib.request.urlopen')
    def test_maps_pull_requests(self, urlopen):
        data = [{'number': 4, 'title': 'Example', 'html_url': 'https://github.com/a/b/pull/4', 'updated_at': '2026-01-01', 'body': None}]
        urlopen.return_value = Response(json.dumps(data).encode())
        records = github.collect({'repository': 'a/b'})
        self.assertEqual(records[0]['id'], 'github:a/b:pr:4')
        self.assertEqual(records[0]['body'], '')
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 15)


if __name__ == '__main__': unittest.main()

class ConnectorContractTest(unittest.TestCase):
    @patch('connectors.github.urllib.request.urlopen')
    def test_request_and_record_contract(self, urlopen):
        payload = [{'number': 7, 'title': 'Fix', 'html_url': 'https://github.com/Org/Repo/pull/7', 'updated_at': 'now', 'body': 'description'}]
        urlopen.return_value = Response(json.dumps(payload).encode())
        result = github.collect({'repository': 'Org/Repo'})
        self.assertEqual(result, [{'id': 'github:org/repo:pr:7', 'visibility': 'public', 'type': 'pull_request', 'title': 'Fix', 'url': payload[0]['html_url'], 'updated_at': 'now', 'body': 'description'}])
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.github.com/repos/Org/Repo/pulls?state=all&per_page=30')
        self.assertEqual(req.get_header('Accept'), 'application/vnd.github+json')
        self.assertEqual(req.get_header('User-agent'), 'team-context-mvp')

    def test_missing_repository(self):
        with self.assertRaisesRegex(ValueError, 'owner/name'):
            github.collect({})


def test_connection_id_is_stable_and_namespaced():
    assert github.connection_id({'repository': 'Owner/Repo'}) == 'github:owner/repo'

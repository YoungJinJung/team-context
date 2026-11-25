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

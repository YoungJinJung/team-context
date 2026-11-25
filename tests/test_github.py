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

    @patch('connectors.github.open_url')
    def test_maps_pull_requests(self, urlopen):
        data = [{'number': 4, 'title': 'Example', 'html_url': 'https://github.com/a/b/pull/4', 'updated_at': '2026-01-01', 'body': None}]
        urlopen.return_value = Response(json.dumps(data).encode())
        records = github.collect({'repository': 'a/b'})
        self.assertEqual(records[0]['id'], 'github:a/b:pr:4')
        self.assertEqual(records[0]['body'], '')
        self.assertEqual(urlopen.call_args.kwargs, {})


if __name__ == '__main__': unittest.main()

class ConnectorContractTest(unittest.TestCase):
    @patch('connectors.github.open_url')
    def test_request_and_record_contract(self, urlopen):
        payload = [{'number': 7, 'title': 'Fix', 'html_url': 'https://github.com/Org/Repo/pull/7', 'updated_at': 'now', 'body': 'description'}]
        urlopen.return_value = Response(json.dumps(payload).encode())
        result = github.collect({'repository': 'Org/Repo'})
        self.assertEqual(result, [{'id': 'github:org/repo:pr:7', 'visibility': 'public', 'type': 'pull_request', 'title': 'Fix', 'url': payload[0]['html_url'], 'updated_at': 'now', 'body': 'description'}])
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.github.com/repos/Org/Repo/pulls?state=all&per_page=100&page=1')
        self.assertEqual(req.get_header('Accept'), 'application/vnd.github+json')
        self.assertEqual(req.get_header('User-agent'), 'team-context-mvp')

    def test_missing_repository(self):
        with self.assertRaisesRegex(ValueError, 'owner/name'):
            github.collect({})


def test_connection_id_is_stable_and_namespaced():
    assert github.connection_id({'repository': 'Owner/Repo'}) == 'github:owner/repo'


class PaginationTest(unittest.TestCase):
    @staticmethod
    def pull(number):
        return {'number': number, 'title': str(number), 'html_url': 'https://github.com/a/b/pull/' + str(number), 'updated_at': 'today', 'body': None}

    @patch('connectors.github.open_url')
    def test_multiple_pages_are_collected(self, open_url):
        open_url.side_effect = [Response(json.dumps([self.pull(i) for i in range(1, 101)]).encode()), Response(json.dumps([self.pull(101)]).encode())]
        records = github.collect({'repository': 'a/b'})
        self.assertEqual(len(records), 101)
        self.assertEqual(records[-1]['id'], 'github:a/b:pr:101')
        self.assertEqual(open_url.call_args_list[1].args[0].full_url, 'https://api.github.com/repos/a/b/pulls?state=all&per_page=100&page=2')

    @patch('connectors.github.open_url')
    def test_empty_page_and_failed_next_page(self, open_url):
        open_url.return_value = Response(b'[]')
        self.assertEqual(github.collect({'repository': 'a/b'}), [])
        open_url.side_effect = [Response(json.dumps([self.pull(i) for i in range(1, 101)]).encode()), OSError('rate limit')]
        with self.assertRaises(OSError): github.collect({'repository': 'a/b'})

    @patch('connectors.github.open_url')
    def test_repeated_records_and_invalid_page_fail(self, open_url):
        open_url.return_value = Response(json.dumps([self.pull(1), self.pull(1)]).encode())
        with self.assertRaisesRegex(ValueError, 'Repeated'): github.collect({'repository': 'a/b'})
        open_url.return_value = Response(b'{}')
        with self.assertRaisesRegex(ValueError, 'Invalid GitHub page'): github.collect({'repository': 'a/b'})

    @patch('connectors.github.open_url')
    def test_exceeding_page_limit_fails(self, open_url):
        counter = iter(range(100))
        def page(request):
            first = next(counter) * 100
            return Response(json.dumps([self.pull(first + i) for i in range(100)]).encode())
        open_url.side_effect = page
        with self.assertRaisesRegex(ValueError, 'exceeds 100 pages'): github.collect({'repository': 'a/b'})


def test_rate_limit_after_full_page_fails_instead_of_returning_partial():
    from urllib.error import HTTPError
    from unittest.mock import patch
    page = [PaginationTest.pull(i) for i in range(100)]
    with patch('connectors.github.open_url', side_effect=[Response(json.dumps(page).encode()), HTTPError('https://api.github.com', 429, 'rate limited', {}, None)]):
        with unittest.TestCase().assertRaises(HTTPError):
            github.collect({'repository': 'a/b'})

"""Live-search HTTP boundary, using an ephemeral socket and no external calls."""
import http.client
import json
from threading import Thread
import unittest
from unittest.mock import patch

from app.dgis_live import DGISLiveError
from app.server import create_server
from tests.test_retrieval import index, poi


class DGISHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server('127.0.0.1', 0, retriever=index(poi('p')))
        cls.thread = Thread(target=cls.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def post(self, payload, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        try:
            connection.request('POST', '/api/2gis/search', json.dumps(payload).encode(), headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_category_only_search_is_transient_and_not_a_walk_recommendation(self):
        result = {'places': [], 'quota': {'limit': 20, 'remaining': 19}, 'category': 'park'}
        with patch('app.server.fetch_live', return_value=result) as fetch:
            status, headers, raw = self.post({'category': 'park'})
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'private, no-store')
        self.assertEqual(json.loads(raw), result)
        fetch.assert_called_once_with('park')
        self.assertNotIn('plan', json.loads(raw))

    def test_private_input_unknown_categories_and_cross_origin_do_not_call_provider(self):
        with patch('app.server.fetch_live') as fetch:
            for value in ({'category': 'park', 'origin': {'lat': 51.1, 'lon': 71.4}},
                          {'query': 'private free text'}, {'category': 'unbounded search'},
                          {'category': {}}, [], None):
                with self.subTest(value=value):
                    self.assertEqual(self.post(value)[0], 400)
            self.assertEqual(self.post({'category': 'park'}, 'https://other.example')[0], 403)
        fetch.assert_not_called()

    def test_quota_error_is_explicit_and_unexpected_failure_is_sanitized(self):
        denied = DGISLiveError('Лимит исчерпан.', code='quota_exhausted', http_status=429,
                               quota={'limit': 20, 'remaining': 0})
        with patch('app.server.fetch_live', side_effect=denied):
            status, headers, raw = self.post({'category': 'cafe'})
        self.assertEqual(status, 429)
        self.assertEqual(headers['Cache-Control'], 'private, no-store')
        self.assertEqual(json.loads(raw)['quota']['remaining'], 0)
        with patch('app.server.fetch_live', side_effect=RuntimeError('secret-key private-path')):
            status, _, raw = self.post({'category': 'cafe'})
        self.assertEqual(status, 503)
        self.assertNotIn(b'secret-key', raw)
        self.assertNotIn(b'private-path', raw)


if __name__ == '__main__':
    unittest.main()

"""Standalone checks. All data and listeners are temporary; no agent is launched."""
import concurrent.futures
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app import server
from board import Board


class RelayChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-check-')
        self.addCleanup(self.temp.cleanup)
        self.board = Board(Path(self.temp.name) / 'board.sqlite3')
        for project in ('alpha', 'beta'):
            self.board.mutate('create', dict(instance=project, title=project,
                sessions=[dict(session='worker', runtime='Other', label='My agent')]))

    def event(self, **extra):
        return dict(instance='alpha', session='worker', id='event-1', kind='progress',
                    text='A retained finding', status='review', **extra)

    def test_identity_atomic_creation_and_duplicate_events(self):
        with self.assertRaises(ValueError):
            self.board.mutate('create', dict(instance='invalid', title='Invalid',
                sessions=[dict(session='worker', runtime='unsupported', label='Bad')]))
        self.assertNotIn('invalid', [p['id'] for p in self.board.index()['instances']])
        self.assertFalse(self.board.mutate('ingest', self.event())['duplicate'])
        self.assertTrue(self.board.mutate('ingest', self.event())['duplicate'])
        with self.assertRaises(ValueError):
            self.board.mutate('ingest', {**self.event(), 'text': 'Changed bytes'})
        self.assertEqual(self.board.snapshot('beta')['events'], [])

    def test_messages_need_explicit_receipts_and_remain_persistent(self):
        message = dict(instance='alpha', session='worker', id='message-1', text='Please inspect')
        self.board.mutate('send', message)
        self.assertTrue(self.board.mutate('send', message)['duplicate'])
        with self.assertRaises(ValueError):
            self.board.mutate('send', {**message, 'text': 'Conflict'})
        inbox = self.board.inbox('alpha', 'worker')
        self.assertTrue(inbox['read_does_not_acknowledge'])
        self.assertEqual(inbox['messages'][0]['status'], 'saved')
        receipt = dict(instance='alpha', session='worker', message='message-1', note='Synthetic receipt')
        with self.assertRaises(ValueError):
            self.board.mutate('receipt', {**receipt, 'status': 'completed'})
        for status in ('delivered', 'acknowledged', 'completed'):
            self.board.mutate('receipt', {**receipt, 'status': status})
        self.assertEqual(self.board.inbox('alpha', 'worker')['messages'], [])
        restored = Board(self.board.path).snapshot('alpha')
        self.assertEqual([r['status'] for r in restored['receipts']],
                         ['delivered', 'acknowledged', 'completed'])

    def test_questions_wait_and_summary_provider_is_not_hardcoded(self):
        event = {**self.event(), 'kind': 'question', 'requires_approval': True, 'default': 'Continue'}
        self.board.mutate('ingest', event)
        body = self.board.snapshot('alpha')['events'][0]['body']
        self.assertIn('Wait for explicit approval', body['default'])
        self.board.mutate('ingest', {**self.event(), 'id': 'summary-1', 'kind': 'summary',
                                    'model': 'My local model', 'effort': 'self-reported'})
        with self.assertRaises(ValueError):
            self.board.mutate('ingest', {**self.event(), 'id': 'bad-time', 'observed_at': '2999-01-01T00:00:00Z'})

    def test_concurrent_identical_retry_is_one_event(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: self.board.mutate('ingest', self.event()), range(4)))
        self.assertEqual(sum(not row['duplicate'] for row in rows), 1)

    def test_http_origin_size_and_no_execution_or_receipt_controls(self):
        listener = server(self.board, 0)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{listener.server_port}'
        def request(method, path, value=None, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', listener.server_port, timeout=5)
            body = json.dumps(value) if value is not None else None
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            result = (response.status, response.read())
            connection.close()
            return result
        try:
            self.assertEqual(request('GET', '/api/state?instance=alpha')[0], 200)
            self.assertEqual(request('GET', '/api/instances', headers={'Origin': 'https://evil.invalid'})[0], 403)
            self.assertEqual(request('POST', '/api/send', {})[0], 403)
            trusted = {'Origin': origin, 'Content-Type': 'application/json'}
            for route in ('control', 'receipt', 'staffing/delivery/start'):
                self.assertEqual(request('POST', '/api/' + route, {}, trusted)[0], 404)
            self.assertEqual(request('POST', '/api/send', dict(instance='alpha', session='worker', text='Browser message'), trusted)[0], 200)
            self.assertEqual(request('POST', '/api/send', {'text': 'x' * 131073}, trusted)[0], 400)
        finally:
            listener.shutdown()
            thread.join(timeout=5)
            listener.server_close()


if __name__ == '__main__':
    unittest.main()

"""Fault-injection tests: no public network and no real model requests."""
import concurrent.futures
import http.client
import json
import pathlib
import socket
import ssl
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch, Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import gateway


class FaultUpstream(BaseHTTPRequestHandler):
    plan = []
    requests = []
    lock = threading.Lock()
    active = maximum = 0
    def log_message(self, *args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        with self.lock:
            cls = type(self)
            cls.requests.append((self.path, body))
            step = cls.plan.pop(0) if cls.plan else {}
            cls.active += 1
            cls.maximum = max(cls.maximum, cls.active)
        try:
            if step.get('entered'): step['entered'].set()
            if step.get('wait'): step['wait'].wait(3)
            if step.get('disconnect'):
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            body = step.get('body', b'{"ok":true}')
            self.send_response(step.get('status', 200))
            self.send_header('Content-Type', step.get('content_type', 'application/json'))
            self.send_header('Content-Length', str(len(body) + (100 if step.get('truncate') else 0)))
            for k, v in step.get('headers', {}).items(): self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        finally:
            with self.lock: type(self).active -= 1


class Reliability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(('127.0.0.1', 0), FaultUpstream)
        cls.proxy = ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
        for server in (cls.up, cls.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for server in (cls.up, cls.proxy): server.shutdown(); server.server_close()
    def setUp(self):
        FaultUpstream.plan = []
        FaultUpstream.requests = []
        FaultUpstream.active = FaultUpstream.maximum = 0
        gateway.MODEL_GATE = gateway.ModelGate()
        ep = {'url': f'http://synthetic.invalid:{self.up.server_port}/v1', 'ip': '127.0.0.1', 'model': 'local'}
        gateway.CONFIG = {'llm': ep, 'embed': ep}
        # Exercise real sockets without spending seconds on each backoff.
        self.sleeps = []
        def fast_wait(handler, delay, deadline):
            self.sleeps.append(delay)
            return delay is not None and delay <= 15 and not handler.disconnected()
        self.addCleanup(patch.stopall)
        patch.object(gateway.Handler, 'wait_retry', fast_wait).start()
        patch.object(gateway.random, 'uniform', return_value=0).start()
    def request(self, embedding=False):
        c = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        body = {'model': 'local', 'input': ['synthetic']} if embedding else {'model': 'local', 'messages': [{'role': 'user', 'content': 'synthetic'}]}
        c.request('POST', '/embed/v1/embeddings' if embedding else '/llm/v1/chat/completions', json.dumps(body))
        try:
            r = c.getresponse()
            try: raw, partial = r.read(), False
            except http.client.IncompleteRead as e: raw, partial = e.partial, True
            return r.status, raw, dict(r.getheaders()), partial
        finally: c.close()
    def wait_for(self, condition):
        end = time.monotonic()+2
        while not condition() and time.monotonic()<end: time.sleep(0.01)
        self.assertTrue(condition())
    def test_transient_retry_then_success_same_request(self):
        FaultUpstream.plan = [{'status': 502}, {'status': 503}, {}]
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(FaultUpstream.requests), 3)
        self.assertEqual(len(set(body for _, body in FaultUpstream.requests)), 1)
        self.assertEqual(self.sleeps, [2, 4])
        self.assertEqual(gateway.MODEL_GATE.status()['upstream_retries'], 2)
    def test_exhaustion_cooldown_prevents_sdk_retry_storm(self):
        FaultUpstream.plan = [{'status': 503}] * 3
        self.assertEqual(self.request()[0], 424)
        self.assertEqual(self.request(embedding=True)[0], 424)
        self.assertEqual(len(FaultUpstream.requests), 3)
        self.assertGreater(gateway.MODEL_GATE.status()['cooldown_seconds'], 25)
    def test_retry_after_honored_and_long_delay_defers(self):
        FaultUpstream.plan = [{'status': 429, 'headers': {'Retry-After': '7'}}, {}]
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.sleeps, [7])
        FaultUpstream.plan = [{'status': 503, 'headers': {'Retry-After': '120'}}]
        out = self.request()
        self.assertEqual(out[0], 424)
        self.assertGreaterEqual(int(out[2]['Retry-After']), 119)
        self.assertEqual(len(FaultUpstream.requests), 3)
    def test_nontransient_and_redirect_not_retried(self):
        for status in [400, 401, 403, 422, 500, 302]:
            FaultUpstream.requests = []
            FaultUpstream.plan = [{'status': status, 'headers': {'Location': 'https://example.com/never'}}]
            out = self.request()
            self.assertEqual(out[0], 424 if status in (302, 500) else status)
            self.assertEqual(len(FaultUpstream.requests), 1)
            self.assertNotIn('Location', out[2])
    def test_tls_failure_is_terminal(self):
        conn = Mock()
        conn.connect.side_effect = ssl.SSLCertVerificationError('synthetic')
        with patch.object(gateway, 'connection', return_value=conn):
            self.assertEqual(self.request()[0], 424)
        self.assertEqual(conn.connect.call_count, 1)
        self.assertEqual(FaultUpstream.requests, [])
    def test_connect_failure_before_post_can_retry(self):
        original = gateway.connection
        failed = Mock()
        failed.connect.side_effect = ConnectionRefusedError()
        with patch.object(gateway, 'connection', side_effect=[failed, original(gateway.CONFIG['llm'])]):
            self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(FaultUpstream.requests), 1)
    def test_ambiguous_post_failure_never_replayed(self):
        FaultUpstream.plan = [{'disconnect': True}, {}]
        self.assertEqual(self.request()[0], 424)
        self.assertEqual(len(FaultUpstream.requests), 1)
        self.assertGreater(gateway.MODEL_GATE.status()['cooldown_seconds'], 0)
    def test_proxy_timeout_is_ambiguous_and_not_retried(self):
        FaultUpstream.plan = [{'status': 504}, {}]
        self.assertEqual(self.request()[0], 424)
        self.assertEqual(len(FaultUpstream.requests), 1)
        self.assertEqual(self.request()[0], 424)
        self.assertEqual(len(FaultUpstream.requests), 1)
    def test_expired_budget_never_contacts_backend(self):
        with patch.object(gateway, 'MODEL_BUDGET', 0):
            self.assertEqual(self.request()[0], 424)
        self.assertEqual(FaultUpstream.requests, [])
    def test_partial_stream_never_replayed(self):
        body = b'data: {"choices":[]}\n\n'
        FaultUpstream.plan = [{'body': body, 'content_type': 'text/event-stream', 'truncate': True}, {}]
        status, raw, headers, partial = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(raw, body)
        self.assertTrue(partial)
        self.assertEqual(len(FaultUpstream.requests), 1)
        self.assertEqual(gateway.MODEL_GATE.status()['active'], 0)
    def test_llm_and_embeddings_share_one_slot_and_bounded_queue(self):
        gate = gateway.MODEL_GATE = gateway.ModelGate(capacity=2)
        entered, release = threading.Event(), threading.Event()
        FaultUpstream.plan = [{'entered': entered, 'wait': release}, {}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.request)
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.request, True)
            self.wait_for(lambda: gate.status()['waiting'] == 1)
            self.assertEqual(self.request()[0], 429)
            self.assertEqual(len(FaultUpstream.requests), 1)
            release.set()
            self.assertEqual(first.result()[0], 200)
            self.assertEqual(second.result()[0], 200)
        self.assertEqual(FaultUpstream.maximum, 1)
        self.assertEqual([p for p, _ in FaultUpstream.requests], ['/v1/chat/completions', '/v1/embeddings'])
    def test_queued_client_disconnect_drops_pending_request(self):
        entered, release = threading.Event(), threading.Event()
        FaultUpstream.plan = [{'entered': entered, 'wait': release}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.request)
            self.assertTrue(entered.wait(2))
            c = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port)
            c.request('POST', '/embed/v1/embeddings', json.dumps({'model': 'local', 'input': ['cancelled']}))
            self.wait_for(lambda: gateway.MODEL_GATE.status()['waiting'] == 1)
            c.close()
            self.wait_for(lambda: gateway.MODEL_GATE.status()['waiting'] == 0)
            release.set(); self.assertEqual(first.result()[0], 200)
        self.assertEqual(len(FaultUpstream.requests), 1)
    def test_queue_wait_is_bounded(self):
        gateway.MODEL_GATE = gateway.ModelGate(wait_seconds=0.05)
        entered, release = threading.Event(), threading.Event()
        FaultUpstream.plan = [{'entered': entered, 'wait': release}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.request); self.assertTrue(entered.wait(2))
            self.assertEqual(self.request(embedding=True)[0], 429)
            release.set(); self.assertEqual(first.result()[0], 200)
        self.assertEqual(len(FaultUpstream.requests), 1)


if __name__ == '__main__': unittest.main()

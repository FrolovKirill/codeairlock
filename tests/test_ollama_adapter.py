"""Synthetic native-Ollama bridge tests; never contact a model service."""
import http.client
import json
import pathlib
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import gateway
import ollama_adapter
from test_reliability import FaultUpstream


class NativeBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(('127.0.0.1', 0), FaultUpstream)
        cls.proxy = ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
        for server in (cls.up, cls.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.up, cls.proxy):
            server.shutdown(); server.server_close()

    def setUp(self):
        self.ep = {'url': f'http://synthetic.invalid:{self.up.server_port}/v1',
                   'ip': '127.0.0.1', 'model': 'synthetic', 'key': 'upstream-only',
                   'ollama': {'context': 8192, 'batch': 32, 'max_output': 512}}
        gateway.CONFIG = {'llm': self.ep, 'embed': {}}
        gateway.MODEL_GATE = gateway.ModelGate()
        self.native = {'model': 'synthetic', 'message': {'role': 'assistant', 'content': 'OK'},
                       'done': True, 'done_reason': 'stop', 'prompt_eval_count': 17, 'eval_count': 2}
        FaultUpstream.plan = [{'body': json.dumps(self.native).encode()}]
        FaultUpstream.requests = []

    def call(self, **extra):
        body = {'model': 'synthetic', 'messages': [{'role': 'user', 'content': 'Synthetic test'}], **extra}
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=3)
        conn.request('POST', '/llm/v1/chat/completions', json.dumps(body))
        response = conn.getresponse()
        result = response.status, response.read(), response.getheader('Content-Type')
        conn.close()
        return result

    def test_fixed_path_and_resource_caps(self):
        status, raw, _ = self.call(max_tokens=100000)
        self.assertEqual(status, 200)
        path, sent = FaultUpstream.requests[0]
        sent = json.loads(sent)
        self.assertEqual(path, '/api/chat')
        self.assertEqual(sent['options'], {'num_ctx': 8192, 'num_batch': 32, 'num_predict': 512})
        self.assertFalse(sent['think'])
        self.assertFalse(sent['stream'])
        self.assertEqual(json.loads(raw)['choices'][0]['message']['content'], 'OK')

    def test_agent_cannot_override_native_options(self):
        self.assertEqual(self.call(options={'num_ctx': 215000})[0], 403)
        self.assertEqual(FaultUpstream.requests, [])

    def test_complete_sse_and_usage(self):
        status, raw, content_type = self.call(stream=True, stream_options={'include_usage': True})
        self.assertEqual((status, content_type), (200, 'text/event-stream'))
        self.assertTrue(raw.endswith(b'data: [DONE]\n\n'))
        events = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: {')]
        self.assertEqual(events[-1]['usage']['total_tokens'], 19)
        self.assertEqual(events[-2]['choices'][0]['finish_reason'], 'stop')

    def test_tool_call_roundtrip(self):
        tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]
        self.native['message'] = {'content': '', 'tool_calls': [{'function': {'name': 'lookup', 'arguments': {'name': 'demo'}}}]}
        FaultUpstream.plan = [{'body': json.dumps(self.native).encode()}]
        status, raw, _ = self.call(tools=tools)
        self.assertEqual(status, 200)
        choice = json.loads(raw)['choices'][0]
        self.assertEqual(choice['finish_reason'], 'tool_calls')
        call = choice['message']['tool_calls'][0]
        converted = ollama_adapter.prepare({'messages': [choice['message'], {'role': 'tool', 'tool_call_id': call['id'], 'content': 'Synthetic result'}]}, self.ep)
        self.assertEqual(converted['messages'][0]['tool_calls'][0]['function']['arguments'], {'name': 'demo'})
        self.assertEqual(converted['messages'][1]['tool_name'], 'lookup')

    def test_incomplete_native_response_is_terminal_without_replay(self):
        self.native['done'] = False
        FaultUpstream.plan = [{'body': json.dumps(self.native).encode()}]
        status, raw, _ = self.call()
        self.assertEqual(status, 424)
        self.assertEqual(json.loads(raw)['error']['code'], 'invalid_ollama_response')
        self.assertEqual(len(FaultUpstream.requests), 1)

    def test_ambiguous_timeout_is_not_retried(self):
        FaultUpstream.plan = [{'status': 504}]
        self.assertEqual(self.call()[0], 424)
        self.assertEqual(len(FaultUpstream.requests), 1)

    def test_forced_tool_cannot_silently_be_ignored(self):
        status, _, _ = self.call(tools=[{'type': 'function', 'function': {'name': 'lookup'}}], tool_choice='required')
        self.assertEqual(status, 424)
        self.assertEqual(len(FaultUpstream.requests), 1)


if __name__ == '__main__':
    unittest.main()

"""Synthetic transport fixture, NOT a real language or embedding model."""
import base64
import hashlib
import json
import math
import re
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/v1/embeddings':
            texts = d['input'] if isinstance(d['input'], list) else [d['input']]
            rows = []
            for i, text in enumerate(texts):
                v = [0.0] * 32
                for token in re.findall(r'[a-z]+', text.lower()):
                    v[int.from_bytes(hashlib.sha256(token.encode()).digest()[:2], 'big') % 32] += 1
                norm = math.sqrt(sum(x*x for x in v)) or 1
                v = [x/norm for x in v]
                if d.get('encoding_format') == 'base64':
                    v = base64.b64encode(struct.pack('<32f', *v)).decode()
                rows.append({'object': 'embedding', 'index': i, 'embedding': v})
            result = {'object': 'list', 'data': rows, 'model': d['model'], 'usage': {'prompt_tokens': 1, 'total_tokens': 1}}
        else:
            msg = {'role': 'assistant', 'content': 'Synthetic API transport verified. Configure your internal LLM to use this environment.'}
            names = [t.get('function',{}).get('name') for t in d.get('tools',[])]
            prompt = str(d.get('messages',[]))
            has_result = any(m.get('role')=='tool' for m in d.get('messages',[]))
            tool = None
            args = {}
            if not has_result and 'environment_probe' in names:
                tool = 'environment_probe'; args = {'ok':True}
            elif not has_result and 'EXERCISE_SEMANTIC' in prompt and 'semantic_search' in names:
                tool = 'semantic_search'; args = {'query':'session token validation active sessions'}
            elif not has_result and 'EXERCISE_LSP' in prompt and 'lsp' in names:
                tool = 'lsp'; args = {'operation':'findReferences','filePath':'/workspace/auth.py','line':4,'character':5}
            if tool:
                msg = {'role':'assistant','content':None,'tool_calls':[{'index':0,'id':'demo-tool-call','type':'function','function':{'name':tool,'arguments':json.dumps(args)}}]}
            result = {'id': 'demo', 'object': 'chat.completion', 'created': 1, 'model': d['model'], 'choices': [{'index': 0, 'message': msg, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
            if tool: result['choices'][0]['finish_reason']='tool_calls'
            if d.get('stream'):
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                chunk = {**result, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': msg, 'finish_reason': None}]}
                self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                chunk['choices'] = [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if tool else 'stop'}]
                self.wfile.write(('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode()); return
        data = json.dumps(result).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)


if __name__ == '__main__': ThreadingHTTPServer(('0.0.0.0', 9000), Handler).serve_forever()

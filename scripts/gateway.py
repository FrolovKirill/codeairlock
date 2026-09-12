"""Fixed-route inference gateway. No generic proxy, DNS, redirects or content logs."""
import http.client
import json
import math
import pathlib
import os
import re
import random
import select
import secrets
import socket
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit
import ollama_adapter

CONFIG = {}
SEARCHES = {}
SEARCH_LOCK = threading.Lock()
SEARCH_TTL = 600
ADMIN_SOCKET = '/tmp/search-admin.sock'
MODEL_ATTEMPTS = 3
MODEL_BUDGET = 120
MODEL_TIMEOUT = 75
TRANSIENT_STATUS = {429, 502, 503}


class ModelGate:
    """One shared inference/embedding connection, bounded waiters and cooldown."""
    def __init__(self, capacity=5, wait_seconds=15, cooldown=30):
        self.condition = threading.Condition()
        self.capacity, self.wait_seconds, self.cooldown = capacity, wait_seconds, cooldown
        self.active = False
        self.waiting = 0
        self.blocked_until = 0
        self.attempts = 0
        self.retries = 0

    def enter(self, disconnected):
        with self.condition:
            if time.monotonic() < self.blocked_until:
                return 'cooldown'
            if int(self.active) + self.waiting >= self.capacity:
                return 'busy'
            deadline = time.monotonic() + self.wait_seconds
            self.waiting += 1
            try:
                while True:
                    if disconnected(): return 'disconnected'
                    if time.monotonic() < self.blocked_until: return 'cooldown'
                    if not self.active:
                        self.active = True
                        return None
                    left = deadline - time.monotonic()
                    if left <= 0: return 'busy'
                    self.condition.wait(min(left, 0.2))
            finally:
                self.waiting -= 1

    def leave(self):
        with self.condition:
            self.active = False
            self.condition.notify_all()

    def pause(self, seconds=None):
        with self.condition:
            self.blocked_until = time.monotonic() + max(self.cooldown, seconds or 0)
            self.condition.notify_all()

    def count_attempt(self, retry):
        with self.condition:
            self.attempts += 1
            self.retries += int(retry)

    def status(self):
        with self.condition:
            return {'active': int(self.active), 'waiting': self.waiting,
                    'cooldown_seconds': max(0, round(self.blocked_until-time.monotonic(), 1)),
                    'upstream_attempts': self.attempts, 'upstream_retries': self.retries}


MODEL_GATE = ModelGate()


def normalize_output_limit(data, endpoint):
    """Apply the operator's output cap before either supported upstream protocol."""
    aliases = [key for key in ('max_tokens', 'max_completion_tokens') if key in data]
    if len(aliases) > 1:
        raise ValueError('Specify only one output limit')
    key = aliases[0] if aliases else 'max_tokens'
    requested = data.get(key, endpoint['max_output'])
    if type(requested) is not int or requested <= 0:
        raise ValueError('Invalid output limit')
    data[key] = min(requested, endpoint['max_output'])


def retry_delay(attempt, retry_after=None):
    delay = 2 ** (attempt + 1) + random.uniform(0, 1)
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            from email.utils import parsedate_to_datetime
            try: delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError): pass
    return delay if math.isfinite(delay) and delay >= 0 else None


def expire_searches():
    # Called under SEARCH_LOCK. Finished results also disappear after ten minutes.
    now = time.monotonic()
    for rid in list(SEARCHES):
        if now - SEARCHES[rid]['created'] >= SEARCH_TTL:
            del SEARCHES[rid]


def search_view(item):
    return {k: v for k, v in item.items() if k != 'created'}


def create_search(data):
    if not CONFIG.get('search'):
        return 403, {'error': 'Search is disabled'}
    if set(data) != {'query'} or not isinstance(data['query'], str):
        return 400, {'error': 'Only query is accepted'}
    query = data['query']
    if not query.strip() or len(query) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in query):
        return 400, {'error': 'Invalid query'}
    with SEARCH_LOCK:
        expire_searches()
        if len(SEARCHES) >= 100:
            return 429, {'error': 'Approval queue is full'}
        rid = secrets.token_hex(16)
        item = {'id': rid, 'query': query, 'status': 'pending', 'created': time.monotonic()}
        SEARCHES[rid] = item
        return 202, search_view(item)


def decide_search(rid, action):
    # Only the Unix-socket admin handler calls this function. Claim once under lock.
    with SEARCH_LOCK:
        expire_searches()
        item = SEARCHES.get(rid)
        if not item:
            return 404, {'error': 'Unknown or expired request'}
        if item['status'] != 'pending':
            return 409, {'error': 'Request already decided'}
        item['status'] = 'running' if action == 'approve' else 'denied'
        query = item['query']
        if action != 'approve':
            return 200, search_view(item)
    endpoint = CONFIG['search']
    conn = connection(endpoint)
    try:
        headers = {'Accept': 'application/json', 'Connection': 'close'}
        if endpoint.get('key'):
            headers['Authorization'] = 'Bearer ' + endpoint['key']
        path = urlsplit(endpoint['url']).path + '?' + urlencode({'q': query, 'format': 'json'})
        conn.request('GET', path, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError('Upstream failed or attempted redirect')
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Oversized search response')
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise ValueError('Invalid SearxNG response')
        # No HTML rendering, URL dereference, images or auxiliary upstream fields.
        results = [{k: str(row.get(k, ''))[:4000] for k in ('title', 'url', 'content')}
                   for row in data['results'][:10] if isinstance(row, dict)]
        outcome = {'status': 'done', 'results': results}
    except (OSError, http.client.HTTPException, ValueError, TypeError):
        outcome = {'status': 'failed', 'error': 'Search upstream failed; a new attempt needs new approval'}
    finally:
        conn.close()
    with SEARCH_LOCK:
        item.update(outcome)
        return 200, search_view(item)


def connection(endpoint):
    url = urlsplit(endpoint['url'])
    cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    kwargs = {'timeout': 180}
    if url.scheme == 'https':
        kwargs['context'] = ssl.create_default_context(cafile=CONFIG.get('ca') or None)
    conn = cls(url.hostname, url.port, **kwargs)
    # Preserve Host / TLS SNI and verification, connect only to pinned IPv4.
    conn._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
        (endpoint['ip'], url.port or (443 if url.scheme == 'https' else 80)), timeout
    )
    return conn


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    def log_message(self, *args):
        pass  # No prompts, paths, search queries, headers, or response bodies in logs.

    def reply(self, status, data, retry_after=None):
        data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        if retry_after is not None:
            self.send_header('Retry-After', str(retry_after))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'ok': True})
        if re.fullmatch(r'/search/requests/[a-f0-9]{32}', self.path):
            with SEARCH_LOCK:
                expire_searches()
                item = SEARCHES.get(self.path.rsplit('/', 1)[-1])
                return self.reply(200, search_view(item)) if item else self.reply(404, {'error': 'Unknown or expired request'})
        self.reply(403, {'error': 'Route denied'})

    def do_POST(self):
        routes = {'/llm/v1/chat/completions': ('llm', '/chat/completions'),
                  '/embed/v1/embeddings': ('embed', '/embeddings')}
        if self.path not in routes and self.path != '/search/requests':
            return self.reply(403, {'error': 'Route denied'})
        if self.headers.get('Transfer-Encoding'):
            return self.reply(400, {'error': 'Chunked request bodies not accepted'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16 * 1024 * 1024:
                return self.reply(413, {'error': 'Invalid body size'})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
            if self.path == '/search/requests':
                return self.reply(*create_search(data))
            name, suffix = routes[self.path]
            endpoint = CONFIG[name]
            # Never allow selecting an accidental cloud-backed or different model.
            if data.get('model') != endpoint['model']:
                return self.reply(403, {'error': 'Model denied'})
            if name == 'llm':
                allowed = {'model', 'messages', 'tools', 'tool_choice', 'stream', 'stream_options',
                           'temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'stop',
                           'presence_penalty', 'frequency_penalty', 'seed', 'parallel_tool_calls', 'response_format'}
                messages = data.get('messages')
                if not isinstance(messages, list):
                    raise ValueError()
                normalize_output_limit(data, endpoint)
                # No remote image/audio/file URLs that a model server might dereference.
                for message in messages:
                    content = message.get('content')
                    if isinstance(content, list):
                        if any(p.get('type') != 'text' or not isinstance(p.get('text'), str) for p in content):
                            return self.reply(403, {'error': 'Only text content is allowed'})
                    elif content is not None and not isinstance(content, str):
                        raise ValueError()
            else:
                allowed = {'model', 'input', 'encoding_format', 'dimensions'}
                inp = data.get('input')
                if not (isinstance(inp, str) or isinstance(inp, list) and all(isinstance(x, str) for x in inp)):
                    raise ValueError()
            if set(data) - allowed:
                return self.reply(403, {'error': 'Unsupported request fields'})
            native = data if name == 'llm' and endpoint.get('ollama') else None
            outgoing = ollama_adapter.prepare(data, endpoint) if native is not None else data
            body = json.dumps(outgoing).encode()
        except (ValueError, KeyError, TypeError, AttributeError):
            return self.reply(400, {'error': 'Invalid request'})
        base = urlsplit(endpoint['url']).path.rstrip('/')
        path = base[:-3] + '/api/chat' if native is not None else base + suffix
        self.forward(endpoint, 'POST', path, body, native)

    def disconnected(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b''
        except OSError:
            return True

    def wait_retry(self, delay, deadline):
        if delay is None or delay > 15 or time.monotonic() + delay >= deadline:
            return False
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if self.disconnected(): return False
            time.sleep(min(0.1, max(0, end-time.monotonic())))
        return not self.disconnected()

    def model_error(self, code, upstream_status=None):
        if self.disconnected(): return
        # 424 is terminal to AI SDK's usual 429/5xx retry loop. Avoid nested retries.
        self.reply(424, {'error': {'message': 'Model request stopped: '+code,
                                 'type': 'gateway_error', 'code': code,
                                 'upstream_status': upstream_status}},
                   retry_after=max(1, math.ceil(MODEL_GATE.status()['cooldown_seconds'])))

    def forward(self, endpoint, method, path, body, native=None):
        gate = MODEL_GATE
        denied = gate.enter(self.disconnected)
        if denied == 'disconnected': return
        if denied == 'cooldown': return self.model_error('upstream_cooldown')
        if denied: return self.reply(429, {'error': 'Model queue busy; try later'}, retry_after=5)
        deadline = time.monotonic() + MODEL_BUDGET
        try:
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Connection': 'close'}
            if endpoint.get('key'):
                headers['Authorization'] = 'Bearer ' + endpoint['key']
            for attempt in range(MODEL_ATTEMPTS):
                if self.disconnected(): return
                if time.monotonic() >= deadline: break
                conn = None
                sent = False
                exposed = False
                transient = None
                pause = retry_delay(attempt)
                try:
                    conn = connection(endpoint)
                    conn.timeout = min(MODEL_TIMEOUT, max(0.1, deadline-time.monotonic()))
                    gate.count_attempt(attempt > 0)
                    conn.connect()  # Connect/TLS failures occur before any POST is sent.
                    sent = True  # A partial write is already an ambiguous POST outcome.
                    conn.request(method, path, body=body, headers=headers)
                    response = conn.getresponse()
                    if response.status in (408, 504):
                        # A proxy timeout does not prove the GPU job was cancelled.
                        gate.pause()
                        return self.model_error('upstream_timeout_unknown_outcome', response.status)
                    if response.status in TRANSIENT_STATUS:
                        transient = response.status
                        pause = retry_delay(attempt, response.getheader('Retry-After'))
                    elif 300 <= response.status < 400:
                        return self.model_error('redirect_denied', response.status)
                    elif response.status == 409 or response.status >= 500:
                        return self.model_error('nonretryable_upstream_response', response.status)
                    elif native is not None and response.status == 200:
                        raw = ollama_adapter.read_response(response)
                        content_type, converted = ollama_adapter.finish(native, endpoint, raw)
                        if self.disconnected(): return
                        exposed = True
                        self.send_response(200)
                        self.send_header('Content-Type', content_type)
                        self.send_header('Content-Length', str(len(converted)))
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.wfile.write(converted)
                        self.wfile.flush()
                        return
                    else:
                        exposed = True  # Never retry after response headers or stream bytes.
                        self.send_response(response.status)
                        ct = response.getheader('Content-Type', 'application/json')
                        self.send_header('Content-Type', 'text/event-stream' if 'text/event-stream' in ct else 'application/json')
                        length = response.getheader('Content-Length', '')
                        if length.isdigit(): self.send_header('Content-Length', length)
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        while chunk := response.read1(65536):
                            if self.disconnected(): return
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        return
                except (ValueError, KeyError, TypeError, AttributeError):
                    gate.pause()
                    return self.model_error('invalid_ollama_response')
                except ssl.SSLError:
                    if exposed:
                        self.close_connection = True
                        return
                    return self.model_error('tls_error')
                except (OSError, http.client.HTTPException):
                    if exposed:
                        self.close_connection = True
                        return
                    if sent:
                        # The backend may still be computing. No blind replay of a POST.
                        gate.pause()
                        return self.model_error('ambiguous_transport_failure')
                    transient = 'connect_failure'
                finally:
                    if conn is not None: conn.close()
                if attempt+1 >= MODEL_ATTEMPTS or not self.wait_retry(pause, deadline):
                    if self.disconnected(): return
                    gate.pause(pause)
                    return self.model_error('retry_budget_exhausted', transient)
            gate.pause()
            self.model_error('retry_deadline_exceeded')
        finally:
            gate.leave()


class AdminHandler(Handler):
    """No TCP listener: available only inside the trusted gateway container."""
    def do_GET(self):
        if self.path == '/model-status':
            return self.reply(200, MODEL_GATE.status())
        if self.path != '/pending':
            return self.reply(403, {'error': 'Route denied'})
        with SEARCH_LOCK:
            expire_searches()
            self.reply(200, {'requests': [search_view(x) for x in SEARCHES.values() if x['status'] == 'pending']})

    def do_POST(self):
        match = re.fullmatch(r'/(approve|deny)/([a-f0-9]{32})', self.path)
        if not match or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
            return self.reply(403, {'error': 'Route denied'})
        self.reply(*decide_search(match[2], match[1]))


class AdminServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if __name__ == '__main__':
    CONFIG = json.loads(pathlib.Path('/config/gateway.json').read_text())
    os.umask(0o077)
    pathlib.Path(ADMIN_SOCKET).unlink(missing_ok=True)
    admin = AdminServer(ADMIN_SOCKET, AdminHandler)
    os.chmod(ADMIN_SOCKET, 0o600)
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', 8081), Handler)
    server.timeout = 30
    server.serve_forever()

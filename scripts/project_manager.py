"""Loopback-only host control plane. It never reads repository files or serves their contents."""
import argparse
import fcntl
import hmac
import json
import os
import pathlib
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from configure import ROOT, RUNTIME, envfile, write
from projects import add_project, get_project, list_projects, update_access, locked

CLI = ROOT / ('codeairlock' if (ROOT / 'codeairlock').exists() else 'secretgpt')


class Operations:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.process = None
        self.cancelled = threading.Event()
        self.busy = False
        self.phase = ''
        self.error = ''
        self.needs_reindex = False
        self.target = None
    def running_project(self):
        if not (RUNTIME / 'compose.json').exists():
            return None
        try:
            compose = ['docker', 'compose', '-f', str(RUNTIME / 'compose.json')]
            result = subprocess.run([*compose, 'ps', '-q', 'workstation'], capture_output=True, text=True, timeout=5)
            container = result.stdout.strip()
            if result.returncode or not container:
                return None
            # Project identity and running state come from the same container snapshot.
            result = subprocess.run(['docker', 'inspect', '--format',
                '{{.State.Running}} {{index .Config.Labels "io.codeairlock.project"}}', container],
                capture_output=True, text=True, timeout=5)
            fields = result.stdout.strip().split()
            return fields[1] if result.returncode == 0 and len(fields) == 2 and fields[0] == 'true' else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def state(self):
        external_busy = False
        active = None
        try:
            with locked('operation', blocking=False, shared=True):
                active = self.running_project()
        except BlockingIOError:
            external_busy = True
        data = list_projects()
        if active not in {p['id'] for p in data['projects']}:
            active = None
        return {**data, 'active': active, 'busy': self.busy or external_busy,
                'phase': self.phase if self.busy else 'CLI operation in progress' if external_busy else self.phase,
                'error': self.error, 'needs_reindex': self.needs_reindex, 'target': self.target}

    def password(self, project_id):
        # Keep identity validation and credential access in one lifecycle snapshot.
        with locked('operation', blocking=False, shared=True):
            active = self.running_project()
            if (self.busy or not active or active != project_id
                    or active not in {p['id'] for p in list_projects()['projects']}):
                raise ValueError('This project is not ready. Wait for it to open.')
            path = RUNTIME / 'project-passwords' / (active + '.txt')
            return {'id': active, 'password': path.read_text().strip()}

    def launch(self, project_id=None, access=None, reindex=False, stop=False):
        with self.lock:
            if self.busy:
                raise RuntimeError('Another operation is running. Stop it first.')
            if not stop:
                get_project(project_id)
                update_access(project_id, access)
            self.busy = True
            self.cancelled.clear()
            self.error = ''
            self.needs_reindex = False
            self.target = project_id
            self.phase = 'Stopping' if stop else 'Starting and indexing project'
            self.thread = threading.Thread(target=self._work, args=(project_id, reindex, stop), daemon=True)
            self.thread.start()

    def _command(self, arguments, log):
        with self.lock:
            if self.cancelled.is_set():
                return False
            self.process = subprocess.Popen([sys.executable, str(CLI), *arguments], cwd=ROOT,
                                            stdout=log, stderr=log, start_new_session=True)
            process = self.process
        code = process.wait()
        with self.lock:
            self.process = None
        return code == 0 and not self.cancelled.is_set()

    def _work(self, project_id, reindex, stop):
        log_path = RUNTIME / 'project-operation.log'
        try:
            with log_path.open('w') as log:
                log_path.chmod(0o600)
                args = ['down'] if stop else ['up', '--project', project_id, '--index'] + (['--reindex'] if reindex else [])
                if not self._command(args, log):
                    raise RuntimeError()
                self.phase = 'Stopped' if stop else 'Ready'
        except Exception:
            # Never expose Docker/Kilo logs (which may contain secrets) through the web API.
            text = log_path.read_text(errors='replace') if log_path.exists() else ''
            self.needs_reindex = 'up --reindex' in text
            self.error = ('Embedding identity changed. Rebuild the index to open this project.' if self.needs_reindex
                          else 'Operation failed. Check Docker and runtime/project-operation.log locally.')
            self.phase = 'Failed'
        finally:
            if self.cancelled.is_set():
                # The CLI transaction cleans up under its operation lock on SIGINT.
                # A second down here could accidentally stop a later CLI project.
                self.phase = 'Stopped'
                self.error = ''
            with self.lock:
                self.busy = False
                self.process = None

    def cancel(self):
        with self.lock:
            if not self.busy:
                return False
            if self.phase == 'Stopping':
                return True
            self.cancelled.set()
            if self.process and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Authorization headers, local paths and tokens must never enter access logs.

    def reply(self, status, body, content_type='application/json'):
        raw = json.dumps(body).encode() if content_type == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-src http://127.0.0.1:" + str(self.server.ui_port) + "; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self, api=False):
        if self.headers.get('Host') != self.server.authority:
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.server.authority:
            return False
        if api:
            expected = 'Bearer ' + self.server.token
            return hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected.encode())
        return True

    def do_GET(self):
        if not self.allowed(api=self.path.startswith('/api/')):
            return self.reply(403, {'error': 'Access denied.'})
        if self.path == '/api/state':
            state = self.server.operations.state()
            state['ui_port'] = self.server.ui_port
            return self.reply(200, state)
        static = {'/': ('projects.html', 'text/html; charset=utf-8'),
                  '/projects.js': ('projects.js', 'text/javascript; charset=utf-8'),
                  '/projects.css': ('projects.css', 'text/css; charset=utf-8')}
        if self.path in static:
            filename, mime = static[self.path]
            return self.reply(200, (ROOT / 'ui' / filename).read_bytes(), mime)
        self.reply(404, {'error': 'Not found.'})

    def do_OPTIONS(self):
        self.reply(403, {'error': 'Cross-origin access is disabled.'})

    def do_POST(self):
        if not self.allowed(api=True):
            return self.reply(403, {'error': 'Access denied.'})
        try:
            if self.headers.get('Content-Type') != 'application/json':
                raise ValueError('JSON required.')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('JSON object required.')
            operations = self.server.operations
            if self.path == '/api/projects':
                if not all(isinstance(data.get(k), str) for k in ('name', 'path', 'access')):
                    raise ValueError('Name, path and access are required.')
                return self.reply(201, add_project(data['name'], data['path'], data['access']))
            if self.path == '/api/open':
                if not isinstance(data.get('id'), str) or type(data.get('reindex', False)) is not bool:
                    raise ValueError('Invalid project selection.')
                operations.launch(data['id'], data.get('access'), data.get('reindex', False))
                return self.reply(202, {'accepted': True})
            if self.path == '/api/stop':
                if not operations.cancel():
                    operations.launch(stop=True)
                return self.reply(202, {'accepted': True})
            if self.path == '/api/password':
                return self.reply(200, operations.password(data.get('id')))
            if self.path == '/api/choose-folder':
                if sys.platform != 'darwin':
                    raise ValueError('Enter the absolute folder path on this host.')
                result = subprocess.run(['osascript', '-e', 'POSIX path of (choose folder with prompt "Choose a repository for CodeAirlock")'],
                                        capture_output=True, text=True, timeout=120)
                return self.reply(200, {'path': result.stdout.strip() if result.returncode == 0 else ''})
            return self.reply(404, {'error': 'Not found.'})
        except ValueError as error:
            self.reply(400, {'error': str(error)})
        except RuntimeError as error:
            self.reply(409, {'error': str(error)})
        except BlockingIOError:
            self.reply(409, {'error': 'Project is switching. Wait until it is ready.'})
        except Exception:
            self.reply(500, {'error': 'Host operation failed. Check local configuration.'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=6090)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit('Use a port between 1024 and 65535.')
    ui_port = int(envfile().get('UI_PORT', '6080'))
    if not 1024 <= ui_port <= 65535 or ui_port == args.port:
        raise SystemExit('UI_PORT must be valid and different from the project manager port.')
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    handle = (RUNTIME / 'manager.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Project manager is already running; using its existing local session.')
        if not args.no_browser:
            webbrowser.open((RUNTIME / 'manager-url.txt').read_text().strip())
        return
    list_projects()  # One-time legacy migration before any project can be added.
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.authority = f'127.0.0.1:{args.port}'
    server.token = secrets.token_urlsafe(32)
    server.ui_port = ui_port
    server.operations = Operations()
    url = f'http://{server.authority}/#token={server.token}'
    write(RUNTIME / 'manager-url.txt', url, 0o600)
    write(RUNTIME / 'manager.pid', str(os.getpid()), 0o600)
    print(f'Project manager: http://{server.authority}/ (open the authenticated link in runtime/manager-url.txt).', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.operations.cancel()
        if server.operations.thread:
            server.operations.thread.join(timeout=60)
        server.server_close()
        handle.close()


if __name__ == '__main__':
    main()

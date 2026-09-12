"""Wait for the editor's index. Never spawn another server or change consent."""
import base64
import json
import pathlib
import subprocess
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATES = {'Complete', 'Error', 'Disabled', 'Standby', 'Indexing', 'Initializing'}


def editor_servers(proc=pathlib.Path('/proc')):
    """Find authenticated editor-owned listeners without reading logs or sessions."""
    listeners = {}
    for row in (proc / 'net/tcp').read_text().splitlines()[1:]:
        cols = row.split()
        address, port = cols[1].split(':')
        if cols[3] == '0A' and address in ('0100007F', '00000000'):
            listeners['socket:[' + cols[9] + ']'] = int(port, 16)
    seen = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            env = dict(part.split(b'=', 1) for part in (entry / 'environ').read_bytes().split(b'\0') if b'=' in part)
            password = env.get(b'KILO_SERVER_PASSWORD')
            if env.get(b'KILO_CLIENT') != b'vscode' or not password:
                continue
            for fd in (entry / 'fd').iterdir():
                try:
                    port = listeners.get(str(fd.readlink()))
                except OSError:
                    continue
                if port and (port, password) not in seen:
                    seen.add((port, password))
                    yield port, password
        except (OSError, ValueError):
            continue


def get(port, password, path):
    auth = base64.b64encode(b'kilo:' + password).decode()
    req = urllib.request.Request('http://127.0.0.1:' + str(port) + path,
                                 headers={'Authorization': 'Basic ' + auth})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        return json.loads(response.read(65537))


def visible(status):
    result = {'state': status.get('state') if status.get('state') in STATES else 'Unknown'}
    for key in ('percent', 'processedFiles', 'totalFiles'):
        value = status.get(key)
        if type(value) in (int, float) and 0 <= value <= 10**12:
            result[key] = value
    return result


def wait_for_index():
    deadline = time.monotonic() + 30
    server = None
    while time.monotonic() < deadline:
        for port, password in editor_servers():
            try:
                health = get(port, password, '/global/health')
                if health.get('healthy') is True and health.get('version') == '7.5.16':
                    server = (port, password)
                    break
            except Exception:
                continue
        if server:
            break
        time.sleep(1)
    if not server:
        print('Open the Kilo extension, enable indexing there, then run index again. Environment remains running.')
        return 1
    deadline = time.monotonic() + 1800
    previous = None
    while time.monotonic() < deadline:
        status = get(*server, '/indexing/status?directory=/workspace')
        safe = visible(status)
        if safe != previous:
            print(json.dumps(safe), flush=True)
            previous = safe
        if status.get('state') == 'Complete':
            print('Editor index complete. No chat request was sent.', flush=True)
            return 0
        if status.get('state') in ('Disabled', 'Standby'):
            print('Enable indexing in Kilo, then run index again. Environment remains running.')
            return 1
        if status.get('state') == 'Error':
            print('Editor indexing failed. Inspect details locally in Kilo.')
            return 1
        time.sleep(3)
    print('Editor index wait timed out. Environment remains running.')
    return 1


if __name__ == '__main__':
    import sys
    if '--inside' in sys.argv:
        try:
            code = wait_for_index()
        except Exception:
            print('Cannot read editor indexing status. Open Kilo and retry; private error details withheld.')
            code = 1
        raise SystemExit(code)
    result = subprocess.run(['docker', 'compose', '-f', str(ROOT / 'runtime/compose.json'),
                             'exec', '-T', 'workstation', 'python3', '-', '--inside'],
                            input=pathlib.Path(__file__).read_text(), text=True)
    raise SystemExit(result.returncode)

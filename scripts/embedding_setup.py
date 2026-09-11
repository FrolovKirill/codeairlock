"""Detect embeddings through the fixed gateway; keep index identity in the home volume."""
import json
import math
import pathlib
import re
import sys
import time
import urllib.request
import uuid

GATEWAY = 'http://172.30.88.3:8081/embed/v1/embeddings'


class SetupError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_for_gateway():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with opener.open('http://172.30.88.3:8081/health', timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise SetupError('Gateway did not become ready; no embedding request was sent.')


def detect_dimension(model):
    # No dimensions argument: ask for the model's native vector length.
    body = {'model': model, 'input': ['Embedding dimension check.'], 'encoding_format': 'float'}
    request = urllib.request.Request(GATEWAY, json.dumps(body).encode(),
                                     {'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=180) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError()
        data = json.loads(raw)['data']
        if not isinstance(data, list) or len(data) != 1:
            raise ValueError()
        vector = data[0]['embedding']
        if (not isinstance(vector, list) or not 1 <= len(vector) <= 65536
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)):
            raise ValueError()
        return len(vector)
    except Exception:
        # Upstream errors may contain sensitive request/endpoint details.
        raise SetupError('Embedding probe failed or returned an invalid vector; workstation was not started.') from None


def prepare_index(settings, dimension, home, reindex=False):
    expected = settings['expected_dimension']
    if expected is not None and expected != dimension:
        raise SetupError(f'EMBED_DIMENSION expects {expected}, endpoint returned {dimension}. Fix or clear the setting.')
    repository = settings['repository']
    if not re.fullmatch(r'[0-9a-f]{64}', repository):
        raise SetupError('Invalid repository identity.')
    identity = {**settings['identity'], 'dimension': dimension}
    profiles = home / 'index-profiles'
    manifest = profiles / (repository + '.json')
    previous = None
    if manifest.exists():
        try:
            previous = json.loads(manifest.read_text())
            if previous['version'] != 1 or not isinstance(previous['identity'], dict):
                raise ValueError()
            if not re.fullmatch(r'[0-9a-f]{32}', previous['index_id']):
                raise ValueError()
        except Exception:
            if not reindex:
                raise SetupError('Index metadata is invalid. Run up --reindex to create a fresh index.') from None
            previous = None
    if previous and previous['identity'] != identity and not reindex:
        raise SetupError('Embedding model, endpoint, pinned IP, revision or dimension changed. Run up --reindex, then index. Existing indexes and sessions are preserved.')
    fresh = previous is None or reindex
    index_id = uuid.uuid4().hex if fresh else previous['index_id']
    directory = home / 'index' / index_id
    directory.mkdir(parents=True, exist_ok=True)
    if fresh:
        profiles.mkdir(parents=True, exist_ok=True)
        temp = manifest.with_suffix('.tmp')
        temp.write_text(json.dumps({'version': 1, 'identity': identity, 'index_id': index_id}, indent=2))
        temp.chmod(0o600)
        temp.replace(manifest)
    return {'dimension': dimension, 'directory': str(directory), 'fresh': fresh}


if __name__ == '__main__':
    try:
        settings = json.loads(pathlib.Path('/embedding-setup.json').read_text())
        wait_for_gateway()
        dimension = detect_dimension(settings['identity']['model'])
        result = prepare_index(settings, dimension, pathlib.Path('/home/node'), '--reindex' in sys.argv[1:])
        print(json.dumps(result))
    except SetupError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print('Embedding setup failed; inspect local configuration and volume permissions.', file=sys.stderr)
        raise SystemExit(1)

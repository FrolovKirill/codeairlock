"""Host-only search approval bridge. Never mounted in the workstation."""
import json
import re
import subprocess
from configure import RUNTIME
from projects import locked

PROGRAM = '''
import sys,json
sys.path.insert(0,"/opt")
from search_admin import call
action,rid,query = json.load(sys.stdin)
if action == "list":
    result = call("GET", "/pending")
else:
    item = next((x for x in call("GET", "/pending")["requests"] if x["id"] == rid), None)
    if not item or item["query"] != query:
        raise SystemExit("Query expired or changed; refresh before deciding")
    item = call("POST", "/"+action+"/"+rid)
    result = {"id": item["id"], "status": item["status"]}
print(json.dumps(result))
'''


def control(action='list', rid=None, query=None):
    if action not in ('list', 'approve', 'deny'):
        raise ValueError('Invalid search decision')
    if action != 'list' and (not isinstance(rid, str) or not re.fullmatch(r'[a-f0-9]{32}', rid)
                             or not isinstance(query, str) or not 0 < len(query) <= 500):
        raise ValueError('Invalid search request')
    with locked('operation', blocking=False, shared=True):
        result = subprocess.run(['docker', 'compose', '-f', str(RUNTIME/'compose.json'),
                                 'exec', '-T', 'gateway', 'python3', '-c', PROGRAM],
                                input=json.dumps([action, rid, query]), text=True,
                                capture_output=True, timeout=195)
    if result.returncode:
        raise ValueError('Search unavailable or request expired. Refresh before trying again.')
    return json.loads(result.stdout)

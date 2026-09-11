"""Pre-index inside the isolated workstation; print only status/counts, never code."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROGRAM = r'''
import json,pathlib,socket,subprocess,time,urllib.request
# Reserve a separate loopback port: the editor extension owns/authenticates 4096.
BASE='http://127.0.0.1:49161'
def get(path):
    with urllib.request.urlopen(BASE+path,timeout=15) as response:
        return json.load(response)

# This loopback port is not published outside the workstation namespace.
sock=socket.socket();sock.settimeout(1)
listening=sock.connect_ex(('127.0.0.1',49161))==0;sock.close()
if not listening:
    with open('/tmp/codeairlock-index-server.log','ab') as log:
        subprocess.Popen(['kilo','serve','--hostname','127.0.0.1','--port','49161'],
                         cwd='/workspace',stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                         start_new_session=True)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        try:
            if get('/global/health').get('healthy'): break
        except Exception: pass
        time.sleep(1)
health=get('/global/health')
if health.get('healthy') is not True or health.get('version')!='7.5.16':
    raise SystemExit('Unexpected local service; no indexing requested.')
deadline=time.monotonic()+1800
previous=None
while time.monotonic()<deadline:
    status=get('/indexing/status?directory=/workspace')
    # The upstream message can include paths or errors containing sensitive data.
    # Only these fixed fields are safe to show in operator/automation output.
    visible={k:status.get(k) for k in ('state','percent','processedFiles','totalFiles')}
    if visible!=previous:
        print(json.dumps(visible),flush=True);previous=visible
    if status.get('state')=='Complete':
        print('Index complete. No chat request was sent. You may now use Kilo.',flush=True)
        break
    if status.get('state') in ('Error','Disabled','Standby'):
        raise SystemExit('Index is not ready; inspect indexing settings locally. No chat request sent.')
    time.sleep(3)
else: raise SystemExit('Index wait timed out; no chat request sent.')
'''

if __name__ == '__main__':
    result = subprocess.run(['docker','compose','-f',str(ROOT/'runtime/compose.json'),
                             'exec','-T','workstation','python3','-'], input=PROGRAM,text=True)
    raise SystemExit(result.returncode)

"""Synthetic-only acceptance probes. Never load or print repository contents."""
import concurrent.futures
import json
import pathlib
import socket
import sys
import urllib.error
import urllib.request

GATE = 'http://172.30.88.3:8081'
cfg = json.loads(pathlib.Path('/config/kilo.json').read_text())
checks = []
def check(label, ok):
    checks.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + label, flush=True)
def request(path, body=None):
    req = urllib.request.Request(GATE+path, data=json.dumps(body).encode() if body is not None else None, headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=180) as response: return response.status,json.load(response)
    except urllib.error.HTTPError as e: return e.code,{}
def blocked(target):
    try:
        with socket.create_connection(target,timeout=1.5): return False
    except OSError: return True

targets = [('public IPv4 HTTPS',('1.1.1.1',443)),('public DNS TCP',('8.8.8.8',53)),
           ('Docker DNS TCP',('127.0.0.11',53)),('Docker host gateway',('172.30.88.1',80)),
           ('cloud metadata',('169.254.169.254',80)),('public IPv6',('2606:4700:4700::1111',443)),
           ('UI cannot be used as outbound HTTP proxy',('172.30.88.4',6080))]
with concurrent.futures.ThreadPoolExecutor() as pool:
    for (label,_), ok in zip(targets,pool.map(blocked,[x[1] for x in targets])): check(label+' blocked',ok)
for ip in ['127.0.0.11','8.8.8.8']:
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.settimeout(1.5)
    # Fixed public, non-secret DNS question. No repository-derived bytes.
    query=b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01'
    try:
        sock.sendto(query,(ip,53)); sock.recvfrom(4096);ok=False
    except OSError: ok=True
    finally:sock.close()
    check('UDP DNS blocked at '+ip,ok)
status,_=request('/health');check('fixed gateway reachable',status==200)
for path in ['/llm/v1/models','/llm/v1/chat/completions?url=https://example.com','/search/arbitrary-secret','/search/topics?secret=test','/http://example.com/']:
    status,_=request(path);check('non-allowlisted route denied',status==403)
model=cfg['model'].split('/',1)[1]
status,_=request('/llm/v1/chat/completions',{'model':'unapproved-model','messages':[]});check('unapproved model denied',status==403)
status,_=request('/llm/v1/chat/completions',{'model':model,'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'https://example.com'}}]}]});check('remote model-side image fetch denied',status==403)
status,response=request('/llm/v1/chat/completions',{'model':model,'messages':[{'role':'user','content':'Synthetic connectivity check. Reply OK.'}],'max_tokens':32,'stream':False})
check('internal LLM response',status==200 and bool(response.get('choices')))
status,response=request('/llm/v1/chat/completions',{'model':model,'messages':[{'role':'user','content':'Call environment_probe with ok=true.'}],
    'tools':[{'type':'function','function':{'name':'environment_probe','description':'Synthetic tool compatibility check','parameters':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}}}],
    'tool_choice':{'type':'function','function':{'name':'environment_probe'}},'max_tokens':128,'stream':False})
choices=response.get('choices',[])
calls=choices[0].get('message',{}).get('tool_calls',[]) if choices else []
check('LLM native tool calling',status==200 and bool(calls) and calls[0].get('function',{}).get('name')=='environment_probe')
status,response=request('/embed/v1/embeddings',{'model':cfg['indexing']['model'],'input':['Synthetic connectivity check.'],'encoding_format':'float'})
data=response.get('data',[])
check('internal embeddings and dimension',status==200 and bool(data) and len(data[0].get('embedding',[]))==cfg['indexing']['dimension'])
status=pathlib.Path('/proc/self/status').read_text()
check('no effective Linux capabilities','CapEff:\t0000000000000000' in status)
check('no_new_privs enabled','NoNewPrivs:\t1' in status)
check('no Docker socket',not pathlib.Path('/var/run/docker.sock').exists())
check('repository read-only mount',any(' /workspace ' in line and ' ro,' in line for line in pathlib.Path('/proc/mounts').read_text().splitlines()))
raise SystemExit(0 if all(checks) else 1)

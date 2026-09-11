"""Audit each namespace from the operator host, without repository access."""
import json
import pathlib
import subprocess
import sys

root=pathlib.Path(__file__).resolve().parents[1]
compose=['docker','compose','-f',str(root/'runtime/compose.json')]
program='''
import json,socket,concurrent.futures
targets=json.loads(__import__('sys').argv[1])
def blocked(target):
 try:
  with socket.create_connection(tuple(target),timeout=1.5):return False
 except OSError:return True
with concurrent.futures.ThreadPoolExecutor() as pool:
 print(json.dumps(list(pool.map(blocked,targets))))
'''
checks={
 'workstation': [('172.30.88.1',6090),('1.1.1.1',443),('127.0.0.11',53),('172.30.88.4',6080),('172.30.88.5',9000)],
 'gateway': [('1.1.1.1',443),('127.0.0.11',53),('172.30.88.4',6080)],
 'ui': [('1.1.1.1',443),('127.0.0.11',53),('172.30.88.3',8081)],
}
passed=True
for service,targets in checks.items():
 out=subprocess.run([*compose,'exec','-T',service,'python3','-c',program,json.dumps(targets)],check=True,capture_output=True,text=True)
 result=json.loads(out.stdout)
 ok=all(result);passed=passed and ok
 print(('PASS ' if ok else 'FAIL ')+service+': all disallowed outbound connections blocked')
for service in ['workstation-net','gateway-net','ui-net']:
 out=subprocess.run([*compose,'exec','-T',service,'nft','-j','list','table','inet','private_env'],check=True,capture_output=True,text=True)
 entries=json.loads(out.stdout)['nftables']
 chains={x['chain']['name']:x['chain'] for x in entries if 'chain' in x}
 ok=all(chains[name]['policy']=='drop' for name in ['input','output','forward']) and chains['early_output']['prio']==-300
 passed=passed and ok
 print(('PASS ' if ok else 'FAIL ')+service+': kernel default-deny and pre-NAT DNS policy installed')
print('This audit sends only fixed synthetic connection probes; no repository bytes are read.')
sys.exit(0 if passed else 1)

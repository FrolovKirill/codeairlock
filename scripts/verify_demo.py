"""End-to-end harness check, refuses to run against any non-demo repository."""
import datetime
import json
import pathlib
import subprocess
import sys

root=pathlib.Path(__file__).resolve().parents[1]
compose=['docker','compose','-f',str(root/'runtime/compose.json')]
def run(args,**kwargs):return subprocess.run(args,check=True,text=True,**kwargs)
real = sys.argv[1:] == ['--real-model']
if sys.argv[1:] and not real: raise SystemExit('Unknown option')
if not real and (root/'runtime/mode').read_text().strip()!='demo':raise SystemExit('Refusing: use --real-model explicitly for trusted endpoints with the synthetic fixture.')
cid=run([*compose,'ps','-q','workstation'],capture_output=True).stdout.strip()
container=json.loads(run(['docker','inspect',cid],capture_output=True).stdout)[0]
mounts=[m for m in container['Mounts'] if m['Destination']=='/workspace']
if len(mounts)!=1 or pathlib.Path(mounts[0]['Source']).resolve()!=root/'demo-repo':
    raise SystemExit('Refusing: /workspace is not the known synthetic fixture.')
results={}
prompts = [('semantic_search', 'Use semantic_search to find the function that validates login sessions in this synthetic project. Query: validate login session token. Report the matching filename.'),
           ('lsp', 'Use the lsp tool with operation findReferences, filePath /workspace/auth.py, line 4, character 5. Report the reference count.')]
if not real: prompts = [('semantic_search','EXERCISE_SEMANTIC'),('lsp','EXERCISE_LSP')]
for name,marker in prompts:
    process=subprocess.run([*compose,'exec','-T','workstation','timeout','180' if real else '110','kilo','run','--format','json',marker],capture_output=True,text=True)
    events=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
    state=next((e['part']['state'] for e in events if e.get('type')=='tool_use' and e.get('part',{}).get('tool')==name),{})
    metadata=state.get('metadata',{})
    rows=metadata.get('results' if name=='semantic_search' else 'result',[])
    ok=state.get('status')=='completed' and len(rows)>0
    if name=='semantic_search':ok=ok and any(r.get('filePath')=='auth.py' for r in rows)
    if name=='lsp':ok=ok and len(rows)>=2
    results[name]={'passed':ok,'result_count':len(rows),'process_exit':process.returncode}
    if real and not ok:
        (root/'runtime'/('connected-'+name+'-diagnostic.jsonl')).write_text(process.stdout)
    print(('PASS ' if ok else 'FAIL ')+name+': '+str(len(rows))+' results on synthetic fixture')
report={'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'scope':('Real trusted endpoints, synthetic repository only.' if real else 'Synthetic API fixture only. No real model quality assessment or private repository reads.'),
        'workstation_image':container['Image'],'results':results}
(root/'runtime'/('connected-harness-verification.json' if real else 'harness-verification.json')).write_text(json.dumps(report,indent=2)+'\n')
sys.exit(0 if all(r['passed'] for r in results.values()) else 1)

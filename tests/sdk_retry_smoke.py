"""Real Kilo client versus synthetic 503 backend, entirely network-none."""
import http.server,json,os,pathlib,subprocess,sys,threading
sys.path.insert(0,'/test/scripts')
import gateway
class Failing(http.server.BaseHTTPRequestHandler):
    calls=0
    def log_message(self,*args):pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        type(self).calls+=1
        self.send_response(503);self.send_header('Content-Length','0');self.end_headers()
up=http.server.ThreadingHTTPServer(('127.0.0.1',0),Failing)
proxy=http.server.ThreadingHTTPServer(('127.0.0.1',0),gateway.Handler)
gateway.CONFIG={'llm':{'url':f'http://synthetic.invalid:{up.server_port}/v1','ip':'127.0.0.1','model':'synthetic'},'embed':{}}
for s in (up,proxy):threading.Thread(target=s.serve_forever,daemon=True).start()
root=pathlib.Path('/tmp/retry-fixture');root.mkdir()
config={'model':'internal/synthetic','small_model':'internal/synthetic','enabled_providers':['internal'],
        'agent':{'title':{'disable':True}},'share':'disabled','autoupdate':False,
        'provider':{'internal':{'npm':'@ai-sdk/openai-compatible','options':{'baseURL':f'http://127.0.0.1:{proxy.server_port}/llm/v1','apiKey':'synthetic'},'models':{'synthetic':{'limit':{'context':8192,'output':256}}}}}}
p=pathlib.Path('/tmp/retry-config.json');p.write_text(json.dumps(config))
env={**os.environ,'KILO_CONFIG':str(p),'KILO_CONFIG_CONTENT':json.dumps({'indexing':{'enabled':False}})}
try:
    result=subprocess.run(['kilo','run','--format','json','Synthetic retry test. Reply OK.'],cwd=root,env=env,capture_output=True,text=True,timeout=35)
    events=[json.loads(x) for x in result.stdout.splitlines() if x.startswith('{')]
    print(json.dumps({'kilo_exit':result.returncode,'upstream_calls':Failing.calls,'gateway':gateway.MODEL_GATE.status(),'event_types':[x.get('type') for x in events]}))
    assert Failing.calls==3,'Unexpected extra backend attempts'
    assert 'retry_budget_exhausted' in result.stdout+result.stderr,'Expected terminal gateway error not surfaced'
    print('PASS real Kilo surfaces terminal error without multiplying backend retries')
finally:
    for s in (up,proxy):s.shutdown();s.server_close()

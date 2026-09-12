import http.client
import json
import pathlib
import sys
import threading
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import gateway
from configure import endpoint


class Upstream(BaseHTTPRequestHandler):
    requests = []
    redirect = False
    def log_message(self,*args):pass
    def do_GET(self):self.respond()
    def do_POST(self):self.respond()
    def respond(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.requests.append((self.path,dict(self.headers),body))
        self.send_response(302 if self.redirect else 200)
        self.send_header('Location','https://example.com/never-follow')
        self.send_header('Content-Type','application/json')
        self.end_headers();self.wfile.write(b'{"ok":true,"results":[{"title":"Synthetic","url":"https://example.com/never-fetch","content":"Test only"}]}')


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),gateway.Handler)
        cls.temp = tempfile.TemporaryDirectory()
        cls.admin = gateway.AdminServer(cls.temp.name + '/admin.sock', gateway.AdminHandler)
        for server in [cls.up,cls.server,cls.admin]:threading.Thread(target=server.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for server in [cls.up,cls.server,cls.admin]:server.shutdown();server.server_close()
        cls.temp.cleanup()
    def setUp(self):
        Upstream.requests=[];Upstream.redirect=False
        gateway.SEARCHES.clear()
        gateway.MODEL_GATE = gateway.ModelGate()
        ep={'url':f'http://does-not-resolve.invalid:{self.up.server_port}/v1','ip':'127.0.0.1','model':'local','key':'fixed-key','max_output':64}
        gateway.CONFIG={'llm':ep,'embed':ep,'search':{**ep,'url':ep['url'].replace('/v1','/search')},'topics':{'python':'Python pathlib official documentation'}}
    def request(self,path,body=None,headers=None,method=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        conn.request(method or ('POST' if body is not None else 'GET'),path,json.dumps(body) if body is not None else None,headers or {})
        r=conn.getresponse();out=(r.status,r.read(),dict(r.getheaders()));conn.close();return out
    def test_inference_and_header_isolation(self):
        status,_,headers=self.request('/llm/v1/chat/completions',{'model':'local','messages':[{'role':'user','content':'synthetic'}]}, {'X-Secret':'not-forwarded','Authorization':'Bearer caller','Host':'evil.invalid'})
        self.assertEqual(status,200)
        path,h,b=Upstream.requests[-1]
        self.assertEqual(path,'/v1/chat/completions')
        self.assertEqual(h['Authorization'],'Bearer fixed-key')
        self.assertNotIn('X-Secret',h)
        self.assertNotIn('Location',headers)
        self.assertEqual(json.loads(b)['model'],'local')
        self.assertEqual(json.loads(b)['max_tokens'],64)
    def test_output_limit_is_clamped_and_aliases_are_unambiguous(self):
        for key in ('max_tokens','max_completion_tokens'):
            with self.subTest(key=key):
                self.assertEqual(self.request('/llm/v1/chat/completions',
                    {'model':'local','messages':[],key:1000})[0],200)
                self.assertEqual(json.loads(Upstream.requests[-1][2])[key],64)
        invalid = [
            {'max_tokens':0}, {'max_tokens':True}, {'max_completion_tokens':'64'},
            {'max_tokens':1,'max_completion_tokens':1},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                before=len(Upstream.requests)
                self.assertEqual(self.request('/llm/v1/chat/completions',
                    {'model':'local','messages':[],**fields})[0],400)
                self.assertEqual(len(Upstream.requests),before)
    def test_reject_path_and_query_bypasses(self):
        for p in ['/llm/v1/chat/completions?secret=x','/search/python?secret=x','/search/%70ython','/search/../python','http://example.com/','/llm/v1/models']:
            with self.subTest(p=p):self.assertEqual(self.request(p)[0],403)
        self.assertEqual(Upstream.requests,[])
    def admin_request(self, path):
        import socket
        conn=http.client.HTTPConnection('localhost',timeout=3)
        conn.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        conn.sock.connect(self.temp.name + '/admin.sock')
        conn.request('POST',path)
        response=conn.getresponse();status=response.status;data=json.load(response);conn.close()
        return status,data
    def propose(self):
        status,raw,_=self.request('/search/requests',{'query':'Python pathlib official documentation'},headers={'X-Secret':'synthetic'})
        self.assertEqual(status,202)
        return json.loads(raw)['id']
    def test_search_requires_separate_approval_and_does_not_forward_headers(self):
        rid=self.propose()
        self.assertEqual(Upstream.requests,[])
        self.assertEqual(json.loads(self.request('/search/requests/'+rid)[1])['status'],'pending')
        self.assertEqual(self.admin_request('/approve/'+rid)[1]['status'],'done')
        self.assertEqual(Upstream.requests[-1][0],'/search?q=Python+pathlib+official+documentation&format=json')
        self.assertNotIn('X-Secret',Upstream.requests[-1][1])
        self.assertEqual(json.loads(self.request('/search/requests/'+rid)[1])['results'][0]['title'],'Synthetic')
        self.assertEqual(self.admin_request('/approve/'+rid)[0],409)
        self.assertEqual(len(Upstream.requests),1)  # no page fetch or replay
    def test_network_caller_cannot_approve_or_use_old_route(self):
        rid=self.propose()
        for path in ['/approve/'+rid,'/deny/'+rid,'/pending','/search/python','/search/requests/'+rid+'/approve']:
            self.assertEqual(self.request(path,{},method='POST')[0],403)
            self.assertEqual(self.request(path)[0],403)
        self.assertEqual(Upstream.requests,[])
    def test_concurrent_approvals_send_once(self):
        from concurrent.futures import ThreadPoolExecutor
        rid=self.propose()
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses=list(pool.map(lambda _: self.admin_request('/approve/'+rid)[0],range(4)))
        self.assertEqual(sorted(statuses),[200,409,409,409])
        self.assertEqual(len(Upstream.requests),1)
    def test_deny_and_expiry_never_send(self):
        rid=self.propose()
        self.assertEqual(self.admin_request('/deny/'+rid)[1]['status'],'denied')
        self.assertEqual(self.admin_request('/approve/'+rid)[0],409)
        rid=self.propose()
        gateway.SEARCHES[rid]['created']=time.monotonic()-601
        self.assertEqual(self.admin_request('/approve/'+rid)[0],404)
        self.assertEqual(self.request('/search/requests/'+rid)[0],404)
        self.assertEqual(Upstream.requests,[])
    def test_query_cannot_be_changed_and_search_can_be_disabled(self):
        rid=self.propose()
        self.assertEqual(self.request('/search/requests/'+rid,{'query':'changed'})[0],403)
        self.assertEqual(self.request('/search/requests',{'query':'test','url':'https://example.com'})[0],400)
        self.assertEqual(self.request('/search/requests',{'query':'test\nsecret'})[0],400)
        self.assertEqual(self.admin_request('/approve/'+rid+'?query=changed')[0],403)
        del gateway.CONFIG['search']
        self.assertEqual(self.request('/search/requests',{'query':'test'})[0],403)
        self.assertEqual(Upstream.requests,[])
    def test_redirect_not_followed(self):
        Upstream.redirect=True
        rid=self.propose()
        self.assertEqual(self.admin_request('/approve/'+rid)[1]['status'],'failed')
        self.assertEqual(self.admin_request('/approve/'+rid)[0],409)
        self.assertEqual(len(Upstream.requests),1)
    def test_model_urls_and_extensions_denied(self):
        samples=[{'model':'cloud','messages':[]}, {'model':'local','messages':[],'base_url':'https://example.com'},
                 {'model':'local','messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'https://example.com'}}]}]}]
        for body in samples:self.assertEqual(self.request('/llm/v1/chat/completions',body)[0],403)
        self.assertEqual(Upstream.requests,[])
    def test_embedding_route(self):
        self.assertEqual(self.request('/embed/v1/embeddings',{'model':'local','input':['synthetic'],'encoding_format':'float'})[0],200)
        self.assertEqual(Upstream.requests[-1][0],'/v1/embeddings')
    def test_private_ip_validation(self):
        base={'LLM_BASE_URL':'https://llm.internal/v1','LLM_MODEL':'local'}
        for ip in ['8.8.8.8','127.0.0.1','169.254.169.254','::1']:
            with self.assertRaises(ValueError):endpoint({**base,'LLM_IP':ip},'LLM')
        self.assertEqual(endpoint({**base,'LLM_IP':'10.0.0.2'},'LLM')['ip'],'10.0.0.2')
    def test_public_endpoint_requires_exact_https_exception(self):
        base={'LLM_BASE_URL':'https://models.example.org/v1','LLM_MODEL':'local',
              'LLM_IP':'8.8.8.8','LLM_TRUSTED_PUBLIC_IP':'8.8.8.8'}
        self.assertEqual(endpoint(base,'LLM')['ip'],'8.8.8.8')
        for patch in [{'LLM_TRUSTED_PUBLIC_IP':''}, {'LLM_TRUSTED_PUBLIC_IP':'1.1.1.1'},
                      {'LLM_BASE_URL':'http://models.example.org/v1'},
                      {'LLM_BASE_URL':'https://models.example.org:8443/v1'},
                      {'LLM_IP':'169.254.169.254','LLM_TRUSTED_PUBLIC_IP':'169.254.169.254'},
                      {'LLM_IP':'127.0.0.1','LLM_TRUSTED_PUBLIC_IP':'127.0.0.1'}]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):endpoint({**base,**patch},'LLM')


if __name__=='__main__':unittest.main()

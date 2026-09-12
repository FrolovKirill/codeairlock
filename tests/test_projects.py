import contextlib
import http.client
import json
import os
import socket
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import configure
import projects
import project_manager as manager


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.root = pathlib.Path(self.temp.name).resolve() / 'deployment'
        self.root.mkdir()
        self.runtime = self.root / 'runtime'
        self.a = pathlib.Path(self.temp.name).resolve() / 'project-a'; self.a.mkdir()
        self.b = pathlib.Path(self.temp.name).resolve() / 'project-b'; self.b.mkdir()
        self.stack = contextlib.ExitStack()
        for module in (projects, configure):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
            self.stack.enter_context(patch.object(module, 'RUNTIME', self.runtime))
        (self.root / '.env').write_text(f'REPO_PATH={self.a}\nREPO_ACCESS=read-only\n')

    def tearDown(self):
        self.stack.close(); self.temp.cleanup()

    def test_legacy_import_once_and_separate_home_volumes(self):
        first = projects.list_projects()
        old = first['projects'][0]
        self.assertEqual(projects.home_volume(old), 'home-private')
        new = projects.add_project('Second', str(self.b), 'read-write')
        self.assertNotEqual(projects.home_volume(old), projects.home_volume(new))
        (self.root / '.env').write_text(f'REPO_PATH={self.b}\n')
        self.assertEqual(projects.list_projects()['projects'][0], old)
        projects.select_project(new['id'])
        self.assertEqual(projects.get_project()['path'], str(self.b))
        projects.select_project(old['id'])
        self.assertEqual(projects.get_project()['path'], str(self.a))
        self.assertEqual((self.runtime / 'projects.json').stat().st_mode & 0o777, 0o600)

    def test_duplicate_alias_and_deployment_ancestor_rejected(self):
        projects.list_projects()
        alias = pathlib.Path(self.temp.name) / 'alias'; alias.symlink_to(self.a, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'already registered'):
            projects.add_project('Alias', str(alias))
        for path in (self.root, self.root.parent):
            with self.assertRaises(ValueError): projects.add_project('Unsafe', str(path))
        with self.assertRaises(ValueError): projects.add_project('Bad mode', str(self.b), 'yes')

    def test_removed_or_redirected_folder_cannot_open(self):
        first = projects.list_projects()['projects'][0]
        self.a.rmdir()
        with self.assertRaises(ValueError): projects.get_project(first['id'])
        self.a.symlink_to(self.b, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'path changed'):
            projects.get_project(first['id'])

    def test_control_sockets_fifos_and_runtime_roots_are_rejected(self):
        projects.list_projects()
        for unsafe in ('/dev', '/var/run', '/var'):
            with self.assertRaises(ValueError): projects.validate_path(unsafe)
        fifo = self.b / 'pipe'
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, 'socket, FIFO or device'):
            projects.add_project('Unsafe', str(self.b))
        fifo.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.b / 'control.sock'))
            with self.assertRaisesRegex(ValueError, 'socket, FIFO or device'):
                projects.add_project('Unsafe', str(self.b))
        finally:
            sock.close()

    def test_missing_legacy_folder_does_not_block_new_projects(self):
        (self.root / '.env').write_text('REPO_PATH=/nonexistent-synthetic-path\n')
        self.assertEqual(projects.list_projects()['projects'], [])
        new = projects.add_project('New', str(self.b))
        projects.select_project(new['id'])
        self.assertEqual(projects.get_project()['id'], new['id'])

    def test_generated_mounts_permissions_passwords_and_homes_follow_project(self):
        first = projects.list_projects()['projects'][0]
        second = projects.add_project('Second', str(self.b), 'read-write')
        (self.root / 'config').mkdir(); (self.root / 'config/lsp.json').write_text('{}')
        settings = {'LLM_BASE_URL':'http://synthetic:9000/v1','LLM_IP':'10.0.0.1','LLM_MODEL':'synthetic',
                    'EMBED_BASE_URL':'http://synthetic:9000/v1','EMBED_IP':'10.0.0.1','EMBED_MODEL':'synthetic'}
        outputs = []
        def fake_docker(*args, **kwargs):
            (self.runtime / 'workstation/vnc.pass').write_bytes(b'fake')
        for project in (first, second, first):
            with patch.object(configure, 'envfile', return_value=settings), patch.object(configure.subprocess, 'run', side_effect=fake_docker):
                configure.generate(project=project)
            compose = json.loads((self.runtime / 'compose.json').read_text())
            workstation = compose['services']['workstation']
            self.assertEqual(workstation['volumes'][0]['source'], project['path'])
            self.assertEqual(workstation['volumes'][0]['read_only'], project['access']=='read-only')
            self.assertIn(projects.home_volume(project)+':/home/node', workstation['volumes'])
            self.assertEqual(set(compose['volumes']), {projects.home_volume(project)})
            self.assertNotIn('projects.json', json.dumps(compose))
            outputs.append((self.runtime / 'UI_PASSWORD.txt').read_text())
        self.assertEqual(outputs[0], outputs[2])
        self.assertNotEqual(outputs[0], outputs[1])


class ManagerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = manager.ThreadingHTTPServer(('127.0.0.1', 0), manager.Handler)
        self.server.authority = f'127.0.0.1:{self.server.server_port}'
        self.server.token = 'synthetic-test-token'
        self.server.ui_port = 6080
        self.calls = []
        self.server.operations = SimpleNamespace(state=lambda: {'projects': [], 'active': None},
            launch=lambda *a, **kw: self.calls.append((a, kw)), cancel=lambda: False, busy=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def request(self, path, method='GET', body=None, **headers):
        default = {'Host':self.server.authority,'Authorization':'Bearer synthetic-test-token'}
        default.update(headers)
        if body is not None: default['Content-Type']='application/json'
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request(method, path, json.dumps(body) if body is not None else None, default)
        response = conn.getresponse(); result = response.status, response.read(), dict(response.getheaders())
        conn.close(); return result

    def test_auth_host_origin_and_no_cors(self):
        self.assertEqual(self.request('/api/state')[0], 200)
        for headers in ({'Authorization':''}, {'Authorization':'Bearer wrong'}, {'Host':'evil.example'},
                        {'Origin':'https://evil.example'}, {'Origin':'http://localhost:6090'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/state', **headers)[0], 403)
                self.assertEqual(self.request('/api/stop','POST',{},**headers)[0],403)
        self.assertEqual(self.request('/api/open','OPTIONS')[0],403)
        self.assertEqual(self.calls, [])

    def test_routes_do_not_expose_files_credentials_or_traverse(self):
        for path in ('/api/state?url=http://example.com', '/../.env', '/runtime/projects.json', '/api/password?x=1'):
            self.assertEqual(self.request(path)[0], 404)
        status, body, headers = self.request('/api/state')
        self.assertNotIn(b'synthetic-test-token',body)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin',headers)

    def test_launch_is_bounded_to_project_identifier(self):
        status, _, _ = self.request('/api/open','POST',{'id':'a'*32,'access':'read-only','reindex':False})
        self.assertEqual(status,202)
        self.assertEqual(self.calls,[(('a'*32,'read-only',False),{})])
        self.assertEqual(self.request('/api/open','POST',{'id':[], 'access':'read-only'})[0],400)
        self.assertEqual(self.request('/api/open','POST',{'id':'a'*32,'reindex':'yes'})[0],400)

    def test_search_requires_authenticated_exact_decision(self):
        body = {'id': 'a'*32, 'query': 'Synthetic query', 'action': 'approve'}
        with patch.object(manager, 'search_control', return_value={'status': 'done'}) as control:
            for headers in ({'Authorization': ''}, {'Origin': 'https://evil.example'}, {'Host': 'evil.example'}):
                self.assertEqual(self.request('/api/search', 'POST', body, **headers)[0], 403)
                self.assertEqual(self.request('/api/search', **headers)[0], 403)
            for invalid in ({**body, 'all': True}, {**body, 'action': 'auto'}, {'id': 'a'*32}):
                self.assertEqual(self.request('/api/search', 'POST', invalid)[0], 400)
            control.assert_not_called()
            self.assertEqual(self.request('/api/search', 'POST', body)[0], 200)
            control.assert_called_once_with('approve', 'a'*32, 'Synthetic query')

    def test_quit_is_authenticated_and_does_not_accept_arbitrary_targets(self):
        from unittest.mock import Mock
        self.server.operations.quit_environment = Mock()
        for headers in ({'Authorization': ''}, {'Origin': 'https://evil.example'}, {'Host': 'evil.example'}):
            self.assertEqual(self.request('/api/quit', 'POST', {}, **headers)[0], 403)
        self.assertEqual(self.request('/api/quit', 'POST', {'project': 'another-stack'})[0], 400)
        self.server.operations.quit_environment.assert_not_called()


class OperationTests(unittest.TestCase):
    def test_quit_stops_all_services_and_keeps_volumes_under_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp); (root/'compose.json').write_text('{}')
            (root/'active-project.json').write_text('{}')
            with patch.object(manager, 'RUNTIME', root), patch.object(projects, 'RUNTIME', root):
                operations = manager.Operations()
                def run(args, **kwargs):
                    with self.assertRaises(BlockingIOError):
                        with projects.locked('operation', blocking=False): pass
                    self.assertNotIn('--volumes', args)
                    return subprocess.CompletedProcess(args, 0, '')
                with patch.object(manager.subprocess, 'run', side_effect=run) as process:
                    operations.quit_environment()
                    self.assertEqual(process.call_args_list[0].args[0][-2:], ['down', '--remove-orphans'])
                    self.assertEqual(process.call_args_list[1].args[0][-3:], ['ps', '--all', '--quiet'])
                self.assertTrue(operations.quitting)
                self.assertFalse((root/'active-project.json').exists())
                with self.assertRaisesRegex(RuntimeError, 'shutting down'):
                    operations.launch('a'*32, 'read-only')

    def test_quit_failure_keeps_manager_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp); (root/'compose.json').write_text('{}')
            with patch.object(manager, 'RUNTIME', root), patch.object(projects, 'RUNTIME', root):
                operations = manager.Operations()
                with patch.object(manager.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
                    with self.assertRaisesRegex(RuntimeError, 'Shutdown failed'): operations.quit_environment()
                self.assertFalse(operations.quitting)
                with projects.locked('operation', blocking=False):
                    with self.assertRaises(BlockingIOError): operations.quit_environment()
                self.assertFalse(operations.quitting)

    def test_quit_missing_compose_checks_owned_containers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with patch.object(manager, 'RUNTIME', root), patch.object(projects, 'RUNTIME', root):
                operations = manager.Operations()
                with patch.object(manager.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'remaining-id\n')) as run:
                    with self.assertRaisesRegex(RuntimeError, 'compose.json is missing'):
                        operations.quit_environment()
                    self.assertIn('label=com.docker.compose.project='+manager.CLI.name, run.call_args.args[0])
                self.assertFalse(operations.quitting)
                with patch.object(manager.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')):
                    operations.quit_environment()
                self.assertTrue(operations.quitting)

    def test_quit_cancels_and_joins_own_operation_before_down(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp); (root/'compose.json').write_text('{}')
            with patch.object(manager, 'RUNTIME', root), patch.object(projects, 'RUNTIME', root):
                from unittest.mock import Mock
                operations = manager.Operations(); operations.busy = True
                operations.process = SimpleNamespace(pid=12345, poll=lambda: None)
                operations.thread = Mock(); operations.thread.is_alive.side_effect = [True, False]
                with patch.object(manager.os, 'killpg') as kill, \
                     patch.object(manager.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')):
                    operations.quit_environment()
                kill.assert_called_once_with(12345, manager.signal.SIGINT)
                operations.thread.join.assert_called_once_with(timeout=120)

    def test_open_does_not_start_independent_indexing(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(manager, 'RUNTIME', pathlib.Path(temp)):
            operations = manager.Operations()
            with patch.object(operations, '_command', return_value=True) as command:
                operations._work('a'*32, False, False)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0], ['up', '--project', 'a'*32])

    def test_cancel_does_not_issue_a_second_unlocked_down(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(manager, 'RUNTIME', pathlib.Path(temp)):
            operations = manager.Operations(); operations.cancelled.set()
            with patch.object(operations, '_command', return_value=False), patch.object(manager.subprocess, 'run') as run:
                operations._work('a'*32, False, False)
            run.assert_not_called()
            self.assertEqual(operations.phase, 'Stopped')

    def test_status_failure_is_not_reported_as_stopped(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(manager, 'RUNTIME', pathlib.Path(temp)):
            (pathlib.Path(temp)/'compose.json').write_text('{}')
            operation = manager.Operations()
            for failure in (subprocess.CompletedProcess([], 1, ''), subprocess.TimeoutExpired(['docker'], 5)):
                kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with patch.object(manager.subprocess, 'run', **kwargs), self.assertRaises(RuntimeError):
                    operation.running_project()
            with patch.object(manager.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')):
                self.assertIsNone(operation.running_project())

    def test_running_identity_comes_from_container_label(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(manager, 'RUNTIME', pathlib.Path(temp)):
            (pathlib.Path(temp)/'compose.json').write_text('{}')
            # Deliberately contradictory stale marker must never be used.
            (pathlib.Path(temp)/'active-project.json').write_text(json.dumps({'id':'a'*32}))
            operations = manager.Operations()
            responses = [subprocess.CompletedProcess([],0,'synthetic-container\n'),
                         subprocess.CompletedProcess([],0,'true '+'b'*32+'\n')]
            with patch.object(manager.subprocess,'run',side_effect=responses):
                self.assertEqual(operations.running_project(),'b'*32)

    def test_password_identity_and_read_share_operation_lock(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as temp:
            runtime = pathlib.Path(temp)
            project_id = 'a'*32
            (runtime/'project-passwords').mkdir()
            (runtime/'project-passwords'/(project_id+'.txt')).write_text('testpass')
            operations = manager.Operations()
            original_read = pathlib.Path.read_text
            def assert_lock_and_read(path, *args, **kwargs):
                with self.assertRaises(BlockingIOError):
                    with projects.locked('operation', blocking=False): pass
                return original_read(path, *args, **kwargs)
            with patch.object(manager,'RUNTIME',runtime), patch.object(projects,'RUNTIME',runtime), \
                 patch.object(manager,'list_projects',return_value={'projects':[{'id':project_id}]}), \
                 patch.object(operations,'running_project',return_value=project_id):
                with patch.object(pathlib.Path,'read_text',assert_lock_and_read):
                    self.assertEqual(operations.password(project_id),{'id':project_id,'password':'testpass'})
                with self.assertRaises(ValueError): operations.password('b'*32)
                with projects.locked('operation',blocking=False): pass

    def test_manager_and_desktop_ports_must_differ(self):
        with patch.object(sys,'argv',['manager','--port','6090']), patch.object(manager,'envfile',return_value={'UI_PORT':'6090'}):
            with self.assertRaisesRegex(SystemExit,'different'):
                manager.main()

    def test_busy_rejects_parallel_switch_and_stop_interrupts_child_group(self):
        operations = manager.Operations(); operations.busy=True
        with self.assertRaises(RuntimeError): operations.launch('a'*32,'read-only')
        operations.process = SimpleNamespace(pid=12345,poll=lambda:None)
        with patch.object(manager.os,'killpg') as kill:
            self.assertTrue(operations.cancel())
            kill.assert_called_once_with(12345,manager.signal.SIGINT)
        self.assertTrue(operations.cancelled.is_set())

if __name__ == '__main__': unittest.main()

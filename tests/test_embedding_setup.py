import copy
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import embedding_setup as setup
import configure
import startup
import subprocess


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name)
        self.settings = {'repository': 'a' * 64, 'expected_dimension': None,
                         'identity': {'url': 'http://synthetic/v1', 'ip': '10.0.0.10', 'model': 'first', 'revision': ''}}

    def tearDown(self):
        self.temp.cleanup()

    def test_reuse_and_model_change_even_with_same_dimension(self):
        first = setup.prepare_index(self.settings, 3, self.home)
        again = setup.prepare_index(self.settings, 3, self.home)
        self.assertTrue(first['fresh'])
        self.assertFalse(again['fresh'])
        self.assertEqual(first['directory'], again['directory'])
        marker = pathlib.Path(first['directory']) / 'preserve'
        marker.write_text('synthetic old index')
        self.settings['identity']['model'] = 'second'
        with self.assertRaisesRegex(setup.SetupError, 'up --reindex'):
            setup.prepare_index(self.settings, 3, self.home)
        changed = setup.prepare_index(self.settings, 3, self.home, reindex=True)
        self.assertNotEqual(first['directory'], changed['directory'])
        self.assertTrue(marker.exists())

    def test_dimension_endpoint_revision_changes_block(self):
        setup.prepare_index(self.settings, 3, self.home)
        for field in ('url', 'ip', 'revision'):
            modified = copy.deepcopy(self.settings)
            modified['identity'][field] += '-changed'
            with self.assertRaises(setup.SetupError):
                setup.prepare_index(modified, 3, self.home)
        with self.assertRaises(setup.SetupError):
            setup.prepare_index(self.settings, 4, self.home)

    def test_expected_dimension_is_assertion_even_with_reindex(self):
        self.settings['expected_dimension'] = 1024
        with self.assertRaisesRegex(setup.SetupError, 'endpoint returned 3'):
            setup.prepare_index(self.settings, 3, self.home, reindex=True)
        self.assertFalse((self.home / 'index-profiles').exists())

    def test_repositories_and_explicit_rebuild_are_separate(self):
        first = setup.prepare_index(self.settings, 3, self.home)
        self.settings['repository'] = 'b' * 64
        second = setup.prepare_index(self.settings, 3, self.home)
        third = setup.prepare_index(self.settings, 3, self.home, reindex=True)
        self.assertEqual(len({x['directory'] for x in (first, second, third)}), 3)

    def test_corrupt_manifest_requires_explicit_rebuild(self):
        setup.prepare_index(self.settings, 3, self.home)
        manifest = next((self.home / 'index-profiles').glob('*.json'))
        manifest.write_text('{bad')
        with self.assertRaises(setup.SetupError):
            setup.prepare_index(self.settings, 3, self.home)
        self.assertTrue(setup.prepare_index(self.settings, 3, self.home, reindex=True)['fresh'])

    def test_probe_uses_native_dimension_and_rejects_invalid_vectors_and_redirects(self):
        responses = [{'data': [{'embedding': [0.2, 0.3, 0.4]}]}]
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                response = responses[0]
                self.send_response(302 if response == 'redirect' else 200)
                self.send_header('Location', '/do-not-follow')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(setup, 'GATEWAY', f'http://127.0.0.1:{server.server_port}/embeddings'):
                self.assertEqual(setup.detect_dimension('first'), 3)
                self.assertNotIn('dimensions', requests[-1])
                self.assertEqual(requests[-1]['input'], ['Embedding dimension check.'])
                for value in ([], [True], ['1'], [float('nan')], [float('inf')]):
                    responses[0] = {'data': [{'embedding': value}]}
                    with self.assertRaises(setup.SetupError): setup.detect_dimension('first')
                responses[0] = 'redirect'
                count = len(requests)
                with self.assertRaises(setup.SetupError): setup.detect_dimension('first')
                self.assertEqual(len(requests), count + 1)
        finally:
            server.shutdown(); server.server_close()


class ConfigurationTests(unittest.TestCase):
    def test_access_modes_and_repository_free_bootstrap(self):
        for access in ('read-only', 'read-write'):
            with self.subTest(access=access), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                (root / 'demo-repo').mkdir()
                (root / 'config').mkdir()
                (root / 'config/lsp.json').write_text('{}')
                def fake_docker(*args, **kwargs):
                    (root / 'runtime/workstation/vnc.pass').write_bytes(b'fake')
                with patch.object(configure, 'ROOT', root), patch.object(configure, 'RUNTIME', root / 'runtime'), \
                     patch.object(configure, 'envfile', return_value={'REPO_ACCESS': access}), \
                     patch.object(configure.subprocess, 'run', side_effect=fake_docker):
                    configure.generate(demo=True)
                cfg = json.loads((root / 'runtime/workstation/kilo.json').read_text())
                compose = json.loads((root / 'runtime/compose.json').read_text())
                services = compose['services']
                self.assertFalse(cfg['indexing']['enabled'])
                self.assertNotIn('dimension', cfg['indexing'])
                self.assertEqual(cfg['permission']['edit'], 'deny' if access == 'read-only' else 'ask')
                mount = services['workstation']['volumes'][0]
                self.assertEqual(mount['read_only'], access == 'read-only')
                helper = services['embedding-setup']
                self.assertEqual(helper['network_mode'], 'service:workstation-net')
                self.assertEqual(helper['profiles'], ['bootstrap'])
                self.assertNotIn('/workspace', json.dumps(helper))
                self.assertNotIn(':/config:', json.dumps(helper))
                self.assertIn('/embedding-setup.json:ro', json.dumps(helper))
                self.assertEqual(services['workstation']['cap_drop'], ['ALL'])
                self.assertTrue(services['workstation']['read_only'])
                self.assertNotIn('KILO_CONFIG_CONTENT', services['workstation'].get('environment', {}))

    def test_invalid_access_fails_before_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root/'demo-repo').mkdir()
            with patch.object(configure, 'ROOT', root), \
                 patch.object(configure, 'RUNTIME', root/'runtime'), \
                 patch.object(configure, 'envfile', return_value={'REPO_ACCESS': 'yes'}), \
                 patch.object(configure.subprocess, 'run') as docker:
                with self.assertRaisesRegex(ValueError, 'REPO_ACCESS'):
                    configure.generate(demo=True)
                docker.assert_not_called()


class StartupTests(unittest.TestCase):
    def test_success_enables_indexing_only_after_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = pathlib.Path(temp)
            (runtime / 'workstation').mkdir()
            (runtime / 'workstation/kilo.json').write_text(json.dumps({'indexing': {'enabled': False, 'lancedb': {}}}))
            (runtime / 'compose.json').write_text(json.dumps({'services': {'workstation': {}}}))
            def fake_run(args, **kwargs):
                if 'run' in args:
                    before = json.loads((runtime / 'workstation/kilo.json').read_text())
                    self.assertFalse(before['indexing']['enabled'])
                    return subprocess.CompletedProcess(args, 0, json.dumps({'dimension': 7, 'directory': '/home/node/index/' + 'a' * 32, 'fresh': True}))
                return subprocess.CompletedProcess(args, 0)
            with patch.object(startup, 'RUNTIME', runtime), patch.object(startup, 'generate'), \
                 patch.object(startup.subprocess, 'run', side_effect=fake_run):
                startup.start()
            cfg = json.loads((runtime / 'workstation/kilo.json').read_text())
            deployment = json.loads((runtime / 'compose.json').read_text())
            self.assertEqual(cfg['indexing']['dimension'], 7)
            self.assertTrue(cfg['indexing']['enabled'])
            override = json.loads(deployment['services']['workstation']['environment']['KILO_CONFIG_CONTENT'])
            self.assertTrue(override['indexing']['enabled'])

    def test_failed_probe_and_interrupt_clean_up_without_starting_workstation(self):
        for failure in (subprocess.CalledProcessError(1, ['synthetic']), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temp:
                calls = []
                def fake_run(args, **kwargs):
                    calls.append(args)
                    if 'run' in args:
                        raise failure
                    return subprocess.CompletedProcess(args, 0)
                with patch.object(startup, 'RUNTIME', pathlib.Path(temp)), \
                     patch.object(startup, 'generate'), \
                     patch.object(startup.subprocess, 'run', side_effect=fake_run):
                    with self.assertRaises(SystemExit):
                        startup.start()
                self.assertIn('down', calls[-1])
                self.assertEqual(sum('up' in call for call in calls), 1)
                self.assertNotIn('workstation', calls[0])

class CleanupFailureTests(unittest.TestCase):
    def test_failed_cleanup_has_fixed_message_and_keeps_original_stage(self):
        import diagnostics
        with tempfile.TemporaryDirectory() as temp:
            runtime = pathlib.Path(temp)
            report = diagnostics.Report(runtime=runtime)
            def fail(args, **kwargs):
                raise subprocess.CalledProcessError(1, ['PRIVATE_COMMAND'])
            with patch.object(startup, 'RUNTIME', runtime), patch.object(startup, 'generate'), \
                 patch.object(startup.subprocess, 'run', side_effect=fail), \
                 patch.object(report, 'services'), \
                 self.assertRaisesRegex(SystemExit, 'Startup failed and cleanup could not finish'), \
                 report.run():
                startup.start(report=report)
            phases = {x['stage']: x['status'] for x in report.data['stages']}
            self.assertEqual(phases['bootstrap'], 'failed')
            self.assertEqual(phases['cleanup'], 'failed')
            self.assertNotIn('PRIVATE_COMMAND', (runtime/diagnostics.REPORT_NAME).read_text())

    def test_snapshot_failure_does_not_skip_cleanup(self):
        import diagnostics
        with tempfile.TemporaryDirectory() as temp:
            report = diagnostics.Report(runtime=temp)
            def run(args, **kwargs):
                if 'up' in args:
                    raise subprocess.CalledProcessError(1, ['synthetic'])
                return subprocess.CompletedProcess(args, 0)
            with patch.object(startup, 'RUNTIME', pathlib.Path(temp)), \
                 patch.object(startup, 'generate'), \
                 patch.object(startup.subprocess, 'run', side_effect=run) as docker, \
                 patch.object(report, 'services', side_effect=OSError('synthetic')), \
                 self.assertRaisesRegex(SystemExit, 'Startup stopped'):
                startup.start(report=report)
            self.assertIn('down', docker.call_args.args[0])

if __name__ == '__main__':
    unittest.main()

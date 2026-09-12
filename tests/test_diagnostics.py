import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import diagnostics

SECRET = 'PRIVATE_CODE_AND_API_KEY_DO_NOT_EXPORT'


class DiagnosticTests(unittest.TestCase):
    def test_exceptions_commands_and_output_never_enter_report(self):
        errors = [ValueError(SECRET), FileNotFoundError(SECRET), PermissionError(SECRET),
                  subprocess.CalledProcessError(1, [SECRET], output=SECRET, stderr=SECRET),
                  subprocess.TimeoutExpired([SECRET], 5, output=SECRET, stderr=SECRET),
                  KeyboardInterrupt(SECRET)]
        for error in errors:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temp:
                report = diagnostics.Report(runtime=temp)
                with self.assertRaises(type(error)), report.run(), report.stage('embedding_probe'):
                    raise error
                path = pathlib.Path(temp)/diagnostics.REPORT_NAME
                text = path.read_text()
                self.assertNotIn(SECRET, text)
                data = json.loads(text)
                self.assertIn(data['status'], ['failed', 'cancelled'])
                self.assertEqual(data['stages'][0]['error_code'], diagnostics.error_code(error))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_configuration_error_has_allowlisted_code_without_values(self):
        known = diagnostics.ConfigurationError('ollama_limits_exceeded', SECRET)
        unknown = diagnostics.ConfigurationError(SECRET, SECRET)
        self.assertEqual(diagnostics.error_code(known), 'ollama_limits_exceeded')
        self.assertEqual(diagnostics.error_code(unknown), 'invalid_configuration')

    def test_export_rebuilds_only_valid_schema_not_raw_strings(self):
        with tempfile.TemporaryDirectory() as temp:
            data = {'status': 'passed', 'mode': SECRET, 'started_at': SECRET,
                    'error': SECRET, 'path': SECRET, 'stdout': SECRET, 'elapsed_seconds': SECRET,
                    'services': {SECRET: {'state': SECRET}, 'gateway': {'state': SECRET, 'health': 'healthy', 'env': SECRET}},
                    'stages': [{'stage': SECRET}, {'stage': 'indexing', 'status': 'failed', 'error_code': SECRET, 'stderr': SECRET}]}
            path = pathlib.Path(temp)/diagnostics.REPORT_NAME
            path.write_text(json.dumps(data))
            out = io.StringIO()
            with contextlib.redirect_stdout(out): diagnostics.export(temp)
            self.assertNotIn(SECRET, out.getvalue())
            result = json.loads(out.getvalue())
            self.assertEqual(result['services'], {'gateway': {'health': 'healthy'}})
            self.assertEqual(result['stages'], [{'stage': 'indexing', 'status': 'failed'}])

    def test_running_phase_is_saved_before_work_and_latest_report_replaces_previous(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp)/diagnostics.REPORT_NAME
            report = diagnostics.Report(demo=True, runtime=temp)
            with report.run(), report.stage('bootstrap'):
                stored = json.loads(path.read_text())
                self.assertEqual(stored['status'], 'running')
                self.assertEqual(stored['stages'][-1]['status'], 'running')
            with diagnostics.Report(runtime=temp).run(): pass
            stored = json.loads(path.read_text())
            self.assertEqual(stored['stages'], [])
            self.assertEqual(stored['mode'], 'connected')

    def test_service_snapshot_uses_only_fixed_fields_and_rejects_unexpected_output(self):
        with tempfile.TemporaryDirectory() as temp:
            report = diagnostics.Report(runtime=temp)
            def run(args, **kwargs):
                self.assertNotIn('logs', args)
                self.assertNotIn('.Config', ' '.join(args))
                if 'ps' in args: return subprocess.CompletedProcess(args, 0, 'a'*64)
                return subprocess.CompletedProcess(args, 0, SECRET+' '+SECRET)
            with patch.object(diagnostics.subprocess, 'run', side_effect=run): report.services()
            self.assertNotIn(SECRET, (pathlib.Path(temp)/diagnostics.REPORT_NAME).read_text())
            self.assertTrue(all(s == {'state': 'unknown', 'health': 'unknown'} for s in report.data['services'].values()))

    def test_malformed_oversized_nonfinite_exports_fail_without_content(self):
        for raw in [SECRET, SECRET*65536, '{"status":[]}',
                    '{"status":"passed","elapsed_seconds":NaN,"stages":[42,null]}']:
            with tempfile.TemporaryDirectory() as temp:
                (pathlib.Path(temp)/diagnostics.REPORT_NAME).write_text(raw)
                out = io.StringIO()
                with contextlib.redirect_stdout(out): diagnostics.export(temp)
                self.assertNotIn(SECRET, out.getvalue())
                self.assertNotIn('NaN', out.getvalue())

    def test_disabled_services_are_absent_without_docker_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            report = diagnostics.Report(runtime=temp)
            with patch.object(diagnostics.subprocess, 'run') as run: report.services([])
            run.assert_not_called()
            self.assertTrue(all(row == {'state': 'absent', 'health': 'none'} for row in report.data['services'].values()))

    def test_failed_container_snapshot_decodes_component_without_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            report = diagnostics.Report(runtime=temp)
            def run(args, **kwargs):
                self.assertNotIn('logs', args)
                output = 'a'*64 if 'ps' in args else 'exited unhealthy 24 false'
                return subprocess.CompletedProcess(args, 0, output)
            with patch.object(diagnostics.subprocess, 'run', side_effect=run):
                report.services(['workstation'])
            row = json.loads((pathlib.Path(temp)/diagnostics.REPORT_NAME).read_text())['services']['workstation']
            self.assertEqual(row['failure'], 'vnc_exited')
            self.assertEqual(row['exit_code'], 24)
            self.assertFalse(row['oom_killed'])

    def test_forged_failure_text_and_invalid_exit_fields_are_discarded(self):
        result = diagnostics.sanitize({'services': {'workstation': {
            'state': 'exited', 'exit_code': True, 'oom_killed': SECRET, 'failure': SECRET}}})
        self.assertEqual(result['services']['workstation'], {'state': 'exited'})

    def test_snapshot_has_shared_timeout_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            report = diagnostics.Report(runtime=temp)
            now = [0.0]
            def timeout(args, **kwargs):
                now[0] += kwargs['timeout']
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            with patch.object(diagnostics.time, 'monotonic', side_effect=lambda: now[0]), \
                 patch.object(diagnostics.subprocess, 'run', side_effect=timeout) as run:
                report.services()
            self.assertEqual(now[0], 5.0)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(len(report.data['services']), len(diagnostics.SERVICES))

    def test_unwritable_report_does_not_prevent_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            report = diagnostics.Report(runtime=temp)
            out = io.StringIO()
            with patch.object(diagnostics, 'write', side_effect=PermissionError(SECRET)), \
                 contextlib.redirect_stderr(out), report.run(), report.stage('cleanup'):
                completed = True
            self.assertTrue(completed)
            self.assertEqual(report.data['stages'][0]['status'], 'passed')
            self.assertEqual(out.getvalue(), 'Startup diagnostics could not be saved.\n')
            self.assertNotIn(SECRET, out.getvalue())

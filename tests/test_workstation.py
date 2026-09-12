import importlib.util
import pathlib
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('workstation', pathlib.Path(__file__).resolve().parents[1]/'deploy/workstation.py')
workstation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workstation)

class WorkstationTests(unittest.TestCase):
    def test_delayed_display_waits_for_readiness(self):
        process = Mock(); process.poll.return_value = None
        probe = Mock(side_effect=[False, False, True])
        with patch.object(workstation.time, 'sleep') as sleep:
            workstation.wait_ready('display', probe, {'display': process})
        self.assertEqual(sleep.call_count, 2)

    def test_exited_child_fails_even_when_probe_returns_true(self):
        for index, name in enumerate(workstation.COMPONENTS):
            process = Mock(); process.poll.return_value = 1
            with self.assertRaises(workstation.ServiceFailure) as error:
                workstation.wait_ready(name, lambda: True, {name: process})
            self.assertEqual(error.exception.code, 21+index)

    def test_readiness_timeout_has_distinct_component_code(self):
        process = Mock(); process.poll.return_value = None
        with patch.object(workstation.time, 'monotonic', side_effect=[0, 21]), \
             self.assertRaises(workstation.ServiceFailure) as error:
            workstation.wait_ready('editor', lambda: False, {'editor': process})
        self.assertEqual(error.exception.code, 33)

    def test_startup_clears_old_ready_marker_before_setup(self):
        marker = Mock()
        def begin_setup():
            marker.unlink.assert_called_once_with(missing_ok=True)
            raise workstation.ServiceFailure('display', timeout=True)
        with patch.object(workstation.pathlib, 'Path') as path:
            path.return_value = marker
            path.home.side_effect = begin_setup
            self.assertEqual(workstation.main(), 31)
        self.assertEqual(marker.unlink.call_count, 2)


class SettingsTests(unittest.TestCase):
    def test_preserves_preferences_and_refreshes_managed_model(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'settings.json'
            path.write_text('''{
                // An ordinary VS Code settings file.
                "editor.fontSize": 19,
                "custom.url": "https://example.test/a/*value*/",
                "custom.nested": {"array": [1, 2,],},
                "kilo-code.new.model.modelID": "old",
                "kilo-code.new.agentWorkStyle": "user-choice",
            }''')
            defaults = {'editor.fontSize': 14, 'kilo-code.new.model.modelID': 'new',
                        'kilo-code.new.agentWorkStyle': 'human-in-the-loop',
                        'telemetry.telemetryLevel': 'off'}
            workstation.configure_editor(path, defaults)
            workstation.configure_editor(path, defaults)  # Repeated restart.
            saved = json.loads(path.read_text())
            self.assertEqual(saved['editor.fontSize'], 19)
            self.assertEqual(saved['custom.url'], 'https://example.test/a/*value*/')
            self.assertEqual(saved['custom.nested'], {'array': [1, 2]})
            self.assertEqual(saved['kilo-code.new.agentWorkStyle'], 'user-choice')
            self.assertEqual(saved['kilo-code.new.model.modelID'], 'new')
            self.assertEqual(saved['telemetry.telemetryLevel'], 'off')

    def test_invalid_settings_remain_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'settings.json'
            for text in ('{"broken":', '[]'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    workstation.configure_editor(path, {})
                self.assertEqual(path.read_text(), text)

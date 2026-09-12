import importlib.util
import pathlib
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

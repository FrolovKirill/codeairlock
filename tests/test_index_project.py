import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import index_project as index


class IndexTests(unittest.TestCase):
    def test_disabled_editor_is_not_started_or_enabled(self):
        with patch.object(index, 'editor_servers', return_value=[(1234, b'synthetic')]), \
             patch.object(index, 'get', side_effect=[{'healthy': True, 'version': '7.5.16'}, {'state': 'Disabled'}]) as get, \
             patch.object(index.subprocess, 'run') as run:
            self.assertEqual(index.wait_for_index(), 1)
            self.assertEqual(get.call_args.args[2], '/indexing/status?directory=/workspace')
            run.assert_not_called()

    def test_waits_for_editor_completion(self):
        with patch.object(index, 'editor_servers', return_value=[(1234, b'synthetic')]), \
             patch.object(index, 'get', side_effect=[{'healthy': True, 'version': '7.5.16'}, {'state': 'Indexing'}, {'state': 'Complete'}]), \
             patch.object(index.time, 'sleep'):
            self.assertEqual(index.wait_for_index(), 0)

    def test_private_status_fields_are_never_exported(self):
        self.assertEqual(index.visible({'state': '/private/path', 'message': 'private', 'processedFiles': 'private', 'totalFiles': 7}), {'state': 'Unknown', 'totalFiles': 7})

    def test_discovers_only_editor_owned_listening_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = pathlib.Path(temp)
            (proc / 'net').mkdir()
            (proc / 'net/tcp').write_text('header\n0: 0100007F:1000 00000000:0000 0A 0 0 0 0 0 999\n')
            for pid, client in [('123', 'vscode'), ('124', 'cli')]:
                folder = proc / pid
                (folder / 'fd').mkdir(parents=True)
                (folder / 'environ').write_bytes(('KILO_CLIENT=' + client + '\0KILO_SERVER_PASSWORD=synthetic\0').encode())
                (folder / 'fd/7').symlink_to('socket:[999]')
            self.assertEqual(list(index.editor_servers(proc)), [(4096, b'synthetic')])

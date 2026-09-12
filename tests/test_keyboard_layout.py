import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location('keyboard_layout', pathlib.Path(__file__).resolve().parents[1]/'deploy/keyboard_layout.py')
keyboard_layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keyboard_layout)


class KeyboardLayoutTests(unittest.TestCase):
    def test_cyrillic_is_preloaded_in_same_group_with_both_cases(self):
        rows = ['key <AD04> { symbols[Group1]=[r,R]; symbols[Group2]=[Cyrillic_ka,Cyrillic_KA]; };']
        rows += ['key <T%03d> { symbols[Group1]=[q,Q]; symbols[Group2]=[Cyrillic_shorti,Cyrillic_SHORTI]; };' % i for i in range(32)]
        result = keyboard_layout.keymap('\n'.join(rows))
        self.assertEqual(result.count('type[Group1]="FOUR_LEVEL"'), 33)
        self.assertIn('symbols[Group1]=[ r, R, Cyrillic_ka, Cyrillic_KA ]', result)
        self.assertNotIn('Group2', result)
        self.assertIn('level3(ralt_switch)', result)

    def test_incomplete_system_layout_fails_before_install(self):
        with self.assertRaisesRegex(ValueError, '33 Cyrillic'):
            keyboard_layout.keymap('key <AD04> { symbols[Group1]=[r,R]; };')

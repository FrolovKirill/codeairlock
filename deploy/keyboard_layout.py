"""Preload Cyrillic on XKB levels 3/4; keep physical US keycodes for shortcuts.

x11vnc's dynamic keysym pool can exhaust after only a few Cyrillic letters.
Separate XKB groups are also unreliable with its modifier synthesis. This map
uses one group with US lower/upper and Cyrillic lower/upper on four levels.
Only installed system layout definitions are read; no repository data is used.
"""
import re
import subprocess


def keymap(compiled):
    keys = []
    for name, body in re.findall(r'key\s+<([^>]+)>\s*\{(.*?)\};', compiled, re.S):
        latin = re.search(r'symbols\[Group1\]\s*=\s*\[([^]]+)\]', body)
        cyrillic = re.search(r'symbols\[Group2\]\s*=\s*\[([^]]+)\]', body)
        if not latin or not cyrillic or 'Cyrillic_' not in cyrillic[1]:
            continue
        symbols = [x.strip() for x in latin[1].split(',')[:2]] + [x.strip() for x in cyrillic[1].split(',')[:2]]
        if len(symbols) != 4:
            raise ValueError('Unexpected system keyboard layout')
        keys.append('key <'+name+'> { type[Group1]="FOUR_LEVEL", symbols[Group1]=[ '+', '.join(symbols)+' ] };')
    if len(keys) != 33:
        raise ValueError('Expected all 33 Cyrillic letters in the system layout')
    return ('xkb_keymap {\n'
            'xkb_keycodes { include "evdev+aliases(qwerty)" };\n'
            'xkb_types { include "complete" };\n'
            'xkb_compatibility { include "complete" };\n'
            'xkb_symbols { include "pc+us+inet(evdev)+level3(ralt_switch)"\n'
            + '\n'.join(keys) + '\n};\n};\n')


def configure():
    options = {'stderr': subprocess.PIPE, 'timeout': 5}
    source = subprocess.check_output(['setxkbmap', '-layout', 'us,ru', '-option', '', '-print'], **options)
    compiled = subprocess.check_output(['xkbcomp', '-xkb', '-', '-'], input=source, **options).decode()
    subprocess.run(['xkbcomp', '-w', '0', '-', ':0'], input=keymap(compiled).encode(), check=True, **options)


if __name__ == '__main__':
    configure()

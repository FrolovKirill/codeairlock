import json
import os
import pathlib
import subprocess
import time

home = pathlib.Path.home()
for p in ['.config', '.local/share', '.cache', '.mozilla/firefox/private', 'editor/User']:
    (home / p).mkdir(parents=True, exist_ok=True)
settings = json.loads(pathlib.Path('/config/editor.json').read_text())
(home / 'editor/User/settings.json').write_text(json.dumps(settings))
profile = home / '.mozilla/firefox/private'
(profile / 'user.js').write_text('''
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.startup.page", 0);
user_pref("browser.tabs.warnOnClose", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("network.trr.mode", 5);
user_pref("network.prefetch-next", false);
user_pref("network.dns.disablePrefetch", true);
user_pref("browser.safebrowsing.downloads.enabled", false);
user_pref("browser.safebrowsing.malware.enabled", false);
user_pref("browser.safebrowsing.phishing.enabled", false);
user_pref("media.peerconnection.enabled", false);
''')
children = []
def launch(args):
    p = subprocess.Popen(args)
    children.append(p)
    return p
launch(['Xvfb', ':0', '-screen', '0', '1600x1000x24', '-nolisten', 'tcp'])
time.sleep(1)
launch(['openbox'])
launch(['/opt/code-server/bin/code-server', '--bind-addr', '127.0.0.1:8080', '--auth', 'none', '--disable-telemetry', '--disable-update-check', '--disable-workspace-trust', '--user-data-dir', str(home / 'editor'), '--extensions-dir', '/opt/extensions', '/workspace'])
launch(['x11vnc', '-display', ':0', '-forever', '-shared', '-rfbport', '5900', '-rfbauth', '/config/vnc.pass', '-noxdamage', '-nosel', '-noclipboard'])
time.sleep(4)
# Chromium's nested setuid/user-namespace sandbox cannot run with this container's
# no-new-privileges + dropped capabilities. The outer container/network namespace
# is the security boundary, including against arbitrary browser/agent execution.
launch(['chromium', '--no-sandbox', '--no-first-run', '--disable-background-networking',
        '--disable-sync', '--disable-default-apps', '--disable-component-update',
        '--password-store=basic', '--user-data-dir='+str(home / 'chromium'),
        '--kiosk', 'http://127.0.0.1:8080/?folder=/workspace'])
while all(p.poll() is None for p in children):
    time.sleep(2)
for p in children:
    p.terminate()
raise SystemExit('A workstation service exited; restarting requires the operator.')

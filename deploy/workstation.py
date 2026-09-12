"""Start UI components in readiness order; Docker exit codes contain no user data."""
import ctypes
import json
import pathlib
import socket
import subprocess
import time
import urllib.request

COMPONENTS = ('display', 'window_manager', 'editor', 'vnc', 'browser')
PROGRAMS = {'Xvfb': 'display', 'openbox': 'window_manager', 'code-server': 'editor',
            'x11vnc': 'vnc', 'chromium': 'browser'}

class ServiceFailure(Exception):
    def __init__(self, component, timeout=False):
        self.component = component
        self.code = (31 if timeout else 21) + COMPONENTS.index(component)

def check_children(children):
    for name, process in children.items():
        if process.poll() is not None:
            raise ServiceFailure(name)

def wait_ready(name, probe, children, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        check_children(children)
        if probe(): return
        if time.monotonic() >= deadline: raise ServiceFailure(name, timeout=True)
        time.sleep(0.1)

def display_ready():
    lib = ctypes.CDLL('libX11.so.6')
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = lib.XOpenDisplay(b':0')
    if not display: return False
    lib.XCloseDisplay(display)
    return True

def port_ready(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.2): return True
    except OSError: return False

def editor_ready():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8080/healthz', timeout=0.5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError): return False

def main():
    children = {}
    ready = pathlib.Path('/tmp/workstation-ready')
    def launch(args):
        children[PROGRAMS[pathlib.Path(args[0]).name]] = subprocess.Popen(args)
    try:
        ready.unlink(missing_ok=True)
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
        launch(['Xvfb', ':0', '-screen', '0', '1600x1000x24', '-s', '0', '-nolisten', 'tcp'])
        wait_ready('display', display_ready, children)
        launch(['openbox'])
        launch(['/opt/code-server/bin/code-server', '--bind-addr', '127.0.0.1:8080', '--auth', 'none', '--disable-telemetry', '--disable-update-check', '--disable-workspace-trust', '--user-data-dir', str(home / 'editor'), '--extensions-dir', '/opt/extensions', '/workspace'])
        launch(['x11vnc', '-display', ':0', '-forever', '-shared', '-rfbport', '5900', '-rfbauth', '/config/vnc.pass', '-noxdamage', '-nosel', '-noclipboard'])
        wait_ready('editor', editor_ready, children)
        wait_ready('vnc', lambda: port_ready(5900), children)
        # Chromium's nested setuid/user-namespace sandbox cannot run with this container's
        # no-new-privileges + dropped capabilities. The outer container/network namespace
        # is the security boundary, including against arbitrary browser/agent execution.
        # Lifecycle serialization stops the previous container before reattaching this home.
        # Chromium's hostname/PID lock otherwise points at that dead container after restart.
        for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
            (home / 'chromium' / name).unlink(missing_ok=True)
        launch(['chromium', '--no-sandbox', '--no-first-run', '--disable-background-networking',
                '--disable-sync', '--disable-default-apps', '--disable-component-update',
                '--password-store=basic', '--user-data-dir='+str(home / 'chromium'),
                '--kiosk', 'http://127.0.0.1:8080/?folder=/workspace'])
        # Only advertise readiness after the display, editor and VNC are listening.
        check_children(children)
        ready.touch()
        while True:
            check_children(children)
            time.sleep(0.2)
    except ServiceFailure as error:
        print('Workstation component failed: '+error.component, flush=True)
        return error.code
    except Exception:
        print('Workstation setup failed; private exception text withheld.', flush=True)
        return 40
    finally:
        ready.unlink(missing_ok=True)
        for process in children.values():
            if process.poll() is None: process.terminate()
        for process in children.values():
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill()

if __name__ == '__main__':
    raise SystemExit(main())

"""Host-only startup metadata. Never ingest logs, prompts, repository files or env."""
import contextlib
import datetime
import json
import math
import pathlib
import subprocess
import sys
import time

from configure import RUNTIME, write, ConfigurationError

STAGES = {'project_selection', 'previous_environment_stop', 'configuration', 'bootstrap',
          'embedding_probe', 'index_configuration', 'workspace_start', 'service_status',
          'network_audit', 'indexing', 'project_registration', 'cleanup'}
STATES = {'running', 'passed', 'failed', 'cancelled'}
ERRORS = {'command_failed', 'timeout', 'invalid_configuration', 'permission_denied',
          'missing_file', 'interrupted', 'internal_error', 'ollama_limits_exceeded', 'invalid_model_limits'}
SERVICES = {'workstation', 'workstation-net', 'gateway', 'gateway-net', 'ui', 'ui-net',
            'searxng', 'search-net', 'search-proxy', 'mock'}
CONTAINER_STATES = {'created', 'running', 'paused', 'restarting', 'removing', 'exited',
                    'dead', 'absent', 'unknown'}
HEALTH = {'healthy', 'unhealthy', 'starting', 'none', 'unknown'}
REPORT_NAME = 'startup-diagnostics.json'
WORKSTATION_COMPONENTS = ('display', 'window_manager', 'editor', 'vnc', 'browser')
WORKSTATION_FAILURES = {**{21+i: name+'_exited' for i, name in enumerate(WORKSTATION_COMPONENTS)},
                        **{31+i: name+'_not_ready' for i, name in enumerate(WORKSTATION_COMPONENTS)},
                        40: 'setup_failed'}


def error_code(error):
    # Exception messages/commands/stdout/stderr may contain secrets. Never stringify.
    if isinstance(error, ConfigurationError):
        return error.code if isinstance(error.code, str) and error.code in ERRORS else 'invalid_configuration'
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return 'interrupted' if isinstance(error, KeyboardInterrupt) else 'command_failed'
    if isinstance(error, subprocess.TimeoutExpired): return 'timeout'
    if isinstance(error, subprocess.CalledProcessError): return 'command_failed'
    if isinstance(error, PermissionError): return 'permission_denied'
    if isinstance(error, FileNotFoundError): return 'missing_file'
    if isinstance(error, (ValueError, KeyError, TypeError)): return 'invalid_configuration'
    return 'internal_error'


def number(value, maximum):
    return type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)


def sanitize(data):
    """Reconstruct an allowlisted export, even if the report was edited/corrupted."""
    if not isinstance(data, dict): return {}
    result = {'schema': 1, 'scope': 'last_startup_snapshot'}
    if data.get('mode') in ('demo', 'connected'): result['mode'] = data['mode']
    if isinstance(data.get('status'), str) and data['status'] in STATES: result['status'] = data['status']
    if isinstance(data.get('started_at'), str):
        try:
            stamp = datetime.datetime.strptime(data['started_at'], '%Y-%m-%dT%H:%M:%SZ')
            result['started_at'] = stamp.strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            pass
    if number(data.get('elapsed_seconds'), 604800): result['elapsed_seconds'] = round(data['elapsed_seconds'], 2)
    result['stages'] = []
    stages = data.get('stages', [])
    for item in stages[:32] if isinstance(stages, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('stage'), str) or item['stage'] not in STAGES:
            continue
        row = {'stage': item['stage']}
        if isinstance(item.get('status'), str) and item['status'] in STATES: row['status'] = item['status']
        if number(item.get('elapsed_seconds'), 604800): row['elapsed_seconds'] = round(item['elapsed_seconds'], 2)
        if isinstance(item.get('error_code'), str) and item['error_code'] in ERRORS: row['error_code'] = item['error_code']
        result['stages'].append(row)
    result['services'] = {}
    services = data.get('services', {})
    for name, state in services.items() if isinstance(services, dict) else []:
        if name not in SERVICES or not isinstance(state, dict): continue
        row = {}
        if isinstance(state.get('state'), str) and state['state'] in CONTAINER_STATES: row['state'] = state['state']
        if isinstance(state.get('health'), str) and state['health'] in HEALTH: row['health'] = state['health']
        if type(state.get('exit_code')) is int and 0 <= state['exit_code'] <= 255: row['exit_code'] = state['exit_code']
        if type(state.get('oom_killed')) is bool: row['oom_killed'] = state['oom_killed']
        if name == 'workstation' and row.get('state') in ('exited', 'dead') and row.get('exit_code') in WORKSTATION_FAILURES:
            row['failure'] = WORKSTATION_FAILURES[row['exit_code']]
        result['services'][name] = row
    return result


class Report:
    def __init__(self, demo=False, runtime=None):
        self.runtime = pathlib.Path(runtime) if runtime is not None else RUNTIME
        self.data = {'mode': 'demo' if demo else 'connected', 'status': 'running', 'stages': [], 'services': {}}
        self.data['started_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.started = time.monotonic()

    def save(self):
        self.data['elapsed_seconds'] = time.monotonic() - self.started
        try:
            write(self.runtime / REPORT_NAME, sanitize(self.data), 0o600)
        except OSError:
            # Disk/permission failures in telemetry must not block startup or cleanup.
            if not getattr(self, '_write_warning', False):
                print('Startup diagnostics could not be saved.', file=sys.stderr)
                self._write_warning = True

    @contextlib.contextmanager
    def run(self):
        self.save()
        try:
            yield self
        except BaseException as error:
            self.data['status'] = 'cancelled' if isinstance(error, KeyboardInterrupt) else 'failed'
            raise
        else:
            self.data['status'] = 'passed'
        finally:
            self.save()

    @contextlib.contextmanager
    def stage(self, name):
        if name not in STAGES: raise ValueError('Unknown diagnostic stage')
        row = {'stage': name, 'status': 'running'}
        self.data['stages'].append(row)
        began = time.monotonic(); self.save()
        try:
            yield
        except BaseException as error:
            row['status'] = 'cancelled' if isinstance(error, KeyboardInterrupt) else 'failed'
            row['error_code'] = error_code(error)
            raise
        else:
            row['status'] = 'passed'
        finally:
            row['elapsed_seconds'] = time.monotonic() - began
            self.save()

    def services(self, configured=None):
        # Ask Docker only for fixed state/health fields, never logs or config/env.
        command = ['docker', 'compose', '-f', str(self.runtime / 'compose.json')]
        deadline = time.monotonic() + 5
        for service in sorted(SERVICES):
            if configured is not None and service not in configured:
                self.data['services'][service] = {'state': 'absent', 'health': 'none'}
                continue
            row = {'state': 'unknown', 'health': 'unknown'}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.data['services'][service] = row
                continue
            try:
                found = subprocess.run([*command, 'ps', '--all', '--quiet', service],
                                       capture_output=True, text=True, timeout=min(2, remaining))
                cid = found.stdout.strip()
                if found.returncode == 0 and not cid:
                    row = {'state': 'absent', 'health': 'none'}
                elif found.returncode == 0 and len(cid) == 64 and all(c in '0123456789abcdef' for c in cid):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.data['services'][service] = row
                        continue
                    state = subprocess.run(['docker', 'inspect', '--format',
                        '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.State.ExitCode}} {{.State.OOMKilled}}', cid],
                        capture_output=True, text=True, timeout=min(2, remaining))
                    fields = state.stdout.strip().split()
                    if state.returncode == 0 and len(fields) == 4:
                        row = {'state': fields[0] if fields[0] in CONTAINER_STATES else 'unknown',
                               'health': fields[1] if fields[1] in HEALTH else 'unknown'}
                        if fields[2].isascii() and fields[2].isdigit() and 0 <= int(fields[2]) <= 255:
                            row['exit_code'] = int(fields[2])
                        if fields[3] in ('true', 'false'): row['oom_killed'] = fields[3] == 'true'
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.data['services'][service] = row
        self.save()


def export(runtime=None):
    path = (pathlib.Path(runtime) if runtime is not None else RUNTIME) / REPORT_NAME
    try:
        with path.open('rb') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536: raise ValueError()
        report = sanitize(json.loads(raw))
        if report.get('status') not in STATES: raise ValueError()
        print(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False))
    except FileNotFoundError:
        print('No startup report yet. Open a project or run up/demo first.')
    except (OSError, ValueError, TypeError):
        print('Diagnostic report unavailable or invalid; raw content was not displayed.')


if __name__ == '__main__':
    export()

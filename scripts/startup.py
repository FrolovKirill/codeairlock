"""Start the agent only after isolated embedding detection and index compatibility checks."""
import json
import contextlib
import sys
import re
import subprocess

from configure import ROOT, RUNTIME, generate, write


def start(demo=False, reindex=False, project=None, report=None):
    stage = report.stage if report else lambda _: contextlib.nullcontext()
    command = ['docker', 'compose', '-f', str(RUNTIME / 'compose.json')]

    def compose(*args, **kwargs):
        return subprocess.run([*command, *args], cwd=ROOT, check=True, **kwargs)

    if (RUNTIME / 'compose.json').exists():
        with stage('previous_environment_stop'):
            compose('down', '--remove-orphans')
    (RUNTIME / 'active-project.json').unlink(missing_ok=True)
    with stage('configuration'):
        generate(demo=demo, project=project)
    try:
        # This phase has no repository mount and cannot run the agent.
        services = ['workstation-net', 'gateway'] + (['mock'] if demo else [])
        with stage('bootstrap'):
            compose('up', '-d', '--pull', 'never', '--wait', *services)
        with stage('embedding_probe'):
            probe = compose('run', '--rm', '--no-deps', '-T', 'embedding-setup',
                            *(['--reindex'] if reindex else []), stdout=subprocess.PIPE, text=True)
            result = json.loads(probe.stdout)
            if (type(result['dimension']) is not int or not 1 <= result['dimension'] <= 65536
                    or not re.fullmatch(r'/home/node/index/[0-9a-f]{32}', result['directory'])):
                raise ValueError('Invalid embedding setup result')
        with stage('index_configuration'):
            config_path = RUNTIME / 'workstation/kilo.json'
            cfg = json.loads(config_path.read_text())
            cfg['indexing']['dimension'] = result['dimension']
            cfg['indexing']['lancedb']['directory'] = result['directory']
            cfg['indexing']['enabled'] = True
            write(config_path, cfg)
            # Kilo's per-project defaults can otherwise disable indexing on fresh homes.
            # Add the override only after discovery/identity validation succeeds.
            deployment_path = RUNTIME / 'compose.json'
            deployment = json.loads(deployment_path.read_text())
            deployment['services']['workstation']['environment'] = {
                'KILO_CONFIG_CONTENT': json.dumps({'indexing': {'enabled': True}})}
            write(deployment_path, deployment, 0o600)
            if project and not demo:
                from projects import validate_path
                if validate_path(project['path']) != project['path']:
                    raise ValueError('Project path changed before mounting; register its new location.')
        with stage('workspace_start'):
            compose('up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '90')
        if report:
            with stage('service_status'):
                report.services(deployment['services'])
            with stage('network_audit'):
                subprocess.run([sys.executable, str(ROOT/'scripts/audit_namespaces.py')],
                               check=True, capture_output=True, timeout=60)
        if project and not demo:
            write(RUNTIME / 'active-project.json', {'id': project['id']}, 0o600)
        state = 'fresh index selected; run index before chat' if result['fresh'] else 'existing index selected'
        print(f"Embeddings: {result['dimension']} dimensions; {state}.")
    except BaseException:
        # Capture fixed Docker status fields before cleanup destroys the evidence.
        if report:
            try:
                report.services()
            except Exception:
                pass  # Diagnostics must never prevent security cleanup.
        (RUNTIME / 'active-project.json').unlink(missing_ok=True)
        # Do not leave a partial deployment after failed discovery or incompatible metadata.
        try:
            with stage('cleanup'):
                subprocess.run([*command, 'down', '--remove-orphans'], cwd=ROOT, check=True, timeout=120)
        except Exception:
            raise SystemExit('Startup failed and cleanup could not finish. Check Docker; containers may remain.') from None
        raise SystemExit('Startup stopped. Resolve the error above and run up again.') from None

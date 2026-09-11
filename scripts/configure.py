"""Generate deployment from operator settings; never read repository contents."""
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
NET = '172.30.88.'
INFRA = 'codeairlock-infra:0.1.0'
WORK = 'codeairlock-workstation:0.1.0'


def envfile():
    path = ROOT / '.env'
    if not path.exists():
        shutil.copyfile(ROOT / '.env.example', path)
        path.chmod(0o600)
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        key, sep, val = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z_]+', key):
            raise ValueError('Invalid .env entry')
        result[key] = val
    return result


def endpoint(env, prefix, model=True):
    value = env[prefix + '_BASE_URL']
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError(f'{prefix}: use an http(s) base URL without credentials, query or fragment')
    ip = ipaddress.IPv4Address(env[prefix + '_IP'])
    allowed = [ipaddress.ip_network(x) for x in ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']]
    if not any(ip in network for network in allowed):
        # Explicit per-endpoint exception for an operator-trusted self-hosted server.
        # No blanket public egress flag: the exception repeats the exact pinned IP.
        trusted = (prefix in ('LLM', 'EMBED') and ip.is_global
                   and env.get(prefix + '_TRUSTED_PUBLIC_IP') == str(ip)
                   and url.scheme == 'https' and (url.port or 443) == 443)
        if not trusted:
            raise ValueError(f'{prefix}_IP must be RFC1918, or an explicitly pinned trusted public model server over HTTPS:443')
    data = {'url': value.rstrip('/'), 'ip': str(ip), 'key': env.get(prefix + '_API_KEY', '')}
    if model:
        if not env[prefix + '_MODEL'] or env[prefix + '_MODEL'] == 'REPLACE_ME':
            raise ValueError(f'Set {prefix}_MODEL in .env first')
        if url.path.rstrip('/').split('/')[-1] != 'v1':
            raise ValueError(f'{prefix}: this deployment uses an OpenAI-compatible /v1 base URL (also supported by Ollama)')
        data['model'] = env[prefix + '_MODEL']
    return data


def write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2) if not isinstance(data, str) else data)
    temp.chmod(mode)
    temp.replace(path)


def rules(outbound=(), inbound=(), loopback=False):
    # Pre-NAT DNS block is essential: Docker remaps 127.0.0.11:53 internally.
    return '''table inet private_env {
 chain early_output { type filter hook output priority -300; policy accept;
  udp dport 53 counter drop
  tcp dport 53 counter drop
 }
 chain output { type filter hook output priority 0; policy drop;
  ct state established,related accept
''' + ('  ip daddr 127.0.0.1 accept\n' if loopback else '') + ''.join(
        f'  ip daddr {ip} tcp dport {port} accept\n' for ip, port in outbound
    ) + '''  counter drop
 }
 chain input { type filter hook input priority 0; policy drop;
  ct state established,related accept
''' + ('  ip saddr 127.0.0.1 ip daddr 127.0.0.1 accept\n' if loopback else '') + ''.join(
        f'  {"ip saddr " + ip + " " if ip else ""}tcp dport {port} accept\n' for ip, port in inbound
    ) + '''  counter drop
 }
 chain forward { type filter hook forward priority 0; policy drop; }
}
'''


def generate(demo=False, project=None):
    os.umask(0o077)
    RUNTIME.mkdir(exist_ok=True)
    RUNTIME.chmod(0o700)
    env = envfile()
    repo = (ROOT / 'demo-repo') if demo else pathlib.Path(project['path'] if project else env.get('REPO_PATH', './demo-repo')).expanduser()
    if not repo.is_absolute(): repo = ROOT / repo
    repo = repo.resolve()
    if not repo.is_dir(): raise ValueError('REPO_PATH is not an existing directory')
    if repo == ROOT or repo in ROOT.parents:
        raise ValueError('Repository mount must not include this deployment, its .env or runtime secrets')
    access = project['access'] if project and not demo else env.get('REPO_ACCESS', 'read-only')
    home = 'home-demo' if demo else 'home-private'
    if project and not demo:
        from projects import home_volume
        home = home_volume(project)
    if access not in ('read-only', 'read-write'):
        raise ValueError('REPO_ACCESS must be read-only or read-write')
    expected = env.get('EMBED_DIMENSION', '').strip() if not demo else ''
    if expected and (not expected.isdecimal() or int(expected) <= 0):
        raise ValueError('EMBED_DIMENSION must be empty (automatic) or a positive integer')
    if demo:
        llm = {'url': 'http://synthetic:9000/v1', 'ip': NET+'5', 'model': 'demo-llm', 'key': ''}
        embed = {'url': 'http://synthetic:9000/v1', 'ip': NET+'5', 'model': 'demo-embed', 'key': ''}
    else:
        llm, embed = endpoint(env, 'LLM'), endpoint(env, 'EMBED')
        if not embed['key'] and embed['url'] == llm['url']:
            embed['key'] = llm['key']  # One credential for the exact same API base URL.
    gate = {'llm': llm, 'embed': embed}
    endpoints = [llm, embed]
    if not demo and env.get('SEARCH_BASE_URL'):
        gate['search'] = endpoint(env, 'SEARCH', model=False)
        endpoints.append(gate['search'])
    vols = []
    if not demo and env.get('CA_BUNDLE'):
        ca = pathlib.Path(env['CA_BUNDLE']).expanduser().resolve()
        # CA certificates are public material; copying them does not read repository code.
        shutil.copyfile(ca, RUNTIME / 'ca.pem')
        (RUNTIME / 'ca.pem').chmod(0o644)
        gate['ca'] = '/config/ca.pem'
        vols.append(f'{RUNTIME}/ca.pem:/config/ca.pem:ro')
    write(RUNTIME / 'gateway.json', gate)
    outbound = sorted(set((x['ip'], urlsplit(x['url']).port or (443 if x['url'].startswith('https:') else 80)) for x in endpoints))
    write(RUNTIME / 'gateway-net/rules.nft', rules(outbound, [(NET+'2',8081)]))
    write(RUNTIME / 'workstation-net/rules.nft', rules([(NET+'3',8081)], [(NET+'4',5900)], True))
    write(RUNTIME / 'ui-net/rules.nft', rules([(NET+'2',5900)], [('',6080)]))
    cfg = {
        'model': 'internal/'+llm['model'], 'small_model': 'internal/'+llm['model'],
        'enabled_providers': ['internal'], 'share': 'disabled', 'autoupdate': False,
        'agent': {'title': {'disable': True}},
        'provider': {'internal': {'npm': '@ai-sdk/openai-compatible', 'name': 'Internal LLM only',
            'options': {'baseURL': 'http://172.30.88.3:8081/llm/v1', 'apiKey': 'internal-gateway'},
            'models': {llm['model']: {'name': llm['model'], 'tool_call': True, 'limit': {'context': int(env.get('LLM_CONTEXT',32768)), 'output': int(env.get('LLM_MAX_OUTPUT',4096))}}}}},
        'indexing': {'enabled': False, 'provider': 'openai-compatible', 'model': embed['model'],
            'openai-compatible': {'baseUrl': 'http://172.30.88.3:8081/embed/v1', 'apiKey': 'internal-gateway'},
            'vectorStore': 'lancedb', 'lancedb': {'directory': '/home/node/index'}, 'embeddingBatchSize': 16, 'searchMaxResults': 12, 'searchMinScore': 0.15 if demo else 0.4},
        'permission': {'read': 'allow', 'glob': 'allow', 'grep': 'allow', 'lsp': 'allow', 'semantic_search': 'allow', 'bash': 'ask', 'edit': 'deny' if access == 'read-only' else 'ask', 'webfetch': 'deny', 'websearch': 'deny', 'external_directory': 'ask'},
        'lsp': json.loads((ROOT / 'config/lsp.json').read_text()),
        'mcp': {'approved_search': {'type':'local', 'command':['python3','/opt/search_mcp.py'], 'enabled': bool(gate.get('search'))}},
    }
    write(RUNTIME / 'workstation/kilo.json', cfg)
    # No credentials or repository contents enter bootstrap settings.
    write(RUNTIME / 'workstation/embedding-setup.json', {
        'repository': hashlib.sha256(str(repo).encode()).hexdigest(),
        'identity': {'url': embed['url'], 'ip': embed['ip'], 'model': embed['model'],
                     'revision': env.get('EMBED_REVISION', '') if not demo else ''},
        'expected_dimension': int(expected) if expected else None,
    })
    write(RUNTIME / 'workstation/access.json', {'repo_access': access})
    editor = {'telemetry.telemetryLevel':'off', 'update.mode':'none', 'extensions.autoCheckUpdates':False, 'extensions.autoUpdate':False,
              'git.autofetch':False, 'workbench.startupEditor':'none', 'security.workspace.trust.enabled':False,
              'kilo-code.new.model.providerID':'internal', 'kilo-code.new.model.modelID':llm['model'],
              'kilo-code.new.autocomplete.enableAutoTrigger':False, 'kilo-code.new.autocomplete.enableChatAutocomplete':False,
              'kilo-code.new.agentWorkStyle':'human-in-the-loop'}
    write(RUNTIME / 'workstation/editor.json', editor)
    for p in ['workstation','gateway-net','workstation-net','ui-net']:
        (RUNTIME / p).chmod(0o755)
    # VNC authentication is inside the trusted localhost-only pixel UI.
    password_file = RUNTIME / 'UI_PASSWORD.txt'
    if project and not demo:
        saved_password = RUNTIME / 'project-passwords' / (project['id'] + '.txt')
        if not saved_password.exists():
            old = password_file.read_text().strip() if project.get('legacy_home') and password_file.exists() else secrets.token_urlsafe(6)[:8]
            write(saved_password, old, 0o600)
        write(password_file, saved_password.read_text().strip(), 0o600)
    elif not password_file.exists():
        write(password_file, secrets.token_urlsafe(6)[:8], 0o600)
    password = password_file.read_text().strip()
    subprocess.run(['docker','run','--rm','--network','none','--user','0:0','--entrypoint','x11vnc',
                    '-v',f'{RUNTIME}/workstation:/out',WORK,'-storepasswd',password,'/out/vnc.pass'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (RUNTIME / 'workstation/vnc.pass').chmod(0o644)
    base = {'image':INFRA, 'read_only':True, 'cap_drop':['ALL'], 'security_opt':['no-new-privileges:true'],
            'tmpfs':['/tmp:rw,nosuid,nodev,size=64m'], 'restart':'no', 'logging':{'driver':'local','options':{'max-size':'5m','max-file':'2'}}}
    def guard(name, ip, extra_network=False):
        return {**base, 'cap_add':['NET_ADMIN'], 'command':['sh','/opt/guard.sh'],
                'volumes':[f'{RUNTIME}/{name}:/policy:ro'],
                'networks':{'private':{'ipv4_address':ip}, **({'uplink':{}} if extra_network else {})},
                'healthcheck':{'test':['CMD','test','-f','/tmp/ready'],'interval':'2s','timeout':'1s','retries':15},
                'sysctls':{'net.ipv4.ip_forward':'0','net.ipv6.conf.all.disable_ipv6':'1'}}
    services = {'workstation-net':guard('workstation-net',NET+'2'), 'gateway-net':guard('gateway-net',NET+'3',True), 'ui-net':guard('ui-net',NET+'4')}
    # Docker Desktop does not publish a port on a namespace with only internal networks.
    # Ingress gets its own bridge; nftables still forbids all new outbound connections.
    services['ui-net']['networks']['ingress'] = {}
    port = int(env.get('UI_PORT','6080'))
    if not 1024 <= port <= 65535: raise ValueError('UI_PORT must be 1024..65535')
    services['ui-net']['ports'] = [f'127.0.0.1:{port}:6080']
    services['gateway'] = {**base,'user':'65534:65534', 'network_mode':'service:gateway-net',
        'depends_on':{'gateway-net':{'condition':'service_healthy'}}, 'command':['python3','/opt/gateway.py'],
        'volumes':[f'{RUNTIME}/gateway.json:/config/gateway.json:ro',*vols]}
    services['ui'] = {**base,'user':'65534:65534','network_mode':'service:ui-net',
        'depends_on':{'ui-net':{'condition':'service_healthy'}},
        'command':['websockify','--web=/usr/share/novnc/','6080',NET+'2:5900']}
    services['workstation'] = {**base,'image':WORK,'user':'1000:1000','network_mode':'service:workstation-net',
        'labels': {'io.codeairlock.project': project['id'] if project and not demo else 'demo',
                   'io.codeairlock.access': access},
        'depends_on':{'workstation-net':{'condition':'service_healthy'},'gateway':{'condition':'service_started'}},
        'shm_size':'512mb','pids_limit':768,'mem_limit':'8g',
        'tmpfs':['/tmp:rw,nosuid,nodev,size=1g','/run:rw,nosuid,nodev,size=32m'],
        'volumes':[{'type':'bind','source':str(repo),'target':'/workspace','read_only':access == 'read-only','bind':{'create_host_path':False}},
                   f'{RUNTIME}/workstation:/config:ro',f'{home}:/home/node']}
    # One-shot bootstrap uses the same firewall, without mounting the repository.
    # It runs only when explicitly selected, before the workstation/UI can start.
    services['embedding-setup'] = {**base, 'image': WORK, 'user': '1000:1000',
        'profiles': ['bootstrap'], 'network_mode': 'service:workstation-net',
        'entrypoint': ['python3', '/bootstrap.py'], 'working_dir': '/tmp',
        'volumes': [f'{ROOT}/scripts/embedding_setup.py:/bootstrap.py:ro',
                    f'{RUNTIME}/workstation/embedding-setup.json:/embedding-setup.json:ro',
                    f'{home}:/home/node']}
    if demo:
        services['mock'] = {**base,'user':'65534:65534','networks':{'private':{'ipv4_address':NET+'5'}},'command':['python3','/opt/mock_api.py']}
    compose = {'name':'codeairlock','services':services,
        'networks':{'private':{'internal':True,'ipam':{'config':[{'subnet':NET+'0/24'}]}},'uplink':{},'ingress':{}},
        'volumes':{home:{}}}
    write(RUNTIME / 'compose.json', compose, 0o600)
    write(RUNTIME / 'mode', 'demo' if demo else 'private', 0o600)
    print(f'Configured {"SYNTHETIC DEMO" if demo else "TRUSTED ENDPOINTS"}. UI: http://127.0.0.1:{port}/vnc.html')
    print('VNC password: runtime/UI_PASSWORD.txt (first 8 characters; local file only).')


if __name__ == '__main__':
    import sys
    generate('--demo' in sys.argv)

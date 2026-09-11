"""Optional SearXNG, reachable only by the approval gateway, with restricted egress."""
import secrets

# Pin the image after fetching; never let startup silently update this service.
SEARXNG_IMAGE = 'searxng/searxng@sha256:e084201aa606fafce2151c8dc2844c9c3309025e90fbe7163b4f5e5183e474f0'
PROXY_IMAGE = 'codeairlock-search-proxy:0.1.0'


def enabled(env, demo=False):
    value = env.get('SEARXNG_ENABLED', 'false').lower()
    if value not in ('true', 'false'):
        raise ValueError('SEARXNG_ENABLED must be true or false')
    if value == 'true' and env.get('SEARCH_BASE_URL'):
        raise ValueError('Choose bundled SearXNG or SEARCH_BASE_URL, not both')
    return value == 'true' and not demo


def add_services(services, base, guard, runtime, net, write, rules):
    config = runtime / 'searxng'
    config.mkdir(exist_ok=True)
    config.chmod(0o755)
    secret_path = runtime / 'search-secret.txt'
    if not secret_path.exists():
        write(secret_path, secrets.token_hex(32), 0o600)
    # No plugins (including hostname lookups), autocomplete,
    # favicon fetching, or engines outside the configured pair.
    settings = {
        'use_default_settings': {'engines': {'keep_only': ['brave', 'duckduckgo']}},
        'general': {'debug': False, 'enable_metrics': False},
        'search': {'formats': ['json'], 'autocomplete': '', 'default_lang': 'en'},
        'server': {'secret_key': secret_path.read_text().strip(), 'limiter': False,
                   'image_proxy': False, 'public_instance': False},
        'enabled_plugins': [], 'plugins': {},
        'outgoing': {'request_timeout': 10.0, 'max_request_timeout': 15.0,
                     'pool_connections': 8, 'pool_maxsize': 8, 'retries': 0,
                     'proxies': {'all://': [f'http://{net}7:3128']}},
    }
    # JSON is a YAML subset, avoiding a host PyYAML dependency.
    write(config / 'settings.yml', settings)
    write(config / 'squid.conf', f'''http_port 3128
visible_hostname search-egress
acl searx_client src {net}6/32
acl CONNECT method CONNECT
acl TLS_port port 443
acl engines dstdomain -n search.brave.com duckduckgo.com html.duckduckgo.com lite.duckduckgo.com links.duckduckgo.com cdn.search.brave.com
acl private_dst dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4 ::/0
http_access deny !searx_client
http_access deny !CONNECT
http_access deny !TLS_port
http_access deny !engines
http_access deny private_dst
http_access allow searx_client
http_access deny all
cache deny all
access_log none
cache_log /dev/null
cache_store_log none
pid_filename /tmp/squid.pid
coredump_dir /tmp
forwarded_for delete
via off
shutdown_lifetime 1 seconds
connect_timeout 10 seconds
request_timeout 20 seconds
''')
    write(runtime / 'search-net/rules.nft', rules([(net+'7', 3128)], [(net+'3', 8080)], True))
    (runtime / 'search-net').chmod(0o755)
    services['search-net'] = guard('search-net', net+'6')
    services['search-proxy'] = {
        **base, 'image': PROXY_IMAGE, 'user': 'proxy',
        'networks': {'private': {'ipv4_address': net+'7'}, 'uplink': {}},
        'volumes': [f'{config}/squid.conf:/config/squid.conf:ro'],
        'pids_limit': 64, 'mem_limit': '256m', 'logging': {'driver': 'none'},
    }
    services['searxng'] = {
        **base, 'image': SEARXNG_IMAGE, 'user': '977:977',
        'network_mode': 'service:search-net',
        'entrypoint': ['/usr/local/searxng/.venv/bin/granian', 'searx.webapp:app'],
        'depends_on': {'search-net': {'condition': 'service_healthy'},
                       'search-proxy': {'condition': 'service_started'}},
        'environment': {'SEARXNG_SETTINGS_PATH': '/etc/searxng/settings.yml',
                        'GRANIAN_HOST': '0.0.0.0', 'GRANIAN_PORT': '8080',
                        'GRANIAN_INTERFACE': 'wsgi', 'GRANIAN_WORKERS': '1',
                        'GRANIAN_LOG_LEVEL': 'error', 'GRANIAN_ACCESS_LOG_ENABLED': 'false'},
        'volumes': [f'{config}/settings.yml:/etc/searxng/settings.yml:ro'],
        'tmpfs': ['/tmp:rw,nosuid,nodev,size=64m',
                  '/var/cache/searxng:rw,nosuid,nodev,size=64m,uid=977,gid=977'],
        'pids_limit': 128, 'mem_limit': '768m', 'logging': {'driver': 'none'},
        'healthcheck': {
            'test': ['CMD', '/usr/local/searxng/.venv/bin/python', '-c',
                     'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=2).close()'],
            'interval': '5s', 'timeout': '3s', 'retries': 12, 'start_period': '10s'},
    }

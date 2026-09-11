"""Operator-only client, invoked by host docker exec; never mounted in workstation."""
import http.client
import json
import re
import socket
import sys


def call(method, path):
    conn = http.client.HTTPConnection('localhost', timeout=190)
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.settimeout(190)
    conn.sock.connect('/tmp/search-admin.sock')
    try:
        conn.request(method, path)
        response = conn.getresponse()
        data = json.load(response)
        if response.status != 200:
            raise SystemExit(json.dumps(data, ensure_ascii=True))
        return data
    finally:
        conn.close()


if __name__ == '__main__':
    args = sys.argv[1:]
    if args == ['list']:
        # ASCII escaping prevents terminal control / bidi tricks in untrusted queries.
        print(json.dumps(call('GET', '/pending'), ensure_ascii=True, indent=2))
    elif len(args) == 2 and args[0] in ('approve', 'deny') and re.fullmatch(r'[a-f0-9]{32}', args[1]):
        if args[0] == 'approve':
            item = next((x for x in call('GET', '/pending')['requests'] if x['id'] == args[1]), None)
            if not item:
                raise SystemExit('Unknown, expired or already decided request')
            print('Exact query to release to SearxNG and its search engines:')
            print(json.dumps(item['query'], ensure_ascii=True))
            if input('Type approve to send this ONE request: ').strip() != 'approve':
                raise SystemExit('Nothing sent; request remains pending')
        result = call('POST', '/' + args[0] + '/' + args[1])
        print(json.dumps({'id': result['id'], 'status': result['status']}))
    else:
        raise SystemExit('Usage: search list | search approve ID | search deny ID')

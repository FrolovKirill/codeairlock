import contextlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import local_search
import search_control
from configure import write, rules


class LocalSearchTests(unittest.TestCase):
    def test_opt_in_and_no_ambiguous_endpoint(self):
        self.assertFalse(local_search.enabled({}))
        self.assertTrue(local_search.enabled({'SEARXNG_ENABLED': 'true'}))
        self.assertFalse(local_search.enabled({'SEARXNG_ENABLED': 'true'}, demo=True))
        for env in ({'SEARXNG_ENABLED': 'yes'}, {'SEARXNG_ENABLED': 'true', 'SEARCH_BASE_URL': 'http://other/search'}):
            with self.assertRaises(ValueError): local_search.enabled(env)

    def test_only_search_settings_mounted_no_repo_or_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            services = {}
            base = {'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true']}
            local_search.add_services(services, base, lambda name, ip: {'ip': ip}, root,
                                      '172.30.88.', write, rules)
            self.assertEqual(set(services), {'search-net', 'search-proxy', 'searxng'})
            for service in ('searxng', 'search-proxy'):
                self.assertTrue(services[service]['read_only'])
                self.assertNotIn('ports', services[service])
                self.assertEqual(services[service]['cap_drop'], ['ALL'])
                self.assertEqual(len(services[service]['volumes']), 1)
                self.assertNotIn('workspace', json.dumps(services[service]))
                self.assertNotIn('gateway.json', json.dumps(services[service]))
            self.assertEqual(services['searxng']['network_mode'], 'service:search-net')
            settings = json.loads((root/'searxng/settings.yml').read_text())
            self.assertEqual(settings['plugins'], {})
            self.assertEqual(settings['use_default_settings']['engines']['keep_only'], ['brave', 'duckduckgo'])
            self.assertEqual(settings['search']['formats'], ['json'])
            policy = (root/'search-net/rules.nft').read_text()
            self.assertIn('ip daddr 172.30.88.7 tcp dport 3128 accept', policy)
            self.assertNotIn('tcp dport 443 accept', policy)
            proxy = (root/'searxng/squid.conf').read_text()
            self.assertLess(proxy.index('http_access deny !engines'), proxy.index('http_access deny private_dst'))
            self.assertIn('http_access deny !CONNECT', proxy)
            self.assertIn('access_log none', proxy)

    def test_approval_is_structured_stdin_not_shell_and_lifecycle_locked(self):
        with patch.object(search_control, 'locked', return_value=contextlib.nullcontext()) as lock, \
             patch.object(search_control.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"status":"done"}')) as run:
            query = 'A harmless $(literal) "query"'
            search_control.control('approve', 'a'*32, query)
            lock.assert_called_once_with('operation', blocking=False, shared=True)
            self.assertEqual(json.loads(run.call_args.kwargs['input']), ['approve', 'a'*32, query])
            self.assertNotIn(query, run.call_args.args[0])
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_invalid_decisions_never_reach_docker(self):
        with patch.object(search_control.subprocess, 'run') as run:
            for args in [('allow-all',), ('approve', 'bad', 'query'), ('approve', 'a'*32, ''), ('deny', 'a'*32, None)]:
                with self.assertRaises(ValueError): search_control.control(*args)
            run.assert_not_called()

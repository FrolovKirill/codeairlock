"""Synthetic configuration tests; never contact actual model servers."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import configure
import ollama_adapter

class ModelSettingsTests(unittest.TestCase):
    def test_missing_empty_and_whitespace_use_identical_defaults(self):
        expected = {'protocol': 'openai', 'context': 16384, 'max_output': 1024, 'batch': 32}
        for value in (None, '', '   '):
            env = {} if value is None else {k: value for k in ('LLM_PROTOCOL','LLM_CONTEXT','LLM_MAX_OUTPUT','LLM_OLLAMA_BATCH')}
            self.assertEqual(configure.model_settings(env), expected)

    def test_explicit_values_have_no_old_ceiling(self):
        limits = configure.model_settings({'LLM_PROTOCOL':'ollama','LLM_CONTEXT':'65536','LLM_MAX_OUTPUT':'2048','LLM_OLLAMA_BATCH':'128'})
        self.assertEqual(limits, {'protocol':'ollama','context':65536,'max_output':2048,'batch':128})
        self.assertEqual(configure.model_settings({'LLM_CONTEXT':'131072'})['context'],131072)

    def test_output_must_fit_context_without_an_extra_ceiling(self):
        self.assertEqual(configure.model_settings({'LLM_CONTEXT':'131072','LLM_MAX_OUTPUT':'131072'})['max_output'],131072)
        with self.assertRaises(configure.ConfigurationError) as raised:
            configure.model_settings({'LLM_CONTEXT':'1024','LLM_MAX_OUTPUT':'1025'})
        self.assertEqual(raised.exception.code,'invalid_model_limits')

    def test_invalid_values_fail_without_echoing_input(self):
        for key in ('LLM_CONTEXT','LLM_MAX_OUTPUT','LLM_OLLAMA_BATCH'):
            for value in ('0','-1','1.5','PRIVATE_NOT_A_NUMBER'):
                with self.assertRaises(configure.ConfigurationError) as raised:
                    configure.model_settings({key:value})
                self.assertEqual(raised.exception.code,'invalid_model_limits')
                self.assertNotIn(value,str(raised.exception))

    def test_demo_ignores_operator_large_values(self):
        self.assertEqual(configure.model_settings({'LLM_CONTEXT':'65536','LLM_PROTOCOL':'ollama'},demo=True),configure.model_settings({}))

    def test_demo_gateway_receives_default_output_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            root=pathlib.Path(temp); (root/'demo-repo').mkdir(); (root/'config').mkdir()
            (root/'config/lsp.json').write_text('{}')
            def fake_docker(*args, **kwargs):
                (root/'runtime/workstation/vnc.pass').write_bytes(b'synthetic')
            with patch.object(configure,'ROOT',root), patch.object(configure,'RUNTIME',root/'runtime'), \
                 patch.object(configure,'envfile',return_value={}), \
                 patch.object(configure.subprocess,'run',side_effect=fake_docker):
                configure.generate(demo=True)
            gateway=json.loads((root/'runtime/gateway.json').read_text())
            self.assertEqual(gateway['llm']['max_output'],1024)

    def test_generated_kilo_and_ollama_receive_same_resolved_values(self):
        for overrides in ({'LLM_CONTEXT':'65536','LLM_MAX_OUTPUT':'2048','LLM_OLLAMA_BATCH':'128'},
                          {'LLM_CONTEXT':'','LLM_MAX_OUTPUT':'','LLM_OLLAMA_BATCH':''}, {}):
            with self.subTest(settings=overrides), tempfile.TemporaryDirectory() as temp:
                root=pathlib.Path(temp); (root/'demo-repo').mkdir();(root/'config').mkdir()
                (root/'config/lsp.json').write_text('{}')
                env={'LLM_PROTOCOL':'ollama','LLM_BASE_URL':'http://synthetic.internal:11434/v1',
                     'LLM_IP':'10.0.0.10','LLM_MODEL':'synthetic',
                     'EMBED_BASE_URL':'http://synthetic.internal:11434/v1','EMBED_IP':'10.0.0.10','EMBED_MODEL':'synthetic-embed',**overrides}
                def fake_docker(*args, **kwargs):
                    (root/'runtime/workstation/vnc.pass').write_bytes(b'synthetic')
                with patch.object(configure,'ROOT',root),patch.object(configure,'RUNTIME',root/'runtime'), \
                     patch.object(configure,'envfile',return_value=env),patch.object(configure.subprocess,'run',side_effect=fake_docker):
                    configure.generate()
                limits=configure.model_settings(env)
                gateway=json.loads((root/'runtime/gateway.json').read_text())
                kilo=json.loads((root/'runtime/workstation/kilo.json').read_text())
                self.assertEqual(gateway['llm']['max_output'],limits['max_output'])
                self.assertEqual(kilo['provider']['internal']['models']['synthetic']['limit'],
                                 {'context':limits['context'],'output':limits['max_output']})
                request=ollama_adapter.prepare({'messages':[{'role':'user','content':'Synthetic test'}]},gateway['llm'])
                self.assertEqual(request['options'],{'num_ctx':limits['context'],'num_predict':limits['max_output'],'num_batch':limits['batch']})

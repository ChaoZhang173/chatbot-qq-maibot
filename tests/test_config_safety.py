import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest
import tomlkit

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigSafetyTests(unittest.TestCase):
    def test_merge_preserves_private_fields_and_unrelated_tasks(self):
        apply = load('apply_profile')
        b = tomllib.loads((ROOT / 'config/bot.profile.toml').read_text())
        b['webui']['access_token'] = 'synthetic-test-value'
        b['bot']['qq_account'] = 'synthetic-account'
        m = tomllib.loads((ROOT / 'config/model_config.example.toml').read_text())
        m['api_providers'][0]['api_key'] = 'synthetic-test-value'
        m['model_task_config']['learner']['max_tokens'] = 3333
        bt, mt = apply.prepare(tomlkit.dumps(b).encode(), tomlkit.dumps(m).encode())
        nb, nm = tomllib.loads(bt), tomllib.loads(mt)
        self.assertEqual(nb['webui']['access_token'], b['webui']['access_token'])
        self.assertEqual(nb['bot']['qq_account'], b['bot']['qq_account'])
        self.assertEqual(nm['api_providers'], m['api_providers'])
        self.assertEqual(nm['model_task_config']['learner'], m['model_task_config']['learner'])
        self.assertEqual(nm['model_task_config']['replyer']['max_tokens'], 1536)

    def test_missing_keys_rejected(self):
        apply = load('apply_profile')
        with self.assertRaises(ValueError):
            apply.merge_existing({}, {'unrecognized': True})

    def test_credential_overlay_rejected(self):
        apply = load('apply_profile')
        with self.assertRaises(ValueError):
            apply.merge_existing({'api_key': ''}, {'api_key': 'synthetic-test-value'})

    def test_scanner_rejects_nonempty_credentials_without_echoing_value(self):
        check = load('check_public_files')
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'example.toml'
            p.write_text('api_key = "synthetic-test-value"\n')
            findings = check.scan_file(p, Path('example.toml'))
            self.assertIn('nonempty-credential-field', findings)
            self.assertNotIn('synthetic-test-value', str(findings))

    def test_qq_policy_is_deny_by_default(self):
        p = tomllib.loads((ROOT / 'config/adapter_policy.example.toml').read_text())
        self.assertEqual(p['defaults']['group']['default_action'], 'block')
        self.assertEqual(p['defaults']['private']['default_action'], 'block')
        self.assertEqual(len(p['adapters'][0]['group']['allow_ids']), 2)
        self.assertNotIn('*', p['adapters'][0]['group']['allow_ids'])

    def test_json_credentials_and_public_keys_are_rejected(self):
        check = load('check_public_files')
        import json
        p = Path('config/example.json')
        for field in ('api_key', 'password', 'token', 'public_key', 'sessdata', 'bili_jct'):
            findings = check.scan_file(p, p, json.dumps({field: 'synthetic-test-value'}))
            self.assertIn('nonempty-credential-field', findings)
            self.assertNotIn('synthetic-test-value', str(findings))
        key = 'ssh-' + 'ed25519 ' + 'A' * 60
        self.assertIn('public-key-or-certificate', check.scan_file(Path('a.txt'), Path('a.txt'), key))
        self.assertIn('private-or-binary-artifact', check.scan_file(Path('a.pub'), Path('a.pub'), ''))

    def test_adapter_runtime_source_exemption_is_narrow(self):
        check = load('check_public_files')
        p = Path('extensions/maibot_qq_native_adapter/runtime/bundle.py')
        self.assertEqual(check.scan_file(p, p, '# source module\n'), [])
        for name in ('state.json', 'state.sqlite', 'unknown.py'):
            q = p.parent / name
            self.assertIn('live-file-or-data', check.scan_file(q, q, '{}'))
        secret = 'sk-' + 'a' * 30
        self.assertIn('api-token', check.scan_file(p, p, secret))

    def test_current_bilibili_example_is_valid_and_paused(self):
        import sys
        sys.path.insert(0, str(ROOT / 'extensions'))
        from phase2.core import load_config
        config = load_config(ROOT / 'config/bilibili.current.example.json')
        self.assertTrue(config['paused'])
        self.assertEqual(config['bot_uid'], '')


if __name__ == '__main__':
    unittest.main()

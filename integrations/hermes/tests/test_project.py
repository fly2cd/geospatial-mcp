"""Unit tests for project-side integration. No real model or user home is accessed."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import project as p
import export_artifact as exporter


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'repo' / 'geospatial-mcp'
        self.project.mkdir(parents=True)
        self.source = self.root / 'installed-hermes'
        self.source.mkdir()
        self.model = {'provider': 'alibaba-cn', 'default': 'fixture-model', 'base_url': 'https://example.invalid/v1'}
        (self.source / 'config.yaml').write_text(json.dumps({'model': {**self.model, 'api_key': 'do-not-copy'}, 'other': 'private-setting'}))
        (self.source / '.env').write_text('DASHSCOPE_API_KEY=fake-test-key\nOPENAI_API_KEY=unrelated-secret\nDASHSCOPE_CN_BASE_URL=https://example.invalid/v1\n')
        (self.source / 'auth.json').write_text('{"secret":"must-not-copy"}')
        data = self.project / 'examples/analysis-data'
        data.mkdir(parents=True)
        (data / 'datasets.json').write_text('{"version":1,"datasets":{"points":{"kind":"vector","path":"points.geojson"}}}')
        python = self.project / '.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.touch()
        self.install = {'home': str(self.source), 'repo': str(self.source / 'hermes-agent'), 'python': 'hermes-python', 'executable': 'hermes-exe', 'version': '0.21.1'}
        self.discover = patch.object(p, 'discover_install', return_value=self.install)
        self.discover.start()
        self.addCleanup(self.discover.stop)

    def prepare(self):
        return p.prepare(project=self.project)

    def test_setup_is_idempotent_and_never_copies_secrets(self):
        before = {path.name: path.read_bytes() for path in self.source.iterdir()}
        info = self.prepare()
        home = Path(info['runtime_home'])
        first = {path.name: path.read_bytes() for path in home.iterdir()}
        self.assertEqual(self.prepare(), info)
        self.assertEqual(first, {path.name: path.read_bytes() for path in home.iterdir()})
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.source.iterdir()})
        self.assertFalse((home / '.env').exists())
        self.assertFalse((home / 'auth.json').exists())
        self.assertNotIn(b'do-not-copy', (home / 'config.yaml').read_bytes())
        self.assertNotIn(b'private-setting', (home / 'config.yaml').read_bytes())

    def test_config_is_hermes_yaml_and_only_geospatial_mcp(self):
        info = self.prepare()
        config = p.load_mapping(Path(info['config']))
        self.assertEqual(set(config['mcp_servers']), {'geospatial'})
        self.assertNotIn('mcpServers', config)
        server = config['mcp_servers']['geospatial']
        self.assertTrue(Path(server['command']).is_absolute())
        self.assertEqual(server['tools'], {'include': ['geo_*'], 'resources': True, 'prompts': False})
        self.assertNotIn('DASHSCOPE_API_KEY', server['env'])

    def test_unowned_config_is_not_overwritten(self):
        home = self.project / p.STATE_REL / 'home'
        home.mkdir(parents=True)
        config = home / 'config.yaml'; config.write_text('existing: true')
        with self.assertRaisesRegex(p.IntegrationError, 'unowned'):
            self.prepare()
        self.assertEqual(config.read_text(), 'existing: true')

    def test_invalid_ownership_marker_rejected(self):
        info = self.prepare()
        (Path(info['runtime_home']) / '.geospatial-managed.json').write_text('{"owner":"other"}')
        with self.assertRaisesRegex(p.IntegrationError, 'ownership'):
            self.prepare()

    def test_changed_mcp_config_not_overwritten(self):
        info = self.prepare(); target = Path(info['config'])
        config = p.load_mapping(target)
        config['mcp_servers']['other'] = {'url': 'https://example.invalid'}
        target.write_text(json.dumps(config)); before = target.read_bytes()
        with self.assertRaisesRegex(p.IntegrationError, 'differ'):
            self.prepare()
        self.assertEqual(before, target.read_bytes())

    def test_project_model_preferences_preserved(self):
        info = self.prepare(); target = Path(info['config'])
        config = p.load_mapping(target)
        config['model']['default'] = 'local-preference'
        target.write_text(json.dumps(config))
        self.assertEqual(self.prepare()['model']['default'], 'local-preference')

    def test_custom_data_selection_survives_later_launches(self):
        custom = self.project / 'custom-data'; custom.mkdir()
        (custom / 'datasets.json').write_text('{"version":1,"datasets":{"custom":{"kind":"vector","path":"points.geojson"}}}')
        info = p.prepare(project=self.project, data_root=custom)
        self.assertEqual(self.prepare()['data_root'], str(custom))
        self.assertEqual(info['data_root'], str(custom))

    def test_invalid_existing_data_root_is_rejected(self):
        info = self.prepare(); path = Path(info['config'])
        config = p.load_mapping(path)
        config['mcp_servers']['geospatial']['env']['GEO_DATA_ROOT'] = '../relative'
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(p.IntegrationError, 'data-root'):
            self.prepare()

    def test_runtime_paths_cannot_escape(self):
        for rel in ['..', '../outside', self.source, '.']:
            with self.subTest(rel=rel), self.assertRaises(p.IntegrationError):
                p.owned_path(self.project, rel)

    def test_junction_escape_rejected(self):
        if os.name != 'nt':
            self.skipTest('Windows junction test')
        link = self.project / 'redirect'
        # Native PowerShell only. Temporary paths are fixed test-owned paths.
        env = os.environ.copy(); env['GEO_TEST_LINK'] = str(link); env['GEO_TEST_TARGET'] = str(self.source)
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        'New-Item -ItemType Junction -Path $env:GEO_TEST_LINK -Target $env:GEO_TEST_TARGET | Out-Null'], env=env, check=True, capture_output=True)
        with self.assertRaises(p.IntegrationError):
            p.owned_path(self.project, 'redirect/config.yaml')

    def test_public_model_ignores_secrets(self):
        self.assertEqual(p.public_model({'model': {**self.model, 'api_key': 'secret', 'token': 'secret'}}), self.model)

    def test_credential_bearing_model_urls_rejected(self):
        for url in ['https://user:pass@example.invalid', 'https://example.invalid/?key=secret', 'https://example.invalid/#secret', 'file:///a']:
            with self.subTest(url=url), self.assertRaises(p.IntegrationError):
                p.public_model({'model': {**self.model, 'base_url': url}})

    def test_invalid_model_rejected(self):
        for model in ['name', {}, {'provider': 'x'}, {'default': 'x'}, {**self.model, 'default': 17}]:
            with self.subTest(model=model), self.assertRaises(p.IntegrationError):
                p.public_model({'model': model})

    def test_configuration_size_and_shape_are_bounded(self):
        target = self.root / 'oversize.yaml'; target.write_bytes(b'x' * (1024 * 1024 + 1))
        with self.assertRaisesRegex(p.IntegrationError, '1 MiB'):
            p.load_mapping(target)
        for content in ['[]', 'a: [', '\x00']:
            target.write_text(content)
            with self.assertRaises(p.IntegrationError):
                p.load_mapping(target)

    def test_nonchat_environment_drops_credentials_and_overrides(self):
        info = self.prepare()
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'secret', 'OPENAI_API_KEY': 'secret', 'OPENAI_BASE_URL': 'https://bad.invalid', 'HERMES_PROFILE': 'other', 'GEO_DATA_ROOT': 'other', 'PYTHONPATH': 'other'}, clear=True):
            env, secrets = p.runtime_environment(info)
        self.assertEqual(secrets, [])
        for name in ['DASHSCOPE_API_KEY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'HERMES_PROFILE', 'GEO_DATA_ROOT']:
            self.assertNotIn(name, env)
        self.assertEqual(env['HERMES_HOME'], info['runtime_home'])
        self.assertEqual(env['PYTHONPATH'], self.install['repo'])

    def test_chat_imports_selected_provider_only(self):
        info = self.prepare()
        with patch.dict(os.environ, {}, clear=True):
            env, secrets = p.runtime_environment(info, True)
        self.assertEqual(env['DASHSCOPE_API_KEY'], 'fake-test-key')
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(secrets, ['fake-test-key'])

    def test_shell_selected_provider_key_takes_precedence(self):
        info = self.prepare()
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'override'}, clear=True):
            env, secrets = p.runtime_environment(info, True)
        self.assertEqual(env['DASHSCOPE_API_KEY'], 'override')
        self.assertEqual(secrets, ['override'])

    def test_pool_only_credentials_are_not_copied(self):
        info = self.prepare(); (self.source / '.env').write_text('')
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(p.IntegrationError, 'OAuth/pool-only'):
            p.runtime_environment(info, True)

    def test_unknown_provider_fails_explicitly(self):
        info = self.prepare(); info['model']['provider'] = 'not-supported'
        with self.assertRaisesRegex(p.IntegrationError, 'credential bridge'):
            p.runtime_environment(info, True)

    def test_smoke_reports_cannot_overwrite_or_escape(self):
        report = p.smoke_report_path(project=self.project)
        self.assertEqual(report.parent, self.project / p.STATE_REL / 'reports')
        report.parent.mkdir(parents=True); report.touch()
        for path in [report, self.source / 'report.json', self.project / 'config.json', self.project / 'dev-log/README.md', self.project / 'dev-log/unrelated.json', self.project.parent / 'dev-log/stage-c1-test.json']:
            with self.subTest(path=path), self.assertRaises(p.IntegrationError):
                p.smoke_report_path(path, project=self.project)
        valid = self.project / 'dev-log/stage-c1-test.json'
        self.assertEqual(p.smoke_report_path(valid, project=self.project), valid)
        valid.parent.mkdir(parents=True)
        valid.write_text('existing evidence')
        with self.assertRaisesRegex(p.IntegrationError, 'overwrite'):
            p.smoke_report_path(valid, project=self.project)
        self.assertEqual(valid.read_text(), 'existing evidence')

    def test_smoke_report_log_junction_escape_rejected(self):
        if os.name != 'nt':
            self.skipTest('Windows junction test')
        link = self.project / 'dev-log'
        env = os.environ.copy()
        env['GEO_TEST_LINK'] = str(link)
        env['GEO_TEST_TARGET'] = str(self.source)
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        'New-Item -ItemType Junction -Path $env:GEO_TEST_LINK -Target $env:GEO_TEST_TARGET | Out-Null'],
                       env=env, check=True, capture_output=True)
        with self.assertRaisesRegex(p.IntegrationError, 'escapes'):
            p.smoke_report_path(link / 'stage-c1-test.json', project=self.project)
        self.assertFalse((self.source / 'stage-c1-test.json').exists())

    def test_chat_uses_server_alias_not_internal_toolset_name(self):
        info = self.prepare()
        with patch.object(p, 'prepare', return_value=info), patch.object(p, 'runtime_environment', return_value=({}, [])), patch.object(p.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, patch.object(sys, 'argv', ['project.py', 'chat', '--query', 'test']), self.assertRaises(SystemExit) as exit_context:
            p.main()
        self.assertEqual(exit_context.exception.code, 0)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--toolsets') + 1], 'geospatial')
        self.assertNotIn('mcp-geospatial', args)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        self.data = b'fixture binary content'
        self.artifact = 'art_' + 'a' * 32
        self.meta = {'artifact_id': self.artifact, 'format': 'gpkg', 'mime_type': 'application/geopackage+sqlite3',
                     'uri': 'artifact://' + self.artifact, 'size_bytes': len(self.data), 'sha256': hashlib.sha256(self.data).hexdigest()}

    def contents(self, data=None, mime=None):
        return [SimpleNamespace(blob=base64.b64encode(self.data if data is None else data).decode(), mime_type=mime or self.meta['mime_type'])]

    def test_download_and_idempotent_save(self):
        exporter.validate_metadata(self.artifact, self.meta)
        payload = exporter.decode_payload(self.meta, self.contents())
        result = exporter.save_payload(self.project, self.meta, payload)
        target = Path(result['client_file'])
        self.assertEqual(target.suffix, '.gpkg')
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual(exporter.save_payload(self.project, self.meta, payload), result)

    def test_invalid_artifact_ids_rejected(self):
        for value in [None, '../outside', 'art_123', 'art_' + 'G' * 32, 'artifact://' + self.artifact]:
            with self.subTest(value=value), self.assertRaises(p.IntegrationError):
                exporter.validate_id(value)

    def test_bad_metadata_rejected(self):
        for key, value in [('artifact_id', 'art_' + 'b' * 32), ('uri', 'https://example.invalid'), ('format', '../gpkg'), ('mime_type', 'text/plain'), ('size_bytes', True), ('size_bytes', -1), ('size_bytes', exporter.MAX_BYTES + 1), ('sha256', 'bad')]:
            meta = {**self.meta, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(p.IntegrationError):
                exporter.validate_metadata(self.artifact, meta)

    def test_corrupted_resources_rejected(self):
        resources = [[], self.contents() * 2, self.contents(b'tampered'), self.contents(mime='text/plain'), [SimpleNamespace(blob='*invalid*', mime_type=self.meta['mime_type'])]]
        for contents in resources:
            with self.subTest(contents=contents), self.assertRaises(p.IntegrationError):
                exporter.decode_payload(self.meta, contents)

    def test_existing_different_file_never_overwritten(self):
        result = exporter.save_payload(self.project, self.meta, self.data)
        target = Path(result['client_file']); target.write_bytes(b'keep me')
        with self.assertRaisesRegex(p.IntegrationError, 'overwrite'):
            exporter.save_payload(self.project, self.meta, self.data)
        self.assertEqual(target.read_bytes(), b'keep me')

    def test_unverified_payload_never_saved(self):
        with self.assertRaises(p.IntegrationError):
            exporter.save_payload(self.project, self.meta, b'wrong')
        self.assertFalse((self.project / p.STATE_REL / 'exports').exists())


if __name__ == '__main__':
    unittest.main()

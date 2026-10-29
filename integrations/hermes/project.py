"""Project-local launcher for the installed Hermes Agent; never edits its global home.

Run with geospatial-mcp's Python. Hermes always runs in its own installed environment.
No Hermes dependency is introduced into the geospatial service package.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from dotenv import dotenv_values
from geospatial_mcp.errors import ConfigurationError

PROJECT = Path(__file__).resolve().parents[2]
STATE_REL = Path('.cache/hermes-project')
SERVER_NAME = 'geospatial'
MANAGED_MARKER = {'owner': 'gab-geospatial-hermes', 'schema_version': 1}


class IntegrationError(Exception):
    pass


def owned_path(project, relative):
    project = project.resolve(strict=True)
    path = (project / relative).resolve()
    if path == project or not path.is_relative_to(project):
        raise IntegrationError('Runtime path escapes the geospatial project.')
    return path


def discover_install(home=None):
    candidates = []
    if home:
        candidates.append(Path(home))
    else:
        override = os.environ.get('HERMES_HOME')
        if override:
            candidates.append(Path(override))
        command = shutil.which('hermes')
        if command:
            candidates.append(Path(command).resolve().parent.parent)
        if os.name == 'nt':
            candidates.append(Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'hermes')
    for candidate in candidates:
        candidate = candidate.resolve()
        repo = candidate / 'hermes-agent'
        python = repo / 'venv/Scripts/python.exe'
        exe = candidate / 'bin/hermes.exe'
        metadata = repo / 'pyproject.toml'
        if all(path.is_file() for path in (python, exe, metadata, candidate / 'config.yaml')):
            package = tomllib.loads(metadata.read_text(encoding='utf-8'))['project']
            if package.get('name') == 'hermes-agent':
                return {'home': str(candidate), 'repo': str(repo), 'python': str(python),
                        'executable': str(exe), 'version': package.get('version', 'unknown')}
    raise IntegrationError('Installed Hermes Agent was not found. Supply --hermes-home pointing to its original home.')


def load_mapping(path):
    with path.open('rb') as stream:
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise IntegrationError('Configuration exceeds 1 MiB.')
    try:
        value = yaml.safe_load(content.decode('utf-8-sig'))
    except (UnicodeError, yaml.YAMLError):
        raise IntegrationError('Invalid UTF-8 YAML configuration.') from None
    if not isinstance(value, dict):
        raise IntegrationError('Configuration must be a YAML mapping.')
    return value


def public_model(config):
    model = config.get('model')
    if not isinstance(model, dict):
        raise IntegrationError('This integration requires a model mapping with provider/default.')
    result = {}
    for key in ('provider', 'default', 'base_url'):
        value = model.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 2048:
                raise IntegrationError('Unsupported model configuration.')
            result[key] = value
    if not result.get('provider') or not result.get('default'):
        raise IntegrationError('No configured Hermes model/provider was found.')
    if 'base_url' in result:
        url = urlsplit(result['base_url'])
        if url.scheme not in {'https', 'http'} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise IntegrationError('Model base_url must not contain credentials, query parameters or fragments.')
    return result


def build_config(model, service_python, data_root, workspace_root, runtime_home):
    return {
        'model': model,
        'agent': {'max_turns': 12},
        'terminal': {'cwd': str(runtime_home)},
        'telemetry': {'enabled': False},
        'mcp_servers': {SERVER_NAME: {
            'command': str(service_python), 'args': ['-m', 'geospatial_mcp', '--transport', 'stdio'],
            'env': {'GEO_DATA_ROOT': str(data_root), 'GEO_WORKSPACE_ROOT': str(workspace_root),
                    'GEO_LOG_LEVEL': 'ERROR', 'PYTHONUTF8': '1'},
            'connect_timeout': 30, 'timeout': 60,
            'tools': {'include': ['geo_*'], 'resources': True, 'prompts': False},
        }},
    }


def prepare(home=None, data_root=None, project=PROJECT):
    install = discover_install(home)
    source_home = Path(install['home'])
    original = source_home / 'config.yaml'
    model = public_model(load_mapping(original))
    runtime_home = owned_path(project, STATE_REL / 'home')
    if runtime_home == source_home or runtime_home.is_relative_to(source_home) or source_home.is_relative_to(runtime_home):
        raise IntegrationError('Project runtime and original Hermes home must be disjoint.')
    workspace = owned_path(project, STATE_REL / 'workspace')
    config_path = owned_path(project, STATE_REL / 'home/config.yaml')
    marker = owned_path(project, STATE_REL / 'home/.geospatial-managed.json')
    if marker.exists() and load_mapping(marker) != MANAGED_MARKER:
        raise IntegrationError('Invalid project runtime ownership marker.')
    if config_path.exists() and not marker.exists():
        raise IntegrationError('Refusing to overwrite an unowned Hermes runtime config.')
    existing = load_mapping(config_path) if config_path.exists() else None
    # Preserve the administrator's registered data selection on later launches.
    if data_root is None and existing is not None:
        try:
            data_root = existing['mcp_servers'][SERVER_NAME]['env']['GEO_DATA_ROOT']
            if not isinstance(data_root, str) or not Path(data_root).is_absolute():
                raise ValueError('not an absolute data path')
        except (KeyError, TypeError, ValueError):
            raise IntegrationError('Invalid existing project data-root setting.') from None
    data = Path(data_root).resolve(strict=True) if data_root else (project / 'examples/analysis-data').resolve(strict=True)
    python = (project / '.venv/Scripts/python.exe').resolve(strict=True)
    # Reuse the service's own boundary/configuration validation before writing anything.
    from geospatial_mcp.settings import Settings
    from geospatial_mcp.storage import DatasetStore
    settings = Settings(data_root=data, catalog_path=data / 'datasets.json', workspace_root=workspace)
    DatasetStore(settings)
    runtime_home.mkdir(parents=True, exist_ok=True)
    config = build_config(model, python, data, workspace, runtime_home)
    if existing is not None:
        if existing.get('mcp_servers') != config['mcp_servers']:
            raise IntegrationError('Existing project MCP settings differ; review config manually instead of overwriting.')
        # Preserve local model/UI preferences. Refresh is deliberately not automatic.
        config = existing
        model = public_model(config)
    else:
        with config_path.open('x', encoding='utf-8', newline='\n') as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
    if not marker.exists():
        with marker.open('x', encoding='utf-8') as stream:
            json.dump(MANAGED_MARKER, stream)
    soul = owned_path(project, STATE_REL / 'home/SOUL.md')
    instructions = Path(__file__).with_name('instructions.md')
    if not soul.exists():
        with soul.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(instructions.read_text(encoding='utf-8'))
    return {'installation': install, 'runtime_home': str(runtime_home), 'config': str(config_path),
            'data_root': str(data), 'workspace_root': str(workspace), 'model': model,
            'original_config_sha256': hashlib.sha256(original.read_bytes()).hexdigest()}


# Only the selected provider's credentials are imported, in memory, from the original .env.
# OAuth auth.json is deliberately not copied. Unsupported providers need explicit adaptation.
PROVIDER_ENV = {
    'alibaba': ('DASHSCOPE_API_KEY', 'DASHSCOPE_BASE_URL'),
    'alibaba-cn': ('DASHSCOPE_API_KEY', 'DASHSCOPE_CN_BASE_URL', 'DASHSCOPE_BASE_URL'),
    'alibaba-coding-plan': ('ALIBABA_CODING_PLAN_API_KEY', 'DASHSCOPE_API_KEY', 'ALIBABA_CODING_PLAN_BASE_URL'),
    'deepseek': ('DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL'),
    'openai-api': ('OPENAI_API_KEY', 'OPENAI_BASE_URL'),
    'anthropic': ('ANTHROPIC_API_KEY', 'ANTHROPIC_TOKEN', 'ANTHROPIC_BASE_URL'),
}


def runtime_environment(info, with_model=False):
    env = os.environ.copy()
    # Avoid importing repository tool modules, sticky profiles or arbitrary runtime overrides.
    for key in list(env):
        if key.startswith(('HERMES_', 'GEO_')) or key.endswith(('_API_KEY', '_TOKEN', '_BASE_URL')) or key in {'PYTHONPATH', 'MCP_CATALOG_PATH'}:
            env.pop(key, None)
    env.update({'HERMES_HOME': info['runtime_home'], 'PYTHONPATH': info['installation']['repo'],
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
                'UV_CACHE_DIR': str(Path(info['runtime_home']).parent / 'uv')})
    secrets = []
    if with_model:
        keys = PROVIDER_ENV.get(info['model']['provider'])
        if keys is None:
            raise IntegrationError('Selected provider has no verified credential bridge. No auth.json will be copied.')
        values = dotenv_values(Path(info['installation']['home']) / '.env', interpolate=False)
        for key in keys:
            value = os.environ.get(key) or values.get(key)
            if value:
                env[key] = value
                if key.endswith(('_API_KEY', '_TOKEN')):
                    secrets.append(value)
        if not secrets:
            raise IntegrationError('Selected provider has no environment API key. OAuth/pool-only credentials are not copied.')
    return env, secrets


def smoke_report_path(requested=None, project=PROJECT):
    if requested is None:
        report = owned_path(project, STATE_REL / 'reports' / ('smoke-' + uuid4().hex + '.json'))
    else:
        report = requested.resolve()
    roots = [owned_path(project, STATE_REL / 'reports'), owned_path(project, Path('dev-log'))]
    if not any(report.parent == root for root in roots) or report.suffix != '.json':
        raise IntegrationError('Report must be a new JSON in project runtime reports/ or project dev-log/.')
    if report.parent == roots[1] and not report.name.startswith('stage-c1-'):
        raise IntegrationError('Evidence report names must start with stage-c1-.')
    if report.exists():
        raise IntegrationError('Refusing to overwrite an existing smoke report.')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'test', 'smoke', 'chat', 'export'])
    parser.add_argument('--hermes-home', type=Path)
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--query')
    parser.add_argument('--query-file', type=Path)
    parser.add_argument('--max-turns', type=int, default=12)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--artifact-id')
    args = parser.parse_args()
    if not 1 <= args.max_turns <= 20:
        parser.error('--max-turns must be between 1 and 20')
    if args.query and args.query_file:
        parser.error('Choose --query or --query-file')
    if args.action == 'export' and not args.artifact_id:
        parser.error('export requires --artifact-id')
    try:
        info = prepare(args.hermes_home, args.data_root)
        if args.action == 'setup':
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return
        if args.action == 'export':
            from export_artifact import export_artifact
            print(json.dumps(asyncio.run(export_artifact(info, args.artifact_id)), ensure_ascii=False, indent=2))
            return
        env, _secrets = runtime_environment(info, with_model=args.action == 'chat')
        if args.action == 'smoke':
            report = smoke_report_path(args.report)
            command = [info['installation']['python'], '-B', str(Path(__file__).with_name('smoke_runtime.py')), '--report', str(report)]
        else:
            command = [info['installation']['executable']]
            if args.action == 'test':
                command += ['mcp', 'test', SERVER_NAME]
            else:
                command += ['chat', '--toolsets', SERVER_NAME, '--max-turns', str(args.max_turns)]
                if args.query:
                    command += ['--query', args.query]
                if args.query_file:
                    command += ['--query-file', str(args.query_file.resolve(strict=True))]
        result = subprocess.run(command, env=env, cwd=info['runtime_home'])
        original = Path(info['installation']['home']) / 'config.yaml'
        if hashlib.sha256(original.read_bytes()).hexdigest() != info['original_config_sha256']:
            raise IntegrationError('Original Hermes config changed during execution; inspect it before proceeding.')
        raise SystemExit(result.returncode)
    except (IntegrationError, ConfigurationError, OSError) as exc:
        print('Hermes integration: ' + str(exc), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == '__main__':
    # Export helpers import these shared path/error definitions, not a second copy.
    sys.modules.setdefault('project', sys.modules[__name__])
    main()

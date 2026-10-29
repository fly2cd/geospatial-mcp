"""Acceptance test using the *installed* Hermes registry and MCP resource handlers.

No mock server, no LLM, no source-workspace copying. Run via project.py smoke.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from hermes_cli import __version__ as hermes_version
from hermes_cli.config import load_config
from tools.mcp_tool_discovery import discover_mcp_tools, get_mcp_status
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from tools.registry import registry

PREFIX = 'mcp__geospatial__'
NATIVE = {'geo_vector_info', 'geo_raster_info', 'geo_list_datasets', 'geo_get_task_info',
          'geo_get_artifact_info', 'geo_vector_buffer', 'geo_vector_clip', 'geo_spatial_join', 'geo_render_vector_map'}


def decode_result(raw):
    outer = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(outer, dict) or outer.get('error'):
        raise AssertionError('Hermes tool returned an error: ' + str(outer))
    value = outer.get('structuredContent', outer.get('result', outer))
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def run(report_path):
    home = Path(os.environ['HERMES_HOME']).resolve(strict=True)
    config = load_config()
    if set(config.get('mcp_servers', {})) != {'geospatial'}:
        raise AssertionError('Acceptance environment must contain only the project MCP server')
    report = {'hermes_version': hermes_version, 'model_invoked': False, 'steps': [], 'downloads': []}
    try:
        names = discover_mcp_tools(allowed_mcp_names=['geospatial'])
        report['tools'] = sorted(names)
        expected = {PREFIX + name for name in NATIVE | {'read_resource', 'list_resources'}}
        if set(names) != expected:
            raise AssertionError(f'Expected 11 Hermes tools; got {names}; status={get_mcp_status()}')
        for name in expected:
            if registry.get_toolset_for_tool(name) != 'mcp-geospatial':
                raise AssertionError('Wrong Hermes toolset ownership')

        def call(name, arguments):
            result = decode_result(registry.dispatch(PREFIX + name, arguments))
            if name in {'geo_vector_buffer', 'geo_vector_clip', 'geo_spatial_join', 'geo_render_vector_map'}:
                report['steps'].append({'tool': name, 'result': result})
            return result

        datasets = call('geo_list_datasets', {})
        assert {'points', 'mask'} <= {row['dataset_id'] for row in datasets['datasets']}
        info = call('geo_vector_info', {'dataset_id': 'points'})
        assert info['feature_count'] == 3
        buffered = call('geo_vector_buffer', {'source': {'dataset_id': 'points'}, 'distance_meters': 10})
        clipped = call('geo_vector_clip', {'source': {'artifact_id': buffered['artifacts'][0]['artifact_id']}, 'mask': {'dataset_id': 'mask'}})
        joined = call('geo_spatial_join', {'left': {'artifact_id': clipped['artifacts'][0]['artifact_id']}, 'right': {'dataset_id': 'mask'}})
        mapped = call('geo_render_vector_map', {'source': {'artifact_id': joined['artifacts'][0]['artifact_id']},
                      'value_field': 'left_value', 'title': 'Hermes geospatial workflow'})
        assert [x['summary']['feature_count'] for x in [buffered, clipped, joined, mapped]] == [3, 2, 3, 3]
        for result in [buffered, clipped, joined, mapped]:
            artifact = result['artifacts'][0]
            metadata = call('geo_get_artifact_info', {'artifact_id': artifact['artifact_id']})
            assert metadata['sha256'] == artifact['sha256']
            status = call('geo_get_task_info', {'task_id': result['task_id']})
            assert status['status'] == 'succeeded'
            rendered = call('read_resource', {'uri': artifact['uri']})
            # This marker is emitted by the installed Hermes binary-resource handler.
            match = re.fullmatch(r'\[MCP resource saved to (.+) \([^\n]+, (\d+) bytes\) .+\]', rendered)
            if not match:
                raise AssertionError('Hermes failed to materialize the binary resource: ' + str(rendered))
            cached = Path(match[1]).resolve(strict=True)
            if not cached.is_relative_to(home):
                raise AssertionError('Hermes cached an artifact outside the project home')
            data = cached.read_bytes()
            assert len(data) == artifact['size_bytes'] == int(match[2])
            assert hashlib.sha256(data).hexdigest() == artifact['sha256']
            report['downloads'].append({'artifact_id': artifact['artifact_id'], 'format': artifact['format'],
                        'mime_type': artifact['mime_type'], 'size_bytes': len(data), 'sha256': artifact['sha256'],
                        'client_cache_file': str(cached), 'task_id': result['task_id']})
        rejected = registry.dispatch(PREFIX + 'geo_vector_info', {'dataset_id': '../outside'})
        error = json.loads(rejected) if isinstance(rejected, str) else rejected
        assert error.get('error'), 'Invalid reference must remain an error through Hermes'
        assert str(home) not in error['error'], 'Error should not expose project-home paths'
        report['negative_test'] = 'PASS: invalid dataset reference is a Hermes error'
        report['status'] = 'passed'
    finally:
        shutdown_mcp_servers()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    run(parser.parse_args().report.resolve())

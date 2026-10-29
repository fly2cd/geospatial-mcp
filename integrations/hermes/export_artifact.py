"""Download a standard MCP artifact into a controlled client export directory."""
import base64
import binascii
import hashlib
from pathlib import Path
import re

from fastmcp import Client
from fastmcp.exceptions import ClientError, FastMCPError, MCPError
from fastmcp.client.transports import StdioTransport
from project import IntegrationError, SERVER_NAME, STATE_REL, PROJECT, load_mapping, owned_path

FORMATS = {'gpkg': 'application/geopackage+sqlite3', 'png': 'image/png'}
MAX_BYTES = 64 * 1024 * 1024  # Client export cap; not a process/network memory sandbox.


def validate_id(artifact_id):
    if not isinstance(artifact_id, str) or re.fullmatch(r'art_[a-f0-9]{32}', artifact_id) is None:
        raise IntegrationError('Expected artifact ID art_<32 lowercase hex characters>.')


def validate_metadata(artifact_id, metadata):
    validate_id(artifact_id)
    if not isinstance(metadata, dict) or metadata.get('artifact_id') != artifact_id:
        raise IntegrationError('Artifact metadata identity mismatch.')
    if metadata.get('format') not in FORMATS or metadata.get('mime_type') != FORMATS[metadata['format']]:
        raise IntegrationError('Unsupported artifact format/MIME type.')
    if metadata.get('uri') != 'artifact://' + artifact_id:
        raise IntegrationError('Unexpected artifact URI.')
    if type(metadata.get('size_bytes')) is not int or not 0 < metadata['size_bytes'] <= MAX_BYTES:
        raise IntegrationError('Artifact size exceeds client export limit.')
    if re.fullmatch(r'[a-f0-9]{64}', str(metadata.get('sha256', ''))) is None:
        raise IntegrationError('Invalid artifact checksum.')


def decode_payload(metadata, contents):
    if len(contents) != 1 or getattr(contents[0], 'mime_type', None) != metadata['mime_type']:
        raise IntegrationError('Resource MIME/count mismatch.')
    blob = getattr(contents[0], 'blob', None)
    if not isinstance(blob, str) or len(blob) > 4 * ((MAX_BYTES + 2) // 3):
        raise IntegrationError('Expected a bounded binary resource.')
    try:
        payload = base64.b64decode(blob, validate=True)
    except (ValueError, binascii.Error):
        raise IntegrationError('Invalid binary resource encoding.') from None
    if len(payload) != metadata['size_bytes'] or hashlib.sha256(payload).hexdigest() != metadata['sha256']:
        raise IntegrationError('Downloaded artifact failed size/SHA256 verification.')
    return payload


def save_payload(project, metadata, payload):
    validate_metadata(metadata.get('artifact_id'), metadata)
    if len(payload) != metadata['size_bytes'] or hashlib.sha256(payload).hexdigest() != metadata['sha256']:
        raise IntegrationError('Refusing to save an unverified payload.')
    directory = owned_path(project, STATE_REL / 'exports')
    directory.mkdir(parents=True, exist_ok=True)
    requested = directory / (metadata['artifact_id'] + '.' + metadata['format'])
    target = owned_path(project, requested)
    # Resolve again to detect a pre-existing file symlink pointing at another file.
    if target.parent != directory or target.name != requested.name:
        raise IntegrationError('Export target must not be redirected.')
    if target.exists():
        if target.stat().st_size != len(payload) or hashlib.sha256(target.read_bytes()).hexdigest() != metadata['sha256']:
            raise IntegrationError('Refusing to overwrite a different existing export.')
    else:
        with target.open('xb') as stream:
            stream.write(payload)
    return {'artifact_id': metadata['artifact_id'], 'format': metadata['format'],
            'client_file': str(target), 'size_bytes': len(payload), 'sha256': metadata['sha256'],
            'downloaded_via': 'MCP resources/read'}


async def export_artifact(info, artifact_id, project=PROJECT):
    validate_id(artifact_id)
    cfg = load_mapping(Path(info['config']))['mcp_servers'][SERVER_NAME]
    # Service environment only: no model key is needed or imported by this client.
    transport = StdioTransport(command=cfg['command'], args=cfg['args'], env=cfg['env'],
                               cwd=info['runtime_home'], keep_alive=False)
    try:
        async with Client(transport, timeout=60, init_timeout=30) as client:
            response = await client.call_tool('geo_get_artifact_info', {'artifact_id': artifact_id})
            if response.is_error or response.structured_content is None:
                raise IntegrationError('Artifact metadata query failed.')
            metadata = response.structured_content
            validate_metadata(artifact_id, metadata)
            contents = await client.read_resource(metadata['uri'])
            payload = decode_payload(metadata, contents)
    except (ClientError, FastMCPError, MCPError, TimeoutError):
        raise IntegrationError('MCP export failed: check the artifact ID, task state and server availability.') from None
    return save_payload(project, metadata, payload)

"""Prepare a hash-bound R01 audit recipe without opening a database or running it.

The registry has71 entries and60 business domains. Preparation never constitutes
business acceptance. Runtime files and private snapshot bytes must match the
reviewed hashes; machine-specific recipes belong outside the source repository.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit


class ScopeError(ValueError):
    pass


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def registry_entries(raw):
    tree = ast.parse(raw)
    assignment = next((node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'ENTRIES'
                               for t in node.targets)), None)
    if assignment is None or not isinstance(assignment.value, (ast.Tuple, ast.List)):
        raise ScopeError('REGISTRY_ENTRIES_UNAVAILABLE')
    entries = []
    for node in assignment.value.elts:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'Entry' and len(node.args) == 6 and not node.keywords):
            raise ScopeError('REGISTRY_ENTRY_NOT_STATIC')
        values = [ast.literal_eval(arg) for arg in node.args]
        entries.append(dict(zip(('entry_id', 'source', 'native', 'domain',
                                 'disposition', 'target'), values)))
    identities = [entry['entry_id'] for entry in entries]
    if len(identities) != len(set(identities)):
        raise ScopeError('DUPLICATE_REGISTRY_ENTRY')
    business = [entry for entry in entries if entry['disposition'] == 'business']
    if len(entries) != 71 or len(business) != 60:
        raise ScopeError('R01_DENOMINATOR_CHANGED')
    return entries


def _path(value):
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ScopeError('ABSOLUTE_TRUSTED_PATH_REQUIRED')
    return path


def build_manifest(snapshot_raw, *, snapshot_sha256, runtime_root, revision, image,
                   current_root, evidence_dir, files, rows, byte_limit, api_entries,
                   api_base_url, api_token_env):
    if not re.fullmatch(r'[0-9a-f]{64}', snapshot_sha256) or sha256(snapshot_raw) != snapshot_sha256:
        raise ScopeError('SNAPSHOT_HASH_MISMATCH')
    if len(snapshot_raw) > 4 * 1024 * 1024:
        raise ScopeError('SNAPSHOT_TOO_LARGE')
    snapshot = json.loads(snapshot_raw)
    if snapshot.get('format') != 'r01-acceptance-scope@1':
        raise ScopeError('SNAPSHOT_FORMAT_UNSUPPORTED')
    runtime_root = Path(runtime_root)
    registry = runtime_root / 'adapters/registry.py'
    registry_raw = registry.read_bytes()
    if snapshot.get('registry_sha256') != sha256(registry_raw):
        raise ScopeError('RUNTIME_REGISTRY_HASH_MISMATCH')
    entries = registry_entries(registry_raw)
    business = {entry['entry_id']: entry for entry in entries
                if entry['disposition'] == 'business'}
    definitions = snapshot.get('definitions')
    if not isinstance(definitions, list) or len(definitions) != 60:
        raise ScopeError('R01_BUSINESS_SCOPE_MISMATCH')
    by_id = {item['entry_id']: item for item in definitions}
    if len(by_id) != 60 or set(by_id) != set(business):
        raise ScopeError('R01_BUSINESS_SCOPE_MISMATCH')
    for identity, expected in business.items():
        row = by_id[identity]
        if any(row.get(key) != expected[key] for key in ('source', 'native', 'domain')):
            raise ScopeError('RUNTIME_ENTRY_IDENTITY_MISMATCH')
        if not (isinstance(row.get('dataset'), str) and row['dataset']
                and re.fullmatch(r'[0-9a-f]{64}', row.get('schema_id', ''))
                and isinstance(row.get('rule'), str) and row['rule']):
            raise ScopeError('DATASET_CONTRACT_UNAVAILABLE')
    if len({row['dataset'] for row in definitions}) != 60:
        raise ScopeError('DUPLICATE_BUSINESS_DATASET')
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ScopeError('EXACT_RELEASE_IDENTITY_REQUIRED')
    api_entries = tuple(api_entries)
    if not (1 <= len(api_entries) <= 100 and len(set(api_entries)) == len(api_entries)
            and set(api_entries) <= set(business)):
        raise ScopeError('EXACT_BUSINESS_API_ENTRIES_REQUIRED')
    if not (type(files) is int and 1 <= files <= 100000
            and type(rows) is int and 1 <= rows <= 1_000_000_000
            and type(byte_limit) is int and 1 <= byte_limit <= 1 << 42):
        raise ScopeError('FINITE_AUDIT_LIMITS_REQUIRED')
    current, evidence = _path(current_root), _path(evidence_dir)
    if evidence == current or current in evidence.parents:
        raise ScopeError('AUDIT_OUTPUT_INSIDE_CURRENT_STORE')
    url = urlsplit(api_base_url)
    if (url.scheme not in ('http', 'https') or url.hostname not in ('localhost', '127.0.0.1', '::1')
            or url.username or url.password or url.query or url.fragment or url.path not in ('', '/')
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', api_token_env)):
        raise ScopeError('LOOPBACK_API_AND_TOKEN_ENV_NAME_REQUIRED')
    names = ('__main__.py', 'audit_export.py', 'pipeline.py', 'local_sources.py',
             'adapters/registry.py', 'adapters/normalize.py')
    runtime_hashes = {name: sha256((runtime_root / name).read_bytes()) for name in names}
    cli = ast.parse((runtime_root / '__main__.py').read_bytes())
    explicit_api_supported = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'add_argument' and any(
            isinstance(arg, ast.Constant) and arg.value == '--audit-api-entry'
            for arg in node.args) for node in ast.walk(cli))
    argv = ['python', '-m', 'app.data_store', 'audit-export', '--root', str(current),
            '--output', str(evidence), '--audit-seconds', '180', '--audit-files', str(files),
            '--audit-rows', str(rows), '--audit-bytes', str(byte_limit),
            '--audit-api-samples', str(len(api_entries)), '--api-base-url', api_base_url,
            '--api-token-env', api_token_env]
    for entry in entries:
        argv += ['--entry', entry['entry_id']]
    for identity in api_entries:
        argv += ['--audit-api-entry', identity]
    return {'format': 'r01-acceptance-recipe@1', 'revision': revision, 'image': image,
            'preparer_sha256': sha256(Path(__file__).read_bytes()),
            'snapshot_sha256': snapshot_sha256, 'registry_sha256': sha256(registry_raw),
            'runtime_source_hashes': runtime_hashes,
            'business_entry_count': 60, 'source_entry_count': 71,
            'business_entries': [by_id[identity] for identity in sorted(by_id)],
            'auxiliary_entries': [entry for entry in entries if entry['disposition'] != 'business'],
            'required_api_entries': list(api_entries),
            'limits': {'seconds': 180, 'files': files, 'rows': rows, 'bytes': byte_limit,
                       'api_samples': len(api_entries)},
            'command_supported_by_bound_runtime': explicit_api_supported,
            'blocking_conditions': [] if explicit_api_supported else ['EXPLICIT_API_SCOPE_UNSUPPORTED'],
            'audit_argv': argv if explicit_api_supported else None,
            'supplier_requests_permitted': 0, 'production_execution_performed': False,
            'business_acceptance_complete': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--snapshot-sha256', required=True)
    parser.add_argument('--runtime-root', type=Path, required=True,
                        help='Private reviewed app/data_store source tree, read only')
    parser.add_argument('--revision', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--current-root', required=True)
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--files', type=int, required=True)
    parser.add_argument('--rows', type=int, required=True)
    parser.add_argument('--bytes', dest='byte_limit', type=int, required=True)
    parser.add_argument('--api-entry', action='append', required=True)
    parser.add_argument('--api-base-url', required=True, help='Actual backend loopback origin')
    parser.add_argument('--api-token-env', required=True, help='Environment variable name, never a token value')
    parser.add_argument('--output', type=Path, required=True)
    args = vars(parser.parse_args(argv))
    snapshot_path, output = args.pop('snapshot'), args.pop('output')
    with snapshot_path.open('rb') as handle:
        raw = handle.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ScopeError('SNAPSHOT_TOO_LARGE')
    args['api_entries'] = args.pop('api_entry')
    manifest = build_manifest(raw, **args)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf8') as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return 0 if manifest['command_supported_by_bound_runtime'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

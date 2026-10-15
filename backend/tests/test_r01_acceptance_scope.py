"""R01 recipe preparation cannot substitute numbered entries for business scope."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('r01_scope', ROOT / 'scripts/r01_acceptance_scope.py')
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)


@pytest.fixture
def inputs(tmp_path):
    runtime = tmp_path / 'runtime'
    (runtime / 'adapters').mkdir(parents=True)
    raw = (ROOT / 'backend/app/data_store/adapters/registry.py').read_bytes()
    (runtime / 'adapters/registry.py').write_bytes(raw)
    for name in ('audit_export.py', 'pipeline.py', 'local_sources.py', 'adapters/normalize.py'):
        (runtime / name).write_text('# synthetic source identity\n')
    (runtime / '__main__.py').write_text("parser.add_argument('--audit-api-entry', action='append')\n")
    entries = scope.registry_entries(raw)
    definitions = [{key: row[key] for key in ('entry_id', 'source', 'native', 'domain')}
                   | {'dataset': 'fixture.' + row['entry_id'], 'schema_id': 'a' * 64,
                      'rule': 'synthetic-v1'} for row in entries if row['disposition'] == 'business']
    snapshot = {'format': 'r01-acceptance-scope@1', 'registry_sha256': scope.sha256(raw),
                'definitions': definitions}
    kwargs = dict(runtime_root=runtime, revision='1' * 40, image='sha256:' + '2' * 64,
                  current_root='/private/current', evidence_dir='/private/evidence/new',
                  files=40000, rows=20000000, byte_limit=8 * 1024**3,
                  api_entries=('E05', 'E61', 'E70'),
                  api_base_url='http://127.0.0.1:8000', api_token_env='TEST_API_TOKEN')
    return snapshot, kwargs


def build(snapshot, kwargs):
    raw = json.dumps(snapshot).encode()
    return scope.build_manifest(raw, snapshot_sha256=hashlib.sha256(raw).hexdigest(), **kwargs)


def test_all_sixty_business_domains_include_later_ids_and_exclude_support(inputs):
    snapshot, kwargs = inputs
    result = build(snapshot, kwargs)
    ids = {row['entry_id'] for row in result['business_entries']}
    assert len(ids) == 60 and {'E61', 'E64', 'E67', 'E68', 'E69', 'E70'} <= ids
    assert not {'E01', 'E58', 'E60', 'E71'} & ids
    assert result['source_entry_count'] == 71
    assert result['limits']['seconds'] == 180
    assert result['runtime_source_hashes']['adapters/registry.py'] == snapshot['registry_sha256']
    assert not result['production_execution_performed'] and not result['business_acceptance_complete']
    assert result['supplier_requests_permitted'] == 0
    assert result['audit_argv'][result['audit_argv'].index('--api-token-env') + 1] == 'TEST_API_TOKEN'


def test_first_sixty_numbered_entries_are_refused_even_with_equal_count(inputs):
    snapshot, kwargs = inputs
    snapshot['definitions'] = [{**snapshot['definitions'][0], 'entry_id': f'E{i:02d}'}
                               for i in range(1, 61)]
    with pytest.raises(scope.ScopeError, match='R01_BUSINESS_SCOPE_MISMATCH'):
        build(snapshot, kwargs)


@pytest.mark.parametrize('mutation', ('duplicate', 'native', 'registry', 'dataset'))
def test_scope_and_runtime_contract_drift_are_refused(inputs, mutation):
    snapshot, kwargs = inputs
    if mutation == 'duplicate':
        snapshot['definitions'][-1] = dict(snapshot['definitions'][0])
    elif mutation == 'native':
        snapshot['definitions'][0]['native'] = 'another_source'
    elif mutation == 'registry':
        snapshot['registry_sha256'] = '0' * 64
    else:
        snapshot['definitions'][1]['dataset'] = snapshot['definitions'][0]['dataset']
    with pytest.raises(scope.ScopeError):
        build(snapshot, kwargs)


def test_changed_private_snapshot_is_refused_before_manifest_generation(inputs):
    _, kwargs = inputs
    with pytest.raises(scope.ScopeError, match='SNAPSHOT_HASH_MISMATCH'):
        scope.build_manifest(b'{}', snapshot_sha256='0' * 64, **kwargs)


@pytest.mark.parametrize('api_entries', (('E01',), ('E05', 'E05'), ('E99',)))
def test_required_api_members_are_exact_unique_business_entries(inputs, api_entries):
    snapshot, kwargs = inputs
    kwargs['api_entries'] = api_entries
    with pytest.raises(scope.ScopeError, match='EXACT_BUSINESS_API_ENTRIES_REQUIRED'):
        build(snapshot, kwargs)


def test_runtime_without_explicit_api_selection_is_blocked(inputs):
    snapshot, kwargs = inputs
    (kwargs['runtime_root'] / '__main__.py').write_text("# '--audit-api-entry' is only a comment\n")
    result = build(snapshot, kwargs)
    assert result['audit_argv'] is None
    assert result['blocking_conditions'] == ['EXPLICIT_API_SCOPE_UNSUPPORTED']


@pytest.mark.parametrize('evidence', ('/private/current', '/private/current/nested', 'relative'))
def test_recipe_cannot_write_audit_evidence_into_current_data(inputs, evidence):
    snapshot, kwargs = inputs
    kwargs['evidence_dir'] = evidence
    with pytest.raises(scope.ScopeError):
        build(snapshot, kwargs)


@pytest.mark.parametrize('url', ('https://external.invalid', 'http://token@localhost',
                               'http://localhost?token=private', 'http://localhost/api'))
def test_recipe_never_embeds_credentials_or_targets_a_remote_api(inputs, url):
    snapshot, kwargs = inputs
    kwargs['api_base_url'] = url
    with pytest.raises(scope.ScopeError, match='LOOPBACK_API_AND_TOKEN_ENV_NAME_REQUIRED'):
        build(snapshot, kwargs)

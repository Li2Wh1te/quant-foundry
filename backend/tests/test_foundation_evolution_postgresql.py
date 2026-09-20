"""Executable extension fixtures; no additional production contracts are enabled.

The fixture installs explicit reader declarations, registers immutable contracts,
then executes the ordinary normalization/governance/publish/read kernel. A 2.0
contract removes an optional field (a breaking reader change), while the physical
storage remains a compatible superset. This tests version isolation for both
business structures without pretending a second live provider was verified.
"""
import json
from copy import deepcopy
import pytest
from app.data_foundation.catalog import register_definition
from app.data_foundation.models import Definition
from app.data_foundation.projection import PROJECTIONS
from app.data_foundation.work import create_work, claim
from app.data_foundation.bars import normalize_batch
from app.data_foundation.publication import stage_decisions, publish
from app.data_foundation.canonical import FoundationError
from app.data_foundation.service import PageRequest
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_daily_postgresql import setup_daily, release_daily
from tests.test_foundation_holdings_postgresql import setup_reports, release_reports, service, request


def evolve(session, fixture, version, monkeypatch, *, report):
    origin, execution, policy = fixture[:3]
    initial = session.get(Definition, origin.contract_id)
    definition = json.loads(initial.definition_json)
    spec = deepcopy(PROJECTIONS[(initial.name, '1.0')])
    spec['version'] += '-fixture-' + version
    if version == '2.0':
        removed = 'period_change_ratio' if report else 'volume'
        definition['optional_fields'].pop(removed)
        spec['fields'].remove(removed)
    else:
        # The additive reader can read 1.0, but it must report the new optional
        # field unavailable instead of filling historical objects retroactively.
        spec['readable_versions'] = ['1.0', version]
        added = 'holding_quantity' if report else 'trade_count'
        definition['optional_fields'][added] = {'type': 'decimal', 'unit': 'shares' if report else 'trades'}
        spec['fields'].append(added)
        spec['unavailable_fields'] = [added]
    contract = register_definition(session, kind='contract', name=initial.name, version=version, definition=definition)
    monkeypatch.setitem(PROJECTIONS, (initial.name, version), spec)
    major = int(version.split('.')[0])
    p = register_definition(session, kind='policy', name=policy.name + '-fixture', version=version,
        definition=json.loads(policy.definition_json) | {'major': major})
    work = create_work(session, kind='A', contract_id=contract.id, execution_id=execution.id,
        dependency_id=origin.dependency_id, source_ref_id=origin.source_ref_id,
        parameters=json.loads(origin.parameters_json) | {'major': major})
    while work.status != 'succeeded':
        lease = claim(session, work_id=work.id)
        normalize_batch(session, work.id, lease.lease_epoch)
    return (work, execution, p, *fixture[3:])


@pytest.mark.parametrize('report', [False, True], ids=['daily', 'holdings'])
def test_major_heads_and_compatible_readers_execute_independently(session, monkeypatch, report):
    from app.data_foundation.query import DataRequirement
    from app.data_foundation.work_models import Head
    fixture = setup_reports(session) if report else setup_daily(session)
    first = release_reports(session, fixture) if report else release_daily(session, fixture)
    if report:
        req = request(fixture, first)
    else:
        req = DataRequirement(dataset_id='market.bar.daily', contract_version='1.0', profile_id='default',
            semantic_series_id=json.loads(fixture[0].parameters_json)['series'], subjects=[fixture[3]],
            business_range={'from': '2026-06-05', 'to': '2026-06-08'}, fields=['close'], release=first.id)
    svc = service(session)
    old = json.loads(svc.query_official(req))
    token = json.loads(svc.check_capability(req))['resolution_token']
    minor = evolve(session, fixture, '1.1', monkeypatch, report=report)
    # 1.1 can read immutable 1.0 with precisely the installed projection.
    compatible = json.loads(svc.query_official(req.model_copy(update={'contract_version': '1.1'})))
    assert compatible['items'] == old['items']
    added = 'holding_quantity' if report else 'trade_count'
    missing = json.loads(svc.query_official(req.model_copy(update={'contract_version': '1.1', 'fields': [added]})))
    assert missing['state'] == 'unavailable' and missing['items'] == []
    if report:
        incremental = release_reports(session, minor, first)
    else:
        from app.data_foundation.governance import create_governance
        work = create_governance(session, normalization_id=minor[0].id, execution_id=minor[1].id, policy_id=minor[2].id,
            parent_release_id=first.id, expected_head_revision=1)
        lease = claim(session, work_id=work.id)
        incremental = stage_decisions(session, work.id, lease.lease_epoch)
        publish(session, incremental.id, lease.lease_epoch)
    assert json.loads(svc.query_official(req.model_copy(update={'contract_version': '1.1', 'release': 'latest'})))['release_id'] == str(incremental.id)
    with pytest.raises(FoundationError):
        svc.query_official(req.model_copy(update={'release': 'latest'}))
    newer = evolve(session, fixture, '2.0', monkeypatch, report=report)
    second = release_reports(session, newer) if report else release_daily(session, newer)
    assert session.get(Head, fixture[0].scope_key).release_id == incremental.id
    assert session.get(Head, newer[0].scope_key).release_id == second.id
    current = req.model_copy(update={'contract_version': '2.0', 'release': 'latest'})
    assert json.loads(svc.query_official(current))['release_id'] == str(second.id)
    assert json.loads(svc.query_official(PageRequest(resolution_token=token)))['items'] == old['items']
    for version, release in [('1.0', second.id), ('2.0', first.id)]:
        with pytest.raises(FoundationError) as error:
            svc.query_official(req.model_copy(update={'contract_version': version, 'release': release}))
        assert error.value.code == 'CONTRACT_RELEASE_MISMATCH'
    removed = 'period_change_ratio' if report else 'volume'
    with pytest.raises(FoundationError):
        svc.query_official(current.model_copy(update={'fields': [removed]}))
    # A snapshot may contain both major heads, each independently pinned.
    snapshot = json.loads(svc.resolve_snapshot([req, current]))
    fixed = json.loads(svc.read_snapshot(snapshot))
    assert [r['release_id'] for r in fixed['items']] == [str(first.id), str(second.id)]
    assert len({e['projection_hash'] for e in snapshot['entries']}) == 2

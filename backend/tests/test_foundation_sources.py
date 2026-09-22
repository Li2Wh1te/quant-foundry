"""Source sealing tests; PostgreSQL-only constraints have separate coverage."""
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
import app.models
from app.db.base import Base
from app.data_foundation.canonical import digest, encode, FoundationError
from app.data_foundation.catalog import register_definition, register_execution, register_dependencies
from app.data_foundation.models import Baseline, Binding, SourceRef
from app.data_foundation.source_refs import register_baseline, register_observation, read_source
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from app.data_ingestion.tonghuashun.contracts import content_hash, exact_json


def execution(session):
    manifest = dict(schema_version=1, git_commit='a'*40, runtime_image_digest=None,
        python_version='3.12.2', dependency_lock_hash='b'*64,
        parser=dict(key='local-source', version='1', hash='c'*64),
        transform=dict(key='none', version='1', hash='d'*64),
        quality=dict(key='hash', version='1', hash='e'*64), config={}, decoder_refs=[],
        archive=dict(key=None, sha256=None, verified=False))
    return register_execution(session, manifest, b'fixed decoder evidence')


@pytest.fixture
def session():
    engine = create_engine('sqlite://')
    @event.listens_for(engine, 'connect')
    def setup(dbapi, _):
        dbapi.execute('PRAGMA foreign_keys=ON')
        dbapi.create_function('btrim', 1, lambda s: s.strip())
    tables = [Base.metadata.tables['instruments'], Observation.__table__]
    from app.data_foundation import models as source_models
    tables += [v.__table__ for v in vars(source_models).values() if isinstance(v, type) and hasattr(v, '__table__')]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_canonical_decimal_context_and_domain():
    value = Decimal('123456789012345678901234567890.12000')
    with localcontext() as context:
        context.prec = 6
        assert encode(value) == '"123456789012345678901234567890.12"'
    assert encode(Decimal('-0')) == '"0"'
    assert digest('a', {'x': 1}) != digest('b', {'x': 1})
    with pytest.raises(ValueError): encode(float('nan'))
    with pytest.raises(ValueError): encode(datetime(2026, 1, 1))


def test_version_conflict_cannot_replace_definition(session):
    fields = dict(schema_version=1, currency='CNY', interval='1d', session_scope='exchange', price_basis='unadjusted', method_status='not_applicable', anchor_status='not_applicable')
    a = register_definition(session, kind='series', name='test', version='1', definition=fields)
    assert register_definition(session, kind='series', name='test', version='1', definition=fields).id == a.id
    with pytest.raises(FoundationError, match='不能变更'):
        register_definition(session, kind='series', name='test', version='1', definition={**fields, 'currency': 'USD'})


def baseline(session, ex, event_key, rows):
    return register_baseline(session, source='tushare', dataset='etf_daily', scope={'code': 'x'}, rows=rows,
        observed_at=datetime(2026, 9, 16, tzinfo=timezone.utc), decoder_id=ex.id, event_key=event_key)


def test_complete_baseline_event_identity_and_retry(session):
    ex = execution(session)
    a = baseline(session, ex, 'a', [{'close': Decimal('1.120000'), 'source_revision': None}])
    assert read_source(session, a.id) == [{'close': '1.12', 'source_revision': None}]
    assert baseline(session, ex, 'a', [{'close': Decimal('1.12'), 'source_revision': None}]).id == a.id
    b = baseline(session, ex, 'b', [{'close': Decimal('2')}])
    c = baseline(session, ex, 'c', [{'close': Decimal('1.12'), 'source_revision': None}])
    assert a.content_hash == c.content_hash and len({a.id, b.id, c.id}) == 3
    with pytest.raises(FoundationError): baseline(session, ex, 'a', [{'close': Decimal('9')}])
    dep = register_dependencies(session, [{'source_ref_id': a.id, 'purpose': 'input'}, {'execution_id': ex.id, 'purpose': 'decoder'}])
    assert dep.id == register_dependencies(session, [{'source_ref_id': a.id, 'purpose': 'input'}, {'execution_id': ex.id, 'purpose': 'decoder'}]).id
    assert ex.replay_status == 'dependency_missing'


def observations(session):
    data = {'item': [{'date_ms': 1, 'close': Decimal('1.2')}]}
    anchor = Observation(id=uuid4(), dataset='etf_daily', subject='159001.SZ', variant='daily', observed_at=datetime.now(timezone.utc),
        request_json='{}', data_json=exact_json(data), content_hash=content_hash(data), row_count=1, chain_depth=0)
    session.add(anchor); session.flush()
    updated = {'item': [{'date_ms': 1, 'close': Decimal('2.3')}]}
    delta = Observation(id=uuid4(), dataset='etf_daily', subject='159001.SZ', variant='daily', observed_at=datetime.now(timezone.utc),
        request_json='{}', data_json=exact_json(dict(key_field='date_ms', removed=[], upserts=updated['item'], metadata={})),
        content_hash=content_hash(updated), row_count=1, chain_depth=1, base_observation_id=anchor.id)
    session.add(delta); session.flush()
    return anchor, delta


def test_fixed_delta_chain_and_scope_validation(session):
    ex = execution(session)
    anchor, delta = observations(session)
    ref = register_observation(session, delta.id, ex.id)
    assert read_source(session, ref.id)['item'][0]['close'] == Decimal('2.3')
    assert register_observation(session, delta.id, ex.id).id == ref.id
    delta.subject = 'other'
    session.flush()
    with pytest.raises(FoundationError): register_observation(session, delta.id, ex.id)


def test_sealing_checks_corrupt_ancestor_even_when_later_upsert_hides_it(session):
    ex = execution(session)
    anchor, delta = observations(session)
    # The newest version replaces this row completely and still reconstructs
    # to its expected hash. Its referenced historical version is nevertheless
    # corrupt, so sealing only the newest hash would be insufficient.
    anchor.data_json = exact_json({'item': [{'date_ms': 1, 'close': Decimal('99')}]})
    session.flush()
    from app.data_ingestion.tonghuashun.repository import materialize
    assert materialize(session, delta)['item'][0]['close'] == Decimal('2.3')
    with pytest.raises(FoundationError, match='依赖版本内容校验失败'):
        register_observation(session, delta.id, ex.id)
    assert session.scalar(select(SourceRef)) is None


def test_sealing_invalid_json_returns_isolatable_source_error(session):
    ex = execution(session)
    anchor, _ = observations(session)
    anchor.data_json = '{malformed'
    session.flush()
    with pytest.raises(FoundationError, match='编码或增量结构无效'):
        register_observation(session, anchor.id, ex.id)


def test_identity_reuses_uuid_and_requires_range_evidence(session):
    from app.instruments.models import Instrument
    from app.data_foundation.identity import register_binding, resolve_binding
    instrument_id = uuid4()
    session.add(Instrument(id=instrument_id, asset_class='etf'))
    session.flush()
    ex = execution(session)
    ref = register_baseline(session, source='tushare', dataset='etf_directory', scope={},
        rows=[{'source': 'tushare', 'ts_code': '510300.SH', 'etf_id': str(instrument_id)}],
        observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key='directory')
    args = dict(source_ref_id=ref.id, subject='510300.SH', instrument_id=instrument_id,
        valid_from=date(2026,6,5), valid_to=date(2026,8,29), binding_version='1')
    binding = register_binding(session, **args)
    assert binding.instrument_id == instrument_id and binding.status == 'unresolved'
    with pytest.raises(FoundationError): resolve_binding(session, [binding.id], 'tushare', '510300.SH', date(2026,7,1))
    with pytest.raises(FoundationError): register_binding(session, **{**args, 'status': 'resolved'})
    with pytest.raises(FoundationError): register_binding(session, **{**args, 'valid_from': date(2026,7,1)})
    confirmed = register_binding(session, **{**args, 'binding_version': '2', 'status': 'resolved',
        'evidence': {'reviewer': 'fixture-review', 'reference': 'fixture-evidence', 'valid_from': '2026-06-05', 'valid_to': '2026-08-29'}})
    assert resolve_binding(session, [confirmed.id], 'tushare', '510300.SH', date(2026,7,1)).instrument_id == instrument_id
    with pytest.raises(FoundationError): resolve_binding(session, [confirmed.id], 'tushare', '510300.SH', date(2026,8,29))
    assert len(session.scalars(select(Instrument)).all()) == 1


def test_changed_capture_has_no_partial_registration(session):
    from app.data_foundation.baselines import apply_s1
    ex = execution(session)
    with pytest.raises(FoundationError):
        apply_s1(session, {'content_hash': 'a'*64, 'scope': {}, 'content': {}},
            expected_hash='b'*64, decoder_id=ex.id, event_key='test')
    assert session.scalar(select(SourceRef)) is None


def test_catalog_registers_without_query_capability(session):
    from app.data_foundation.contracts import register_initial_catalog
    rows = register_initial_catalog(session)
    assert len(rows) == 4
    assert [r.id for r in rows] == [r.id for r in register_initial_catalog(session)]
    assert json.loads(rows[-1].definition_json)['read_status'] == 'not_implemented'

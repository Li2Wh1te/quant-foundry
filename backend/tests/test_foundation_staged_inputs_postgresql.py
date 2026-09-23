"""Frozen staging survives importer deletion and cannot replace a newer head."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4
import json
import pytest
from sqlalchemy import select, delete
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session, fixture, isolated_series
from app.data_ingestion.models.tonghuashun import TonghuashunDumpImport as Import, TonghuashunDumpStage as Stage, TonghuashunWorkUnit as Unit
from app.data_foundation.staged_inputs import freeze_dump, freeze_request_units, require_space, verify_runtime
from app.data_foundation.source_refs import read_source
from app.data_foundation.models import SourceRef
from app.data_foundation.record_adapters import rows_for, convert
from app.data_foundation.full_formalization import advance_source
from app.data_foundation.work_models import Head, Release
from app.data_foundation.canonical import FoundationError


def staged(session):
    stamp = datetime.now(timezone.utc); generation = uuid4(); dataset = 'stock_actions_dump'
    # Each test owns uncommitted staging in the disposable pipeline database.
    slot = Import(dataset=dataset,generation=generation,status='ready',started_at=stamp,
        lease_until=stamp+timedelta(hours=1),digest='a'*64,metadata_json=json.dumps(dict(
        sha256='a'*64,observed_start='2026-09-15',observed_end='2026-09-15')),
        total_subjects=1,imported_subjects=0,superseded_subjects=0)
    session.add(slot)
    session.flush()
    session.add(Stage(dataset=dataset,subject='000001.SZ',data_json=json.dumps(dict(item=[
        dict(ticker='000001',ex_date_ms=1789401600000,dividend_per_share='0.125000001',per_share_bonus='-0.1')]))))
    session.flush()
    return slot


def test_freeze_is_idempotent_after_importer_consumes_generation(session, tmp_path):
    (_, _, execution), image = fixture(session, tmp_path)
    verify_runtime(session, execution, image, tmp_path)
    slot = staged(session); generation = slot.generation
    first = freeze_dump(session,dataset=slot.dataset,generation=generation,execution_id=execution)
    assert first['staged_subjects'] == 1
    ref = session.get(SourceRef, first['source_ref_ids'][0]); raw = read_source(session,ref.id)
    body = convert(ref,rows_for(ref,raw)[0])['body']
    assert body['source_code'] == '000001.SZ'
    assert body['events'][0]['reported_dividend_per_share'] == '0.125000001'
    assert body['events'][0]['reported_per_share_bonus'] == '-0.1'
    session.execute(delete(Stage).where(Stage.dataset==slot.dataset))
    slot.generation = uuid4(); slot.status = 'completed'; slot.imported_subjects = 1
    session.flush()
    again = freeze_dump(session,dataset=slot.dataset,generation=generation,execution_id=execution)
    assert again['source_ref_ids'] == first['source_ref_ids']
    assert read_source(session,ref.id) == raw


def test_generation_or_denominator_mismatch_never_freezes_a_partial_scope(session, tmp_path):
    (_, _, execution), _ = fixture(session,tmp_path)
    slot = staged(session)
    with pytest.raises(FoundationError):freeze_dump(session,dataset=slot.dataset,generation=uuid4(),execution_id=execution)
    slot.total_subjects=2; session.flush()
    with pytest.raises(FoundationError,match='分母'):freeze_dump(session,dataset=slot.dataset,generation=slot.generation,execution_id=execution)


def test_opaque_request_unit_is_preserved_without_guessing_business_identity(session, tmp_path):
    (_, _, execution), _ = fixture(session,tmp_path)
    unit = Unit(scope='a'*64,request_key='b'*64,data_json='{"item":[{"amount":"1.23"}]}',created_at=datetime.now(timezone.utc))
    session.add(unit); session.flush()
    result = freeze_request_units(session,execution_id=execution)
    assert result['reason_code']=='SOURCE_IDENTITY_UNRESOLVED' and result['fixed_units']==1
    body = read_source(session,result['source_ref_ids'][0])
    assert body[0]['response']['item'][0]['amount']=='1.23'
    assert session.get(Unit,('a'*64,'b'*64)) is not None


def test_disk_pressure_stops_writes_and_missing_runtime_is_not_bypassed(session, tmp_path, monkeypatch):
    (_, _, execution), image = fixture(session,tmp_path)
    import app.data_foundation.staged_inputs as module
    monkeypatch.setattr(module.shutil,'disk_usage',lambda path:SimpleNamespace(free=1))
    with pytest.raises(FoundationError,match='磁盘'):require_space(tmp_path,2)
    with pytest.raises(FoundationError):verify_runtime(session,execution,'sha256:'+'0'*64,tmp_path)


def test_changed_response_under_same_request_key_retains_both_versions(session, tmp_path):
    (_, _, execution), _ = fixture(session, tmp_path)
    unit = Unit(scope='c'*64, request_key='d'*64,
                data_json='{"item":[{"amount":"1.23"}]}', created_at=datetime.now(timezone.utc))
    session.add(unit)
    session.flush()
    first = freeze_request_units(session, execution_id=execution)
    unit.data_json = '{"item":[{"amount":"4.56"}]}'
    session.flush()
    second = freeze_request_units(session, execution_id=execution)
    assert first['source_ref_ids'] != second['source_ref_ids']
    assert read_source(session, first['source_ref_ids'][0])[0]['response']['item'][0]['amount'] == '1.23'
    assert read_source(session, second['source_ref_ids'][0])[0]['response']['item'][0]['amount'] == '4.56'
    assert freeze_request_units(session, execution_id=execution)['source_ref_ids'] == second['source_ref_ids']


def test_write_guard_preserves_mutable_staging_without_partial_manifest(session, tmp_path):
    (_, _, execution), _ = fixture(session, tmp_path)
    slot = staged(session)
    before = set(session.scalars(select(SourceRef.id)))

    def exhausted():
        raise FoundationError('LOCAL_DISK_PRESSURE', '磁盘余量不足')

    with pytest.raises(FoundationError, match='磁盘'):
        freeze_dump(session, dataset=slot.dataset, generation=slot.generation,
                    execution_id=execution, before_write=exhausted)
    assert set(session.scalars(select(SourceRef.id))) == before
    assert session.get(Stage, (slot.dataset, '000001.SZ')) is not None


def test_staged_publication_preserves_current_head(session, tmp_path):
    (_, _, execution), image = fixture(session,tmp_path)
    slot=staged(session)
    frozen=freeze_dump(session,dataset=slot.dataset,generation=slot.generation,execution_id=execution)
    source=frozen['source_ref_ids'][0]
    # Remove the mutable staging rows before the worker uses another session;
    # publication must depend exclusively on the immutable frozen source.
    session.execute(delete(Stage).where(Stage.dataset==slot.dataset))
    session.delete(slot)
    session.commit()
    result=advance_source(session.bind,source,execution_id=execution,runtime_digest=image,
        archive_root=tmp_path,steps=100,publish=True,preserve_head=True)
    assert result['status']=='published' and result['head_activated'] is False
    release=session.get(Release,result['release_id'])
    head=session.get(Head,release.scope_key)
    assert head is None or head.release_id != release.id

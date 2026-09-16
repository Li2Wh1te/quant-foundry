"""D05/D06/D08/D09/D10/D17: enforce source integrity in PostgreSQL 17."""
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select, text, update, delete
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.data_foundation.models import BaselineBlock, Definition, SourceRef
from app.data_foundation.source_refs import register_observation, register_baseline, read_source
from app.data_foundation.canonical import FoundationError
from tests.test_foundation_sources import execution, observations

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='requires disposable PostgreSQL')


@pytest.fixture
def engine():
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


def test_referenced_anchor_and_delta_cannot_change_or_disappear(engine):
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
    with Session(engine) as session:
        ex = execution(session)
        anchor, delta = observations(session)
        ref = register_observation(session, delta.id, ex.id)
        assert read_source(session, ref.id)['item'][0]['close'] == Decimal('2.3')
        for obs in (anchor, delta):
            for statement in (update(Observation).where(Observation.id == obs.id).values(data_json='{}'), delete(Observation).where(Observation.id == obs.id)):
                with pytest.raises(DBAPIError):
                    with session.begin_nested(): session.execute(statement)
        session.rollback()


def test_baseline_seal_protects_payload_and_rejects_late_blocks(engine):
    with Session(engine) as session:
        ex = execution(session)
        ref = register_baseline(session, source='tushare', dataset='test', scope={}, rows=[{'price': Decimal('0.1234567890123456789')}],
            observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key=uuid4().hex)
        assert read_source(session, ref.id) == [{'price': '0.1234567890123456789'}]
        with pytest.raises(DBAPIError):
            with session.begin_nested(): session.execute(update(BaselineBlock).where(BaselineBlock.baseline_id == ref.baseline_id).values(payload_json='[]'))
        with pytest.raises(DBAPIError):
            with session.begin_nested():
                session.add(BaselineBlock(baseline_id=ref.baseline_id, ordinal=1, row_count=0, payload_json='[]', content_hash='a'*64))
                session.flush()
        session.rollback()


def test_observation_registration_serializes_against_concurrent_update(engine):
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
    with Session(engine) as setup:
        ex = execution(setup)
        anchor, delta = observations(setup)
        exid, aid, did = ex.id, anchor.id, delta.id
        setup.commit()
    # Committed fixture evidence intentionally remains in this disposable DB:
    # evidence tables are append-only, including during test cleanup.
    with Session(engine) as reader, ThreadPoolExecutor(max_workers=1) as pool:
        register_observation(reader, did, exid)
        def mutate():
            with Session(engine) as writer:
                writer.execute(text("SET LOCAL lock_timeout = '5s'"))
                try:
                    writer.execute(update(Observation).where(Observation.id == aid).values(row_count=99))
                    writer.commit()
                    return 'changed'
                except DBAPIError:
                    writer.rollback()
                    return 'protected'
        future = pool.submit(mutate)
        try:
            with pytest.raises(TimeoutError): future.result(timeout=.2)
            reader.commit()
        finally:
            reader.rollback()
        assert future.result(timeout=6) == 'protected'

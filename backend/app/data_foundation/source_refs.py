"""Seal source events, preserving occurrence identity independently of value hash."""
import json
from decimal import Decimal
from uuid import UUID
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.models import Baseline, BaselineBlock, Execution, SourceRef, ObservationDependency
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from app.data_ingestion.tonghuashun.repository import materialize
from app.data_ingestion.tonghuashun.contracts import content_hash


def validate_chain(chain):
    """Verify every fixed version with one forward reconstruction per level.

    Reconstructing each ancestor independently has quadratic cost in chain
    depth, amplified by years of full-history rows. Carry only the current
    materialization and verify each intermediate hash before applying the next
    delta. This retains the old all-dependency integrity guarantee, including
    detecting an invalid ancestor whose corruption is hidden by a later upsert.
    """
    data = None
    try:
        for index, item in enumerate(reversed(chain)):
            if item.chain_depth != index:
                raise FoundationError('SOURCE_INVALID', '同花顺版本链深度与实际依赖不一致。')
            payload = json.loads(item.data_json, parse_float=Decimal)
            if index == 0:
                data = payload
            else:
                key = payload['key_field']
                rows = {row[key]: row for row in data['item']}
                for identity in payload['removed']:
                    rows.pop(identity, None)
                rows.update({row[key]: row for row in payload['upserts']})
                data = {**payload['metadata'], 'item': [rows[k] for k in sorted(rows)]}
            if content_hash(data) != item.content_hash:
                raise FoundationError('SOURCE_INVALID', '同花顺固定版本或依赖版本内容校验失败。')
    except FoundationError:
        raise
    except (ValueError, KeyError, TypeError, IndexError):
        raise FoundationError('SOURCE_INVALID', '同花顺固定版本编码或增量结构无效，未登记来源。') from None


def register_baseline(session, *, source, dataset, scope, rows, observed_at, decoder_id, event_key):
    if session.get(Execution, decoder_id) is None:
        raise FoundationError('DEPENDENCY_MISSING', '来源解码依赖尚未登记。')
    payload = encode(rows)
    content_hash = digest('baseline-rows', rows)
    lock_key(session, 'baseline-event', [source, dataset, event_key])
    event = session.scalar(select(Baseline).where(Baseline.source == source, Baseline.dataset == dataset, Baseline.event_key == event_key))
    if event:
        if event.content_hash != content_hash or event.scope_json != encode(scope):
            raise FoundationError('SOURCE_CHANGED', '同一基线观察事件的内容不能变更。')
        observed_at = event.observed_at
    manifest_hash = event.manifest_hash if event else digest('baseline-manifest', dict(source=source, dataset=dataset, scope=scope,
        observed_at=observed_at, content_hash=content_hash, event_key=event_key))
    locator = digest('baseline-locator', {'manifest_hash': manifest_hash, 'decoder_id': decoder_id})
    lock_key(session, 'baseline', manifest_hash)
    old = session.scalar(select(SourceRef).where(SourceRef.source == source, SourceRef.representation == 'local_table_baseline', SourceRef.locator_hash == locator))
    if old:
        read_source(session, old.id)
        return old
    baseline = session.scalar(select(Baseline).where(Baseline.manifest_hash == manifest_hash))
    if baseline is None:
        baseline = Baseline(event_key=event_key, manifest_hash=manifest_hash, source=source, dataset=dataset, scope_json=encode(scope),
            observed_at=observed_at, row_count=len(rows), content_hash=content_hash, created_at=now())
        session.add(baseline)
        session.flush()
        session.add(BaselineBlock(baseline_id=baseline.id, ordinal=0, payload_json=payload, content_hash=content_hash, row_count=len(rows)))
        session.flush()
    ref = SourceRef(source=source, dataset=dataset, subject='scope', variant='baseline-v1', representation='local_table_baseline',
        locator_hash=locator, content_hash=content_hash, observed_at=observed_at, baseline_id=baseline.id, decoder_id=decoder_id, created_at=now())
    session.add(ref)
    session.flush()
    read_source(session, ref.id)
    return ref


def register_observation(session, observation_id: UUID, decoder_id: UUID):
    if session.get(Execution, decoder_id) is None:
        raise FoundationError('DEPENDENCY_MISSING', '来源解码依赖尚未登记。')
    lock_key(session, 'observation-ref', str(observation_id))
    # FOR SHARE blocks every UPDATE, not just key changes, until dependency
    # insertion commits. The migration trigger then protects all future writes.
    chain, seen, cursor_id = [], set(), observation_id
    while cursor_id is not None:
        if cursor_id in seen or len(chain) >= 31:
            raise FoundationError('SOURCE_INVALID', '同花顺版本依赖链循环或超出限制。')
        seen.add(cursor_id)
        cursor = session.scalar(select(Observation).where(Observation.id == cursor_id).with_for_update(read=True).execution_options(populate_existing=True))
        if cursor is None:
            raise FoundationError('SOURCE_UNAVAILABLE', '同花顺固定版本或基础版本不存在。')
        if chain and (cursor.dataset, cursor.subject, cursor.variant) != (chain[0].dataset, chain[0].subject, chain[0].variant):
            raise FoundationError('SOURCE_INVALID', '同花顺基础版本的数据范围不一致。')
        chain.append(cursor)
        cursor_id = cursor.base_observation_id
    validate_chain(chain)
    root = chain[0]
    locator = digest('observation-locator', {'id': root.id, 'decoder_id': decoder_id, 'dataset': root.dataset, 'subject': root.subject, 'variant': root.variant})
    old = session.scalar(select(SourceRef).where(SourceRef.source == 'tonghuashun', SourceRef.representation == 'ths_observation', SourceRef.locator_hash == locator))
    if old:
        if old.content_hash != root.content_hash:
            raise FoundationError('SOURCE_MUTATED', '固定来源定位的内容已改变。')
        return old
    ref = SourceRef(source='tonghuashun', dataset=root.dataset, subject=root.subject, variant=root.variant,
        representation='ths_observation', locator_hash=locator, content_hash=root.content_hash,
        observed_at=root.observed_at, observation_id=root.id, decoder_id=decoder_id, created_at=now())
    session.add(ref)
    session.flush()
    session.add_all([ObservationDependency(source_ref_id=ref.id, observation_id=item.id) for item in chain])
    session.flush()
    return ref


def read_source(session, ref_id):
    ref = session.get(SourceRef, ref_id)
    if ref is None:
        raise FoundationError('SOURCE_UNAVAILABLE', '固定来源引用不存在。')
    if ref.observation_id:
        observation = session.get(Observation, ref.observation_id)
        if observation.content_hash != ref.content_hash:
            raise FoundationError('SOURCE_MUTATED', '固定来源内容摘要不一致。')
        return materialize(session, observation)
    baseline = session.get(Baseline, ref.baseline_id)
    rows = []
    blocks = session.scalars(select(BaselineBlock).where(BaselineBlock.baseline_id == baseline.id).order_by(BaselineBlock.ordinal)).all()
    for index, block in enumerate(blocks):
        data = json.loads(block.payload_json)
        if block.ordinal != index or len(data) != block.row_count or digest('baseline-rows', data) != block.content_hash:
            raise FoundationError('SOURCE_MUTATED', '来源基线块校验失败。')
        rows.extend(data)
    if len(rows) != baseline.row_count or digest('baseline-rows', rows) != baseline.content_hash or baseline.content_hash != ref.content_hash:
        raise FoundationError('SOURCE_MUTATED', '来源基线完整性校验失败。')
    return rows

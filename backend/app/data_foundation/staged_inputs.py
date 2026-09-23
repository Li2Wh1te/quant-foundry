"""Freeze already-downloaded local inputs without fetching or merging suppliers.

Dump staging is mutable and its ordinary importer deletes rows after merging.
Freeze each exact staged subject under the import-generation lock first. Its
formal publication preserves the current head: older downloaded values cannot
overwrite a newer REST observation. Opaque request-cache units are preserved as
unresolved evidence because hashed keys do not prove dataset/subject identity.
"""
import json
import shutil
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select, func
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.source_refs import register_baseline, read_source
from app.data_foundation.models import Baseline, SourceRef
from app.data_foundation.execution import verify_execution
from app.data_ingestion.models.tonghuashun import TonghuashunDumpImport as Import, TonghuashunDumpStage as Stage, TonghuashunWorkUnit as Unit
from app.data_ingestion.tonghuashun.dump_service import DUMPS

FORMAT = 'local-staged-dump-v1'


def require_space(path, minimum_bytes=10 * 1024**3):
    if type(minimum_bytes) is not int or minimum_bytes < 0:
        raise ValueError('Minimum free space must be a non-negative byte count')
    if shutil.disk_usage(Path(path)).free < minimum_bytes:
        raise FoundationError('LOCAL_DISK_PRESSURE', '本地磁盘余量不足，暂存固定与全量写入未继续；已有检查点保留。')


def verify_runtime(session, execution_id, runtime_digest, archive_root):
    """Apply the same runtime proof before any staging snapshot is written."""
    from app.data_foundation.record_work import DOMAIN_KEY, domain_hash
    verify_execution(session, SimpleNamespace(execution_id=execution_id,
        parameters_json=encode(dict(domain=DOMAIN_KEY, domain_hash=domain_hash()))), runtime_digest, archive_root)


def freeze_dump(session, *, dataset, generation, execution_id, before_write=None):
    """The caller commits the complete generation snapshot before processing."""
    if dataset not in DUMPS:
        raise ValueError('Unsupported local dump channel')
    event = digest('fixed-stage-generation-v1', [dataset, generation])
    def saved():
        existing = session.scalar(select(SourceRef).join(Baseline, Baseline.id == SourceRef.baseline_id).where(
            Baseline.source == 'tonghuashun', Baseline.dataset == 'staged_import_manifest', Baseline.event_key == event))
        if existing is None:
            return None
        result = read_source(session, existing.id)[0]
        return dict(result, generation=UUID(result['generation']),
                    source_ref_ids=[UUID(value) for value in result['source_ref_ids']], manifest_ref_id=existing.id)
    previous = saved()
    if previous is not None:
        return previous
    slot = session.scalar(select(Import).where(Import.dataset == dataset).with_for_update())
    # Another freezer may have committed while this transaction waited on the
    # importer lock. Reuse its sealed denominator even if import has advanced.
    previous = saved()
    if previous is not None:
        return previous
    if slot is None or slot.generation != generation:
        raise FoundationError('SOURCE_CONTEXT_CHANGED', '暂存导入代次已变化，不能替换本次固定范围。')
    if slot.status not in ('ready', 'completed'):
        raise FoundationError('STAGING_NOT_READY', '暂存文件尚未完成校验，不能作为完整来源固定。')
    metadata = json.loads(slot.metadata_json, parse_float=Decimal)
    if not slot.digest or metadata.get('sha256') != slot.digest:
        raise FoundationError('SOURCE_HASH_MISMATCH', '暂存导入缺少一致的文件摘要。')
    expected = slot.total_subjects - slot.imported_subjects
    actual = session.scalar(select(func.count()).select_from(Stage).where(Stage.dataset == dataset))
    if expected != actual or expected < 0:
        raise FoundationError('CAPTURE_COUNT_MISMATCH', '暂存导入分母与剩余标的不一致。')
    target = DUMPS[dataset][1]
    refs = []
    observed = now()
    for staged in session.scalars(select(Stage).where(Stage.dataset == dataset).order_by(Stage.subject).execution_options(yield_per=10)):
        if before_write is not None:
            before_write()
        raw = json.loads(staged.data_json, parse_float=Decimal)
        if not isinstance(raw, dict) or not isinstance(raw.get('item'), list):
            raise FoundationError('SOURCE_SCHEMA_INVALID', '暂存标的缺少明确记录列表。')
        content = dict(raw, adjust='none', bulk_source=metadata, coverage='observed_rows_only',
                       requested_start=metadata.get('observed_start'), requested_end=metadata.get('observed_end'))
        if target == 'stock_actions':
            content['thscode'] = staged.subject
        wrapper = dict(format=FORMAT, subject=staged.subject, native_dataset=target, content=content)
        ref = register_baseline(session, source='tonghuashun', dataset=target,
            scope=dict(kind=FORMAT, import_dataset=dataset, generation=str(generation), artifact_hash=slot.digest,
                       subject=staged.subject), rows=[wrapper], observed_at=observed, decoder_id=execution_id,
            event_key=digest('fixed-staged-subject-v1', [dataset, generation, staged.subject]))
        refs.append(ref.id)
    from app.data_foundation.catalog import register_dependencies
    dependency = register_dependencies(session, [dict(source_ref_id=ref, purpose='fixed_staged_subject') for ref in refs])
    result = dict(dataset=dataset, generation=generation, source_dataset=target,
                  staged_subjects=actual, imported_at_capture=slot.imported_subjects, total_subjects=slot.total_subjects,
                  artifact_hash=slot.digest, source_ref_ids=refs, dependency_id=dependency.id, preserve_head=True)
    manifest = register_baseline(session, source='tonghuashun', dataset='staged_import_manifest',
        scope=dict(dataset=dataset, generation=str(generation)), rows=[result], observed_at=observed,
        decoder_id=execution_id, event_key=event)
    return dict(result, manifest_ref_id=manifest.id)


def freeze_request_units(session, *, execution_id, before_write=None):
    """Preserve opaque local units, never guess an identity from response text."""
    refs = []
    for unit in session.scalars(select(Unit).order_by(Unit.scope, Unit.request_key).execution_options(yield_per=10)):
        if before_write is not None:
            before_write()
        response = json.loads(unit.data_json, parse_float=Decimal)
        response_hash = digest('unresolved-response-v1', response)
        ref = register_baseline(session, source='tonghuashun', dataset='unresolved_request_units',
            scope=dict(scope_hash=unit.scope, request_hash=unit.request_key, reason='SOURCE_IDENTITY_UNRESOLVED'),
            rows=[dict(scope_hash=unit.scope, request_hash=unit.request_key,
                       response=response)],
            observed_at=unit.created_at, decoder_id=execution_id,
            event_key=digest('unresolved-local-request-v1', [unit.scope, unit.request_key, response_hash]))
        refs.append(ref.id)
    return dict(status='unresolved', reason_code='SOURCE_IDENTITY_UNRESOLVED',
                fixed_units=len(refs), source_ref_ids=refs)


def staged_body(source, raw):
    """Validate the fixed wrapper before using the existing business adapter."""
    from app.data_foundation.record_adapters import convert, rows_for
    if (not isinstance(raw, dict) or raw.get('format') != FORMAT or raw.get('native_dataset') != source.dataset
            or source.dataset not in {value[1] for value in DUMPS.values()}
            or not isinstance(raw.get('subject'), str) or not raw['subject']):
        raise FoundationError('SOURCE_SCHEMA_INVALID', '固定暂存来源封装或主体无效。')
    native = SimpleNamespace(source=source.source, dataset=source.dataset,
                             subject=raw['subject'], representation='ths_observation')
    rows = rows_for(native, raw.get('content'))
    if len(rows) != 1:
        raise FoundationError('SOURCE_SCHEMA_INVALID', '固定暂存窗口必须作为完整对象处理。')
    return convert(native, rows[0])

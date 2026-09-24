"""Whole-report adapters for the shared fenced A/B work and release kernel."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now
from app.data_foundation.holdings import DATASET, SERIES, ResolvedIdentity, normalize_report, target_key
from app.data_foundation.holding_models import (CandidateReport, CandidateReportMember,
    OfficialReport, OfficialReportMember, ReportBlockMember)
from app.data_foundation.models import Binding, DependencyEntry, SourceRef, Definition
from app.data_foundation.source_refs import read_source
from app.data_foundation.work import fenced, finish_batch, append_event, create_work
from app.data_foundation.work_models import (Work, Candidate, CandidateManifest, CandidateEntry,
    Unit, Decision, Release, ReleaseBlock, BlockRef, Assessment)
from app.data_foundation.quality import assessment

DOMAIN_KEY = 'holdings-report-v1'
HEADER_FIELDS = ('fund_share_id', 'period_start', 'period_end', 'report_type', 'scope_kind',
                 'series', 'member_count', 'transport_complete', 'portfolio_complete', 'public_at')
MEMBER_FIELDS = ('member_ordinal', 'member_instrument_id', 'binding_id', 'source_member_code',
                 'hold_ratio', 'market_value', 'period_change_ratio', 'rank', 'field_quality_json')
KEY_FIELDS = ('fund_share_id', 'period_start', 'period_end', 'report_type', 'scope_kind', 'series')

# Input adapters interpret source-local structure before the shared object
# validator. Only the approved provider is installed in production; isolated
# extension tests register a second native shape without enabling a live feed.
SOURCE_READERS = {'tonghuashun': lambda data: data}


def domain_hash():
    return digest('holdings-domain-code', {p: (Path(__file__).parent / p).read_text()
        for p in ('holdings.py', 'holding_work.py', 'holding_models.py', 'identity.py')})


def fields(row, names):
    return {name: getattr(row, name) for name in names}


def object_hash(head, members):
    return digest('typed-holdings-object-v1', {'header': fields(head, HEADER_FIELDS),
        'members': [fields(m, MEMBER_FIELDS) for m in members]})


def _verify(work):
    params = json.loads(work.parameters_json)
    if (params['domain'] != DOMAIN_KEY or params['domain_hash'] != domain_hash()
            or params['dataset'] != DATASET or params['series'] != SERIES):
        raise FoundationError('DEPENDENCY_MISSING', '固定报告转换依赖不可加载。')
    return params


def report_input(session, source):
    """Read content and request receipts only from the sealed source closure."""
    container = read_source(session, source.id)
    if source.representation == 'local_table_baseline':
        if len(container) != 1:
            raise FoundationError('SOURCE_SCHEMA_INVALID', '报告基线必须包含一份完整来源容器。')
        envelope = container[0]
        reader = SOURCE_READERS.get(source.source)
        if reader is None:
            raise FoundationError('DEPENDENCY_MISSING', '固定报告来源的结构读取器尚未实现。')
        return envelope['subject'], reader(envelope['data']), envelope.get('requests')
    from app.data_foundation.models import ObservationDependency
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation
    receipts = []
    for raw in session.scalars(select(TonghuashunObservation.request_json).join(ObservationDependency,
            ObservationDependency.observation_id == TonghuashunObservation.id)
            .where(ObservationDependency.source_ref_id == source.id)):
        decoded = json.loads(raw)
        if isinstance(decoded, list):
            receipts.extend(decoded)
    return source.subject, container, receipts


def normalize_batch(session, work_id, epoch):
    work = fenced(session, work_id, epoch)
    params = _verify(work)
    if work.kind != 'A':
        raise ValueError('Normalization work required')
    source = session.get(SourceRef, work.source_ref_id)
    if source.dataset != 'fund_stock_history':
        raise FoundationError('SOURCE_SCHEMA_INVALID', '报告读取器需要已固定的历史股票持仓原件。')
    subject, container, receipts = report_input(session, source)
    bindings = session.scalars(select(Binding).join(DependencyEntry, DependencyEntry.binding_id == Binding.id)
        .where(DependencyEntry.manifest_id == work.dependency_id)).all()
    def resolve(code, kind, start, end):
        matches = [b for b in bindings if b.source == source.source and b.subject == f'{kind}:{code}'
            and b.status == 'resolved' and b.valid_from <= start <= end < b.valid_to]
        if len(matches) != 1:
            return None
        b = matches[0]
        return ResolvedIdentity(b.instrument_id, b.id, b.valid_from, b.valid_to, b.known_at)
    keys = params['report_keys']
    work.total = len(keys)
    # A report is the indivisible transaction unit, not a row-budget fragment.
    # Only one report is attempted per lease batch to keep work resumable.
    if work.cursor < len(keys):
        index = work.cursor
        result = normalize_report(subject=subject, selected_key=keys[index],
                                  container=container, resolve_identity=resolve, receipt_requests=receipts)
        head = result['header']
        typed_header = ({k: head[k] for k in HEADER_FIELDS if k in head}
            | {'transport_complete': True, 'portfolio_complete': None}) if head else None
        typed_members = [{k: member[k] for k in MEMBER_FIELDS if k in member}
            | {'field_quality_json': encode(member['field_quality'])} for member in result['members']]
        # Hash the exact typed representation before inserting the immutable
        # candidate. Publication can then verify integrity without source replay.
        value_hash = (object_hash(SimpleNamespace(**typed_header),
            [SimpleNamespace(**m) for m in typed_members]) if result['readiness'] == 'ready'
            else result.get('values_hash', digest('report-input-invalid', result)))
        if head and not params['start'] <= str(head['period_end']) <= params['end']:
            raise FoundationError('SCOPE_MISMATCH', '报告超出固定工作的报告期范围。')
        checked = assessment(session, input_hash=digest('report-input', [work.fingerprint, keys[index]]),
            rule_hash=params['domain_hash'], scope_hash=work.scope_key,
            status='pass' if result['readiness'] == 'ready' else 'fail',
            results={'reasons': result['reasons'], 'source_report_key': keys[index],
                'header': head, 'transport_complete': result['transport_complete'],
                'portfolio_complete': result['portfolio_complete'],
                'member_count': head['member_count'] if head else None})
        candidate = Candidate(work_id=work.id, source_ref_id=source.id,
            binding_id=head['binding_id'] if head else None, dependency_id=work.dependency_id,
            unit_key=keys[index], occurrence=index, values_hash=value_hash,
            readiness=result['readiness'], assessment_id=checked.id, created_at=now())
        session.add(candidate)
        session.flush()
        if candidate.readiness == 'ready':
            typed = CandidateReport(candidate_id=candidate.id, **typed_header)
            session.add(typed)
            session.flush()
            members = []
            for values in typed_members:
                row = CandidateReportMember(candidate_id=candidate.id, **values)
                members.append(row)
                session.add(row)
        session.add(Unit(work_id=work.id, unit_key=keys[index], result_hash=candidate.values_hash,
                         row_count=head['member_count'] if head else 0))
        work.cursor += 1
    session.flush()
    fenced(session, work.id, epoch)
    if work.cursor == work.total:
        rows = session.scalars(select(Candidate).where(Candidate.work_id == work.id)
                               .order_by(Candidate.occurrence)).all()
        if len(rows) != len(keys):
            raise FoundationError('CANDIDATE_INCOMPLETE', '报告候选清单不完整。')
        manifest = CandidateManifest(work_id=work.id, row_count=len(rows),
            manifest_hash=digest('candidates', [[c.id, c.values_hash, c.readiness] for c in rows]), created_at=now())
        session.add(manifest)
        session.flush()
        session.add_all([CandidateEntry(manifest_id=manifest.id, ordinal=i, candidate_id=c.id)
                         for i, c in enumerate(rows)])
        append_event(session, work, 'quality', 'evaluated', {
            'ready': sum(c.readiness == 'ready' for c in rows),
            'quarantined': sum(c.readiness != 'ready' for c in rows), 'unit': 'reports',
            'members': sum(u.row_count for u in session.scalars(select(Unit).where(Unit.work_id == work.id)))})
    finish_batch(session, work, status='succeeded' if work.cursor == work.total else 'queued')
    return work.cursor


def plan_actions(session, manifest_id, policy_id, parent_release_id=None, *, input_set_id=None):
    from app.data_foundation.inputs import input_manifests
    manifests = input_manifests(session, manifest_id=manifest_id, input_set_id=input_set_id)
    if any(session.get(Work, m.work_id).status != 'succeeded' for m in manifests):
        raise FoundationError('CANDIDATE_NOT_SEALED', '报告标准化尚未完成。')
    policy = session.get(Definition, policy_id)
    if policy is None or policy.kind != 'policy':
        raise FoundationError('POLICY_MISMATCH', '报告治理规则不存在。')
    definition = json.loads(policy.definition_json)
    if definition.get('atomicity') != 'whole-report':
        raise FoundationError('POLICY_MISMATCH', '报告治理必须按整对象准入。')
    from app.data_foundation.governance import choose
    groups = {}
    for candidate in session.scalars(select(Candidate).join(CandidateEntry, CandidateEntry.candidate_id == Candidate.id)
                                    .where(CandidateEntry.manifest_id.in_([m.id for m in manifests]))):
        report = session.get(CandidateReport, candidate.id)
        head = fields(report, KEY_FIELDS) if report else json.loads(session.get(Assessment, candidate.assessment_id).results_json).get('header')
        if not head or not head.get('fund_share_id'):
            # The business key itself is unknown. Do not manufacture a gap under
            # a freshly invented identity or publish a misleading empty release.
            raise FoundationError('UNLOCATED_CANDIDATE', '报告基金身份未确认，无法定位正式业务键。')
        head = {k: str(head[k]) for k in KEY_FIELDS}
        key = target_key(**{('fund_id' if k == 'fund_share_id' else 'start' if k == 'period_start'
            else 'end' if k == 'period_end' else 'kind' if k == 'report_type' else 'scope' if k == 'scope_kind' else k): v
            for k, v in head.items()})
        source = session.get(SourceRef, candidate.source_ref_id)
        group = groups.setdefault(key, {'head': head, 'candidates': []})
        group['candidates'].append({'id': str(candidate.id), 'source': source.source,
            'series': head['series'], 'ready': report is not None and candidate.readiness == 'ready'})
    actions = []
    for key, group in sorted(groups.items()):
        action, selected, reason = choose(group['candidates'], definition)
        withdrawals = definition.get('withdrawn_report_keys', {})
        retentions = definition.get('retained_report_keys', {})
        parent_id = None
        if key in retentions:
            if key in withdrawals or not isinstance(retentions[key], str) or not retentions[key].strip():
                raise FoundationError('POLICY_MISMATCH', '报告保留规则冲突或缺少依据。')
            parent = session.scalar(select(ReportBlockMember).join(BlockRef, BlockRef.block_id == ReportBlockMember.block_id)
                .where(BlockRef.release_id == parent_release_id, ReportBlockMember.target_key == key))
            if parent is None or parent.state != 'value':
                raise FoundationError('RETAIN_INVALID', '保留报告不属于固定父发布的正式对象。')
            action, selected, reason, parent_id = 'retain', None, retentions[key], str(parent.official_id)
        if key in withdrawals:
            if not isinstance(withdrawals[key], str) or not withdrawals[key].strip():
                raise FoundationError('POLICY_MISMATCH', '报告撤回必须在固定政策中记录依据。')
            action, selected, reason = 'withdraw', None, withdrawals[key]
        actions.append({'target_key': key, **group['head'], 'action': action,
                        'candidate_id': selected, 'reason': reason, 'retained_official_id': parent_id})
    return actions


def create_governance(session, *, normalization_id, execution_id, policy_id,
                      parent_release_id=None, expected_head_revision=0, expected_issue_epoch=0):
    origin = session.get(Work, normalization_id)
    manifest = session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == normalization_id))
    if origin is None or manifest is None:
        raise FoundationError('CANDIDATE_NOT_SEALED', '报告尚无固定候选清单。')
    return create_work(session, kind='B', contract_id=origin.contract_id, execution_id=execution_id,
        dependency_id=origin.dependency_id, parameters={**_verify(origin), 'actions': plan_actions(session, manifest.id, policy_id, parent_release_id)},
        candidate_manifest_id=manifest.id, policy_id=policy_id, parent_release_id=parent_release_id,
        expected_head_revision=expected_head_revision, expected_issue_epoch=expected_issue_epoch)


def stage_decisions(session, work_id, epoch):
    work = fenced(session, work_id, epoch)
    params = _verify(work)
    actions = params['actions']
    from app.data_foundation.inputs import candidate_origins
    origins = candidate_origins(session, work)
    if work.candidate_input_set_id:
        from app.data_foundation.governance import verify_multi_plan
        verify_multi_plan(session, work)
    else:
        manifest = session.get(CandidateManifest, work.candidate_manifest_id)
        origin = session.get(Work, manifest.work_id)
        if (work.kind != 'B' or work.dependency_id != origin.dependency_id
                or {k: v for k, v in params.items() if k != 'actions'} != json.loads(origin.parameters_json)
                or actions != plan_actions(session, manifest.id, work.policy_id, work.parent_release_id)):
            raise FoundationError('GOVERNANCE_PLAN_MISMATCH', '报告治理计划与固定输入不一致。')
    if work.cursor < len(actions):
        action = actions[work.cursor]
        checked = assessment(session, input_hash=digest('report-action', action),
            rule_hash=session.get(Definition, work.policy_id).content_hash,
            scope_hash=work.scope_key, status='pass' if action['action'] in ('select', 'retain') else 'fail',
            results={'reason': action['reason']})
        cid = UUID(action['candidate_id']) if action['candidate_id'] else None
        decision = Decision(assessment_id=checked.id, work_id=work.id,
            target_key=action['target_key'], candidate_manifest_id=origins.get(cid) if work.candidate_input_set_id else work.candidate_manifest_id,
            candidate_input_set_id=work.candidate_input_set_id,
            selected_candidate_id=cid, parent_official_id=None,
            parent_report_id=UUID(action['retained_official_id']) if action['retained_official_id'] else None, action=action['action'],
            evidence_json=encode({'reason': action['reason'], 'comparison': 'not_applicable',
                                 'atomic_unit': 'whole-report', 'policy_id': work.policy_id}), created_at=now())
        session.add(decision)
        session.flush()
        if cid:
            head = session.get(CandidateReport, cid)
            members = session.scalars(select(CandidateReportMember).where(CandidateReportMember.candidate_id == cid)
                                      .order_by(CandidateReportMember.member_ordinal)).all()
            if (not head.transport_complete or len(members) != head.member_count
                    or [m.member_ordinal for m in members] != list(range(head.member_count))
                    or object_hash(head, members) != session.get(Candidate, cid).values_hash):
                raise FoundationError('PUBLICATION_INCOMPLETE', '报告头与成员未完整封存。')
            official = OfficialReport(**fields(head, HEADER_FIELDS), candidate_id=cid, decision_id=decision.id,
                                      values_hash=object_hash(head, members), created_at=now())
            session.add(official)
            session.flush()
            session.add_all([OfficialReportMember(official_id=official.id, **fields(m, MEMBER_FIELDS)) for m in members])
        member_count = head.member_count if cid else (session.get(OfficialReport, UUID(action['retained_official_id'])).member_count
            if action['retained_official_id'] else 0)
        session.add(Unit(work_id=work.id, unit_key=action['target_key'], result_hash=digest('report-action', action), row_count=member_count))
        work.cursor += 1
    session.flush()
    fenced(session, work.id, epoch)
    if work.cursor < len(actions):
        finish_batch(session, work, status='queued')
        return None
    return seal_release(session, work)


def block_value(member):
    return fields(member, ('target_key', *KEY_FIELDS, 'state', 'official_id', 'decision_id'))


def seal_release(session, work):
    old = session.scalar(select(Release).where(Release.work_id == work.id))
    if old:
        return old
    params = json.loads(work.parameters_json)
    if work.cursor != work.total:
        raise FoundationError('PUBLICATION_INCOMPLETE', '报告治理尚未完成。')
    # Read inherited hashes with the block references instead of resolving
    # each unchanged report with a separate database query.
    blocks, block_hashes = {}, {}
    for partition, block_id, content_hash in session.execute(select(
            BlockRef.partition_key, BlockRef.block_id, ReleaseBlock.content_hash)
            .outerjoin(ReleaseBlock, ReleaseBlock.id == BlockRef.block_id)
            .where(BlockRef.release_id == work.parent_release_id)):
        if content_hash is None:
            raise FoundationError('MANIFEST_INVALID', '父发布块缺失。')
        blocks[partition], block_hashes[block_id] = block_id, content_hash
    decisions = {d.target_key: d for d in session.scalars(select(Decision).where(Decision.work_id == work.id))}
    if len(decisions) != len(params['actions']):
        raise FoundationError('PUBLICATION_INCOMPLETE', '报告治理决策未收齐。')
    from datetime import date
    for action in params['actions']:
        decision = decisions[action['target_key']]
        official = session.scalar(select(OfficialReport).where(OfficialReport.decision_id == decision.id))
        if decision.action == 'retain':
            official = session.get(OfficialReport, decision.parent_report_id)
        if decision.action in ('select', 'retain') and official is None:
            raise FoundationError('PUBLICATION_INCOMPLETE', '报告正式值缺失。')
        member = ReportBlockMember(target_key=action['target_key'],
            fund_share_id=UUID(action['fund_share_id']), period_start=date.fromisoformat(action['period_start']),
            period_end=date.fromisoformat(action['period_end']), report_type=action['report_type'],
            scope_kind=action['scope_kind'], series=action['series'], decision_id=decision.id,
            official_id=official.id if official else None,
            state={'select': 'value', 'retain': 'value', 'gap': 'gap', 'block': 'blocked', 'withdraw': 'withdrawn'}[decision.action])
        block = ReleaseBlock(partition_key=action['target_key'], row_count=1,
                             content_hash=digest('report-block-v1', block_value(member)), created_at=now())
        session.add(block)
        session.flush()
        member.block_id = block.id
        session.add(member)
        session.flush()
        blocks[block.partition_key] = block.id
        block_hashes[block.id] = block.content_hash
    summary = [[key, bid, block_hashes[bid]] for key, bid in sorted(blocks.items())]
    release = Release(work_id=work.id, scope_key=work.scope_key, parent_id=work.parent_release_id,
                      manifest_hash=digest('release-manifest', summary), status='draft', created_at=now())
    session.add(release)
    session.flush()
    session.add_all([BlockRef(release_id=release.id, partition_key=k, block_id=v) for k, v in blocks.items()])
    session.flush()
    validate_release(session, release)
    release.status = 'sealed'
    session.flush()
    return release


def validate_release(session, release):
    summary = []
    for ref in session.scalars(select(BlockRef).where(BlockRef.release_id == release.id).order_by(BlockRef.partition_key)):
        block = session.get(ReleaseBlock, ref.block_id)
        rows = session.scalars(select(ReportBlockMember).where(ReportBlockMember.block_id == block.id)).all()
        if len(rows) != 1 or block.row_count != 1 or rows[0].target_key != ref.partition_key:
            raise FoundationError('MANIFEST_INVALID', '报告清单对象不完整。')
        member = rows[0]
        if digest('report-block-v1', block_value(member)) != block.content_hash:
            raise FoundationError('MANIFEST_INVALID', '报告清单摘要不一致。')
        decision = session.get(Decision, member.decision_id)
        expected_state = {'select': 'value', 'retain': 'value', 'gap': 'gap', 'block': 'blocked', 'withdraw': 'withdrawn'}
        if (decision is None or decision.target_key != member.target_key
                or expected_state.get(decision.action) != member.state
                or session.get(Work, decision.work_id).scope_key != release.scope_key):
            raise FoundationError('MANIFEST_INVALID', '报告对象与决策目标不一致。')
        if member.official_id:
            head = session.get(OfficialReport, member.official_id)
            if decision.action == 'retain':
                work = session.get(Work, decision.work_id)
                parent = session.scalar(select(ReportBlockMember).join(BlockRef, BlockRef.block_id == ReportBlockMember.block_id)
                    .where(BlockRef.release_id == work.parent_release_id, ReportBlockMember.target_key == member.target_key))
                if (decision.parent_report_id != head.id or parent is None
                        or parent.state != 'value' or parent.official_id != head.id):
                    raise FoundationError('MANIFEST_INVALID', '保留报告的固定父版本引用不一致。')
                # The new decision records retention, while the immutable head
                # retains its original selection and source lineage.
                decision = session.get(Decision, head.decision_id)
            members = session.scalars(select(OfficialReportMember).where(OfficialReportMember.official_id == head.id)
                                      .order_by(OfficialReportMember.member_ordinal)).all()
            if (head.decision_id != decision.id or decision.action != 'select'
                    or head.candidate_id != decision.selected_candidate_id
                    or fields(head, KEY_FIELDS) != fields(member, KEY_FIELDS) or not head.transport_complete
                    or len(members) != head.member_count
                    or [m.member_ordinal for m in members] != list(range(head.member_count))
                    or object_hash(head, members) != head.values_hash):
                raise FoundationError('MANIFEST_INVALID', '正式报告头与成员的范围或摘要不一致。')
        summary.append([ref.partition_key, block.id, block.content_hash])
    if digest('release-manifest', summary) != release.manifest_hash:
        raise FoundationError('MANIFEST_INVALID', '正式报告发布清单摘要不一致。')

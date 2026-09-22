"""Finite all-local source intake using the existing immutable source registry.

This module does not claim normalization or publication. Campaigns retain every
known and observed source key, including empty sources and import channels.
Each dataset's scan fixes its own committed version list and records its capture
time; subsequent source arrivals are picked up by a new campaign. No provider
client, supplier configuration update, or source latest substitution is used.
"""
import argparse
import json
import platform
from uuid import UUID

from sqlalchemy import select, union
from sqlalchemy.orm import Session

from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.catalog import lock_key, now
from app.data_foundation.intake import register_scope, set_paused, start_scan, scan_page, scan_summary
from app.data_foundation.intake_models import IntakeCampaign, CampaignScan, IntakeScope, IntakeControl, Scan
from app.data_foundation.models import Execution
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation, TonghuashunCollectionState as State
from app.data_ingestion.tonghuashun.contracts import DATASETS

# Channels retain their evidence but do not create duplicate business datasets.
CHANNEL_TARGETS = {'stock_daily_dump': 'stock_daily', 'stock_recent_dump': 'stock_daily',
                   'stock_actions_dump': 'stock_actions'}


def register_campaign(session, *, event_key, decoder_id):
    if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 128:
        raise ValueError('A campaign event key of 1 to 128 characters is required')
    if session.get(Execution, decoder_id) is None:
        raise FoundationError('DEPENDENCY_MISSING', '来源解码执行版本尚未登记。')
    lock_key(session, 'local-intake-campaign', event_key)
    existing = session.scalar(select(IntakeCampaign).where(IntakeCampaign.event_key == event_key))
    if existing:
        if existing.decoder_id != decoder_id:
            raise FoundationError('INPUT_CHANGED', '同一全量纳入周期不能替换执行版本。')
        return existing
    actual = set(session.scalars(union(select(Observation.dataset), select(State.dataset))))
    # Registry keys supply honest empty outcomes, while actual keys ensure an
    # unregistered local domain never silently disappears from the denominator.
    datasets = sorted(actual | set(DATASETS) | set(CHANNEL_TARGETS))
    row = IntakeCampaign(event_key=event_key, decoder_id=decoder_id,
        datasets_json=encode(datasets), created_at=now())
    session.add(row)
    session.flush()
    return row


def capture_dataset(session, campaign_id, dataset):
    campaign = session.get(IntakeCampaign, campaign_id)
    if campaign is None or dataset not in json.loads(campaign.datasets_json):
        raise FoundationError('SCOPE_MISMATCH', '来源键不属于该固定全量纳入周期。')
    lock_key(session, 'campaign-capture', [str(campaign_id), dataset])
    link = session.get(CampaignScan, (campaign_id, dataset))
    if link:
        return session.get(Scan, link.scan_id)
    scope = register_scope(session, dataset=dataset, subject='*', variant='*',
        selector={'scope': 'all_local'}, decoder_id=campaign.decoder_id)
    scan = start_scan(session, scope.id, f'campaign:{campaign.id}', mode='frozen')
    session.add(CampaignScan(campaign_id=campaign_id, dataset=dataset, scan_id=scan.id))
    # Explicitly invoking this campaign is authorization to process its frozen
    # local evidence. Merely deploying the code does not enable intake.
    set_paused(session, scope.id, False)
    session.flush()
    return scan


def advance(engine, campaign_id, *, steps=1, page_size=100):
    """Commit short pages independently and rotate fairly across datasets."""
    if not 1 <= steps <= 1000 or not 1 <= page_size <= 200:
        raise ValueError('Campaign steps must be 1..1000 and page size 1..200')
    with Session(engine) as session:
        campaign = session.get(IntakeCampaign, campaign_id)
        if campaign is None:
            raise FoundationError('SOURCE_UNAVAILABLE', '全量纳入周期不存在。')
        # A resumed campaign must not attribute new decoder code to an old
        # immutable execution manifest. Publication independently verifies the
        # full runtime archive before any formal value can become visible.
        from app.data_foundation.execution import installed_code_hash
        execution = session.get(Execution, campaign.decoder_id)
        manifest = json.loads(execution.manifest_json)
        if (manifest['parser']['hash'] != installed_code_hash()
                or manifest['python_version'] != platform.python_version()):
            raise FoundationError('DEPENDENCY_MISSING', '全量纳入周期的固定解码版本与当前运行代码不一致。')
        datasets = json.loads(campaign.datasets_json)
    results = []
    for _ in range(steps):
        # Least-progressed input first also avoids starvation across CLI restarts.
        with Session(engine) as session:
            linked = {dataset: session.get(Scan, scan_id) for dataset, scan_id in session.execute(
                select(CampaignScan.dataset, CampaignScan.scan_id).where(CampaignScan.campaign_id == campaign_id))}
            pending = [dataset for dataset in datasets if dataset not in linked or (
                linked[dataset].status != 'completed'
                and not session.get(IntakeControl, linked[dataset].scope_id).paused)]
            if not pending:
                break
            dataset = min(pending, key=lambda key: (linked[key].seen if key in linked else -1, key))
        # Freeze membership in a separate transaction so a page crash cannot
        # accidentally recapture a larger source version list on retry.
        with Session(engine) as session, session.begin():
            scan_id = capture_dataset(session, campaign_id, dataset).id
        with Session(engine) as session, session.begin():
            result = scan_page(session, scan_id, limit=page_size)
        results.append(result)
        if result['paused']:
            break
    return results


def campaign_summary(session, campaign_id):
    campaign = session.get(IntakeCampaign, campaign_id)
    if campaign is None:
        raise FoundationError('SOURCE_UNAVAILABLE', '全量纳入周期不存在。')
    entries = {row.dataset: row for row in session.scalars(
        select(CampaignScan).where(CampaignScan.campaign_id == campaign_id))}
    items = []
    for dataset in json.loads(campaign.datasets_json):
        link = entries.get(dataset)
        if link is None:
            item = {'dataset': dataset, 'status': 'not_captured'}
        else:
            scan = session.get(Scan, link.scan_id)
            item = scan_summary(session, session.get(IntakeScope, scan.scope_id), scan)
            item['captured_at'] = scan.created_at
        item['channel_target'] = CHANNEL_TARGETS.get(dataset)
        items.append(item)
    return {'campaign_id': campaign.id, 'event_key': campaign.event_key,
        'source': 'tonghuashun', 'items': items,
        'traversal_complete': all(item.get('traversal_complete', False) for item in items),
        'registration_complete': all(item.get('registration_complete', False) for item in items),
        'formal_publication_status': 'not_performed_by_intake',
        'message': '全量来源纳入结果按数据集分别结算，来源版本数量不代表正式业务键数量。'}


def main():
    from app.db.session import get_engine
    parser = argparse.ArgumentParser(description='全量纳入已有同花顺本地来源版本，不调用供应商、不发布正式值。')
    parser.add_argument('command', choices=['start', 'advance', 'status'])
    parser.add_argument('--event-key')
    parser.add_argument('--decoder-id', type=UUID)
    parser.add_argument('--campaign-id', type=UUID)
    parser.add_argument('--steps', type=int, default=1)
    parser.add_argument('--page-size', type=int, default=100)
    args = parser.parse_args()
    engine = get_engine()
    if args.command == 'start':
        if not args.event_key or not args.decoder_id:
            parser.error('start requires --event-key and --decoder-id')
        with Session(engine) as session, session.begin():
            result = {'campaign_id': register_campaign(session, event_key=args.event_key, decoder_id=args.decoder_id).id}
    else:
        if not args.campaign_id:
            parser.error('--campaign-id is required')
        if args.command == 'advance':
            for result in advance(engine, args.campaign_id, steps=args.steps, page_size=args.page_size):
                print(encode(result), flush=True)
        with Session(engine) as session:
            result = campaign_summary(session, args.campaign_id)
    print(encode(result))


if __name__ == '__main__':
    main()

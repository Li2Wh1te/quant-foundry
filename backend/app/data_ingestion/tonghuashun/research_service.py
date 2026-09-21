"""Resumable research collection policies, independent of Tushare identities."""

from datetime import UTC, datetime, timedelta, date
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_ingestion.clients.tonghuashun import TonghuashunError
from app.data_ingestion.tonghuashun.acquisition import Acquisition
from app.data_ingestion.tonghuashun.contracts import (
    CollectionError, CollectionConflict, DATASETS, SHANGHAI, date_ms, exact_json, items, provider_date, years_before,
)
from app.data_ingestion.tonghuashun.milestone_three import DATES, SELECTED, SLOTS, TRADE_ONLY
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.research_acquisition import Unit, code_rows, fetch


def calendar_days(acq):
    data = acq.read('a-share.calendar.trading-days', {})
    rows = items(data)
    days = [provider_date(r.get('date_ms')) for r in rows]
    if len(days) != len(set(days)) or any(r.get('date') != d.strftime('%Y%m%d') for r, d in zip(rows, days)):
        raise CollectionError('同花顺交易日历包含重复或不一致日期。')
    return sorted(days)


def boundary(spec, unit, now, mode):
    if mode == 'reconcile':
        value = now.replace(day=1, hour=3, minute=0, second=0, microsecond=0)
        return value if now >= value else (value - timedelta(days=1)).replace(day=1)
    if spec.frequency == 'weekly':
        return (now - timedelta(days=(now.weekday()+1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    slots = ((1200,) if unit.subject == 'day' else (600, 660, 840, 900)) if spec.kind == 'm3_period' else SLOTS.get(spec.frequency, (1200,))
    past = [now.replace(hour=m//60, minute=m % 60, second=0, microsecond=0) for m in slots if m <= now.hour*60+now.minute]
    # Never turn an early-morning run into yesterday's current snapshot.
    return max(past) if past else None


def plan(spec, params, repo, acq, now):
    today = now.date()
    days = calendar_days(acq) if spec.key in (TRADE_ONLY | (DATES - {'hot_history'})) or spec.kind == 'm3_period' else []
    if spec.key in TRADE_ONLY and today not in days:
        return []
    if spec.kind == 'm3_global':
        return [Unit('market', {})]
    if spec.kind == 'm3_period':
        return [Unit('day', {'period': 'day'})] + ([Unit('hour', {'period': 'hour'})] if today in days else [])
    if spec.kind == 'm3_offerings':
        return [Unit(s, {'subscribe': s}) for s in ('active', 'upcoming')]
    if spec.kind == 'm3_quota':
        return [Unit(s, {'tab': exact_json([s])}) for s in params.quota_tabs]
    if spec.key in DATES:
        cutoff = 566 if spec.frequency == 'auction' else 1200
        end = params.end_date or (today if now.hour*60+now.minute >= cutoff else today-timedelta(days=1))
        if spec.key == 'hot_history':
            start = params.start_date or years_before(today, 1)
            if start < years_before(today, 1):
                raise CollectionError('历史热股排行仅支持近一年。')
            dates = [start+timedelta(days=n) for n in range((end-start).days+1)]
        else:
            dates = [d for d in days if d <= end]
            if params.start_date:
                if params.start_date < years_before(today, 1):
                    raise CollectionError('交易日历仅覆盖近一年，无法确认更早回补范围。')
                dates = [d for d in dates if d >= params.start_date]
            elif spec.years != 1:
                dates = dates[-30:]
        result = []
        for day in dates:
            if spec.key == 'dragon_tiger':
                result.extend(Unit(day.isoformat()+'.'+board, {'date': day.isoformat(), 'board_type': board}, day)
                    for board in ('all', 'org', 'hot_money'))
            else:
                query = {'date_ms': date_ms(day)} if spec.kind == 'm3_pool' else {'date': day.isoformat()}
                result.append(Unit(day.isoformat(), query, day))
        return result
    subjects = repo.related('manager_id') if spec.kind == 'm3_manager' else repo.subjects(
        tuple(a for a in spec.assets if a in params.asset_types), current_only=not params.subjects)
    if spec.key in SELECTED and not params.subjects:
        raise CollectionError('此任务只采集指定股票，请填写标的代码。')
    if params.subjects:
        if set(params.subjects) - set(subjects):
            raise CollectionError('指定对象不在同花顺目录或基金经理资料中。')
        subjects = params.subjects
    if not subjects:
        raise CollectionError('没有可采集对象，请先完成同花顺目录和所需基金资料。')
    if spec.key == 'fund_manager_performance':
        return [Unit((s if len(s)<=54 else hashlib.sha256(s.encode()).hexdigest()[:40])+'.'+r, {'manager_id': s, 'range': r}) for s in subjects for r in ('month','tmonth','year','nowyear','now')]
    return [Unit(s, {('thscode' if spec.key == 'etf_quote' else 'thscodes' if spec.kind in ('m3_quote','m3_selected') else spec.identity): s}) for s in subjects]


def collect_research(dataset, params, raw_client, engine, *, now=None):
    from app.data_ingestion.tonghuashun.service import BudgetedClient, log_result, aware
    now = (now or datetime.now(UTC)).astimezone(SHANGHAI)
    from app.data_ingestion.tonghuashun.control import control
    monitor = control()
    spec = DATASETS[dataset]
    client = BudgetedClient(raw_client, engine)
    acq = Acquisition(client)
    variant = f'{params.start_date or "auto"}_{params.end_date or "auto"}' if params.start_date or params.end_date else 'default'
    with Session(engine) as session:
        units = plan(spec, params, CollectionRepository(session), acq, now)
        previous = {u.subject: CollectionRepository(session).read(dataset,u.subject,variant,with_data=False) for u in units}
    recent_days = sorted({u.day for u in units if u.day is not None})[-5:]
    eligible = []
    for u in units:
        old = previous[u.subject]
        threshold = boundary(spec,u,now,params.mode)
        if threshold is None:
            continue
        if params.mode == 'reconcile' and old.data is None:
            continue
        if params.mode == 'backfill' and old.status in ('succeeded','empty') and not params.refresh_today:
            continue
        if old.status in ('succeeded','empty') and not params.refresh_today:
            if u.day and u.day not in recent_days:
                continue
            last = aware(old.reconciled_at if params.mode == 'reconcile' else old.succeeded_at)
            if last is not None and last >= threshold:
                continue
        eligible.append(u)
    # Fresh dated facts precede old backfill; attempts rotate all other scopes.
    eligible.sort(key=lambda u: (0 if u.day in recent_days else 1,
        aware(previous[u.subject].attempted_at) or datetime.min.replace(tzinfo=UTC),
        -(u.day.toordinal()) if u.day else 0, u.subject))
    selected = eligible[:params.batch_size]
    summary = {'source':'tonghuashun','dataset':dataset,'subjects':len(units),'succeeded':0,'failed':0,
        'skipped':len(units)-len(eligible),'pending':len(eligible)-len(selected),
        'received':0,'changed':0,'unchanged':0,'removed':0}
    if monitor:
        monitor.emit(batch_total=len(selected), coverage_total=len(units), coverage_pending=len(eligible))
    # Batch quote endpoints at their documented caps. One stock still owns one
    # CAS/version, so an absent stock cannot be silently treated as acquired.
    # ETF snapshots accept exactly one singular thscode; stock/index quote
    # endpoints have separate documented batch contracts.
    quote_limit = 1 if spec.key == 'etf_quote' else 100
    batches = [selected[i:i+quote_limit] for i in range(0,len(selected),quote_limit)] if spec.kind == 'm3_quote' else [[u] for u in selected]
    for batch in batches:
        batch_data, batch_error, batch_trace = {}, None, []
        if monitor:
            monitor.check()
        if spec.kind == 'm3_quote':
            q = Acquisition(client)
            try:
                query = ({'thscode': batch[0].subject} if spec.key == 'etf_quote'
                         else {'thscodes': ','.join(u.subject for u in batch)})
                if spec.key == 'stock_auction':
                    query['stage'] = 'final'
                data = q.read(spec.interface,query)
                rows = items(data,allow_empty=True)
                code_rows(rows,{u.subject for u in batch})
                # The provider publishes closed auctions as status=final and
                # phase=closed. Retain the earlier ready/final envelope, while
                # rejecting live, missing, and not-ready combinations.
                if spec.key == 'stock_auction' and (data.get('data_status'), data.get('auction_phase')) not in {
                    ('final', 'closed'), ('ready', 'final'),
                }:
                    raise CollectionError('集合竞价终态尚未就绪，本次未推进完成标记。')
                batch_data = {r['thscode']:{**data,'item':[r]} for r in rows}
                batch_trace = q.requests
            except (CollectionError,TonghuashunError) as exc:
                batch_error = exc
        for u in batch:
            old = previous[u.subject]
            if monitor:
                monitor.begin(dataset, u.subject, variant, old.revision, params,
                              cache=spec.kind in ('m3_series', 'm3_selected'))
            read = Acquisition(client)
            try:
                if batch_error:
                    raise batch_error
                if spec.kind == 'm3_quote':
                    if u.subject not in batch_data:
                        raise CollectionError('行情快照未返回请求标的。')
                    data = batch_data[u.subject]
                    read.requests, read.fetched_count = batch_trace, 1
                else:
                    with Session(engine) as session:
                        full = CollectionRepository(session).read(dataset,u.subject,variant)
                    if full.revision != old.revision:
                        raise CollectionConflict('同一范围已被其他任务更新，下批重新检查。')
                    data = fetch(read,spec,u,params,full.data,now)
                data = {**data,'collection_scope':u.parameters, 'source_observed_at': now.isoformat()}
                with Session(engine) as session:
                    result = CollectionRepository(session).publish(dataset,u.subject,variant,expected=old.revision,
                        data=data,requests=read.requests,now=datetime.now(UTC),reconcile=params.mode=='reconcile')
                    if monitor:
                        monitor.published(session)
                    session.commit()
                partial = bool(data.get('failed_requests'))
                summary['failed' if partial else 'succeeded'] += 1
                for key in ('received','changed','unchanged','removed'):
                    summary[key] += result[key]
                if monitor:
                    monitor.completed(summary)
                log_result(spec,u.subject,params,data,{**result,'fetched_count':read.fetched_count},not partial,
                    'partial_reports' if partial else None)
            except CollectionConflict:
                # Do not mark valid data as invalid or overwrite the winner.
                # A later run rechecks eligibility, including a failed winner.
                summary['skipped'] += 1
                summary['pending'] += 1
            except (CollectionError,TonghuashunError) as exc:
                if monitor:
                    monitor.discard()
                kind = exc.kind if isinstance(exc,TonghuashunError) else 'invalid_data'
                with Session(engine) as session:
                    try:
                        CollectionRepository(session).fail(dataset,u.subject,variant,expected=old.revision,kind=kind,now=datetime.now(UTC))
                        session.commit()
                    except CollectionError:
                        session.rollback()
                summary['failed'] += 1
                if monitor:
                    monitor.completed(summary, advanced=False)
                log_result(spec,u.subject,params,None,{'fetched_count':read.fetched_count},False,kind,error_message=str(exc))
                if kind in ('unauthenticated','forbidden','rate_limited'):
                    raise CollectionError(f'{spec.name}采集停止：成功 {summary["succeeded"]} 个，失败 {summary["failed"]} 个；账号或限流异常，完成范围已保存。') from None
    start = min((u.day for u in selected if u.day),default=now.date())
    end = max((u.day for u in selected if u.day),default=now.date())
    summary['event'] = 'tonghuashun_collection_failed' if summary['failed'] else 'tonghuashun_collection_completed'
    summary['message'] = (f'{spec.name}采集：日期范围 {start} 至 {end}，成功 {summary["succeeded"]} 个，失败 {summary["failed"]} 个，'
        f'变更 {summary["changed"]} 条，未变更 {summary["unchanged"]} 条，跳过 {summary["skipped"]} 个，待续采 {summary["pending"]} 个；'
        + ('成功范围完成标记已推进。' if summary['succeeded'] else '本次未推进完成标记。'))
    if summary['failed']:
        raise CollectionError(summary['message'])
    return summary

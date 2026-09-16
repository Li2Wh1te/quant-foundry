"""Bounded S1 capture: dry-run and apply compare the same complete source view."""
from datetime import date, timedelta
from uuid import UUID
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.data_foundation.canonical import FoundationError, digest, normalized
from app.data_foundation.catalog import now
from app.data_foundation.identity import register_binding
from app.data_foundation.source_refs import register_baseline
from app.data_ingestion.models.etf import EtfCode
from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_ingestion.models.trading_calendar import TradingCalendarDay
from app.instruments.models import Instrument

S1_CODES = ('159915.SZ', '159919.SZ', '510300.SH', '510500.SH', '512100.SH')
S1_START, S1_END = date(2026, 6, 5), date(2026, 8, 28)
MARKETS = {'SH': 'SSE', 'SZ': 'SZSE'}


def full_row(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def capture_s1(engine):
    """Read-only repeatable-read snapshot is short and never spans user review."""
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with connection.begin():
            connection.execute(text('SET TRANSACTION READ ONLY'))
            connection.execute(text("SET LOCAL statement_timeout = '30s'"))
            with Session(bind=connection) as session:
                observed_at = session.scalar(text('SELECT transaction_timestamp()'))
                bars = session.scalars(select(EtfDailyBar).where(EtfDailyBar.source == 'tushare',
                    EtfDailyBar.ts_code.in_(S1_CODES), EtfDailyBar.trade_date.between(S1_START, S1_END))
                    .order_by(EtfDailyBar.ts_code, EtfDailyBar.trade_date)).all()
                directory = session.scalars(select(EtfCode).where(EtfCode.source == 'tushare', EtfCode.ts_code.in_(S1_CODES)).order_by(EtfCode.ts_code)).all()
                calendar = session.scalars(select(TradingCalendarDay).where(TradingCalendarDay.exchange.in_(MARKETS.values()),
                    TradingCalendarDay.calendar_date.between(S1_START, S1_END)).order_by(TradingCalendarDay.exchange, TradingCalendarDay.calendar_date)).all()
                identities = set(session.scalars(select(Instrument.id).where(Instrument.id.in_([r.etf_id for r in directory]))))
                if len(directory) != 5 or any(r.etf_id not in identities for r in directory):
                    raise FoundationError('IDENTITY_CONFLICT', 'S1目录缺失或既有标的身份不完整。')
                if len(bars) != 300 or any(sum(b.ts_code == code for b in bars) != 60 for code in S1_CODES):
                    raise FoundationError('SOURCE_CHANGED', 'S1日线范围应为5个标的各60行，当前来源不符。')
                for row in directory:
                    market = MARKETS.get(row.exchange)
                    if market is None:
                        raise FoundationError('CALENDAR_UNRESOLVED', 'S1目录市场没有明确的日历映射。')
                    actual = {b.trade_date for b in bars if b.ts_code == row.ts_code}
                    expected = {c.calendar_date for c in calendar if c.exchange == market and c.is_open}
                    all_dates = {c.calendar_date for c in calendar if c.exchange == market}
                    if actual != expected or len(all_dates) != (S1_END - S1_START).days + 1:
                        raise FoundationError('CALENDAR_UNRESOLVED', 'S1日历日期不完整或交易会话与日线不一致。')
                content = normalized({'bars': [full_row(r) for r in bars], 'directory': [full_row(r) for r in directory],
                    'calendar': [full_row(r) for r in calendar]})
    scope = {'codes': S1_CODES, 'start': S1_START, 'end': S1_END}
    # Observation time changes between review and apply; source content does not
    # get silently replaced. Include all stored columns, including audit times.
    return {'scope': normalized(scope), 'content': content, 'observed_at': observed_at,
            'content_hash': digest('s1-capture', {'scope': scope, 'content': content})}


def apply_s1(session, capture, *, expected_hash, decoder_id, event_key):
    if capture['content_hash'] != expected_hash or digest('s1-capture', {'scope': capture['scope'], 'content': capture['content']}) != expected_hash:
        raise FoundationError('SOURCE_CHANGED', '来源内容与审核清单不一致，未登记基线。')
    refs = {}
    for part, dataset in [('bars', 'etf_daily'), ('directory', 'etf_directory'), ('calendar', 'exchange_calendar')]:
        refs[part] = register_baseline(session, source='tushare', dataset=dataset, scope=capture['scope'],
            rows=capture['content'][part], observed_at=capture['observed_at'], decoder_id=decoder_id, event_key=event_key)
    bindings = [register_binding(session, source_ref_id=refs['directory'].id, subject=r['ts_code'],
        instrument_id=UUID(r['etf_id']), valid_from=S1_START, valid_to=S1_END + timedelta(days=1),
        binding_version=event_key, status='unresolved') for r in capture['content']['directory']]
    return {'source_refs': {k: str(v.id) for k, v in refs.items()}, 'unresolved_bindings': [str(b.id) for b in bindings]}

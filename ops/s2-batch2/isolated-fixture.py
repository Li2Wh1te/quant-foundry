"""Artificial inputs for a fresh, disposable image test. Never a production tool."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import sys
from uuid import NAMESPACE_URL, uuid5, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_engine
from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.registry import BY_ID
from app.data_store.locking import _filesystem_name
from app.data_store.pipeline import run_entry
from app.data_store.storage import CurrentStore
from app.instruments.models import Instrument, InstrumentCodeMappingRecord
from app.market_service.bindings import TEST_VARIANT

START, END = date(2024, 1, 1), date(2026, 10, 9)
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


class Inputs:
    def __init__(self, *rows):
        self.rows, self.summary = rows, {}

    def iter_entry(self, _entry):
        self.summary = {'complete': False}
        yield from self.rows
        self.summary = {'complete': True, 'source_rows': len(self.rows), 'state': 'present'}


def source(entry_id, subject, row, *, variant='default', observed=NOW):
    entry = BY_ID[entry_id]
    return LocalInput('tushare', entry.native, subject, variant, observed, row,
                      digest([entry_id, row]), order_kind='current_table_snapshot',
                      representation='local_table_baseline')


def main():
    settings = get_settings()
    # This utility is only mounted into the disposable test containers.
    assert settings.environment == 'test'
    assert settings.database_host == 'postgres'
    assert settings.database_name == 'qf_s2_market_test'
    assert str(settings.data_store_root) == '/app/data/current-store'
    engine = get_engine()
    if sys.argv[1:] == ['append-identity']:
        with Session(engine) as session, session.begin():
            previous = session.scalar(select(InstrumentCodeMappingRecord)
                .where(InstrumentCodeMappingRecord.source_code == '510300.SH')
                .order_by(InstrumentCodeMappingRecord.fact_version.desc()))
            assert previous is not None and previous.logical_fact_key == 'isolated-d13:510300.SH'
            known = datetime.now(timezone.utc)
            fields = {name:getattr(previous,name) for name in ('instrument_id','source','source_code','trading_code',
                'valid_from','valid_to','logical_fact_key','effective_range','mapping_source','source_revision')}
            session.add(InstrumentCodeMappingRecord(**fields,id=uuid4(),fact_version=previous.fact_version+1,
                supersedes_fact_id=previous.id,knowledge_range=Range(known,None,bounds='[)'),
                evidence='changed artificial D13 fact',known_at=known,observed_at=known))
        print(json.dumps(dict(artificial_identity_revision_appended=True)))
        return
    assert not sys.argv[1:]
    with engine.begin() as connection:
        assert connection.execute(text('SELECT count(*) FROM instruments')).scalar_one() == 0
        # Explicit artificial control fact on this empty test database only.
        connection.execute(text("INSERT INTO data_store_legacy_maintenance(singleton,phase) VALUES(1,'ready')"))
    directory, prices, factors, calendars = [], [], [], []
    with Session(engine) as session, session.begin():
        for index in range(125):
            code = '510300.SH' if index == 0 else f'{600000 + index:06}.SH'
            iid = uuid5(NAMESPACE_URL, 'qf-s2-artificial:' + code)
            session.add(Instrument(id=iid, asset_class='etf', status='active'))
            session.flush()
            session.add(InstrumentCodeMappingRecord(id=uuid4(), instrument_id=iid,
                source='tushare', source_code=code, trading_code=code.split('.')[0],
                valid_from=START, valid_to=None, fact_version=1,
                logical_fact_key='isolated-d13:' + code,
                effective_range=Range(START, None, bounds='[)'),
                knowledge_range=Range(datetime(2024, 1, 1, tzinfo=timezone.utc), None, bounds='[)'),
                mapping_source='isolated-reviewed-identity', evidence='artificial D13 identity oracle',
                known_at=datetime(2024, 1, 1, tzinfo=timezone.utc), observed_at=NOW))
            directory.append(source('E67', 'directory', dict(source='tushare', ts_code=code,
                csname='人工 ETF ' + str(index), exchange='SSE', list_date='20240101')))
    day, count = START, 0
    while day <= date(2026, 10, 31):
        opened = day.weekday() < 5  # A synthetic oracle, not an exchange rule.
        calendars.append(source('E68', 'calendar', dict(exchange='SSE', calendar_date=day.isoformat(), is_open=opened)))
        if opened and day <= END:
            value = Decimal('4.123456') + Decimal(count) / 1000000
            prices.append(source('E70', 'daily', dict(source='tushare', ts_code='510300.SH', trade_date=day.isoformat(),
                open=value, high=value + 1, low=value - 1, close=value + Decimal('.1'),
                vol=Decimal('9007199254740993.1234'), amount=Decimal('4100.1234'))))
            factors.append(source('E69', 'factor', dict(source='tushare', ts_code='510300.SH', trade_date=day.isoformat(),
                adj_factor=Decimal('1' if day < date(2026, 1, 1) else '2'))))
            count += 1
        day += timedelta(days=1)
    # A second instrument has valid independent prices without factors.
    no_factor = replace(prices[-1], content={**prices[-1].content, 'ts_code': '600001.SH'})
    prices.append(no_factor)
    factors.append(source('E69', 'factor', dict(source='tushare', ts_code='510300.SH',
        trade_date='2026-10-12', adj_factor=Decimal('100'))))  # Must not enter END/as_of.
    with CurrentStore(engine, settings.data_store_root,
            cursor_key=settings.cursor_signing_key.get_secret_value().encode(), initialize=True) as current:
        actual_fs = _filesystem_name(current.files.fd)
        assert actual_fs in ('ext4', 'xfs', 'btrfs'), actual_fs
        for entry_id, rows in [('E67', directory), ('E68', calendars), ('E70', prices), ('E69', factors),
                ('E70', [replace(row, variant=TEST_VARIANT) for row in prices]),
                ('E69', [replace(row, variant=TEST_VARIANT) for row in factors])]:
            result = run_entry(current, BY_ID[entry_id], Inputs(*rows))
            assert result['qualified'], result
        # A genuine isolated invalid input creates an issue in an unrelated
        # source-scoped object, without editing quality flags or issue tables.
        bad = source('E70', 'daily', dict(source='tushare', ts_code='699999.SH', trade_date='2026-10-09',
            open=Decimal('-1'), high=Decimal('4'), low=Decimal('3'), close=Decimal('3.5')))
        assert not run_entry(current, BY_ID['E70'], Inputs(bad))['qualified']
    print(json.dumps(dict(synthetic=True, filesystem=actual_fs, instruments=125,
        price_sessions=count, calendar_days=len(calendars), start=START.isoformat(), end=END.isoformat(),
        instrument_key='instrument:' + str(uuid5(NAMESPACE_URL, 'qf-s2-artificial:510300.SH')),
        no_factor_key='instrument:' + str(uuid5(NAMESPACE_URL, 'qf-s2-artificial:600001.SH')))))


if __name__ == '__main__':
    main()

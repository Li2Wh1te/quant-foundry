"""Default market composition and short, bounded current read sessions.

The session composes the existing request gate, CurrentStore object quality and
current inventory fingerprints. It has no backtest admission or supplier path.
Tokens describe current dependencies only; no snapshots or token ledger exist.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import re
import time
from typing import Callable, Iterator
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.data_store.adapters.canonical import NativeInputError
from app.data_store.adapters.contracts import native_json
from app.data_store.availability import require_request_ready
from app.data_store.current_revision import inventory_revision
from app.data_store.errors import DataStoreError
from app.data_store.merge import restore_row
from app.data_store.readers import Page, Query
from app.data_store.schema import fingerprint
from app.data_store.storage import CurrentStore
from app.data_store.values import control_json
from app.instruments.domain import InstrumentCodeMapping, order_mapping_segments

from .bindings import BINDINGS, ETF_CATALOG, ETF_DAILY, ETF_FACTORS, EXCHANGE_CALENDAR, MarketBinding
from .contracts import Capability, CapabilitiesResult, CatalogOption, InstrumentResolution, Notice, ReadMeta
from .errors import MarketError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_IDENTITY_ROWS = 2000
MAX_CURSOR_BYTES = 8192


@dataclass(frozen=True)
class IdentityQuery:
    """Bounded selectors over existing current-known mapping facts.

    An explicit date window selects effective code segments for D10; it never
    changes the current knowledge boundary into a simulated/publication time.
    """
    symbol: str | None = None
    asset_type: str = 'etf'
    source_codes: tuple[str, ...] = ()
    instrument_keys: tuple[str, ...] = ()
    start: date | None = None
    end: date | None = None

    def __post_init__(self):
        if (self.asset_type != 'etf' or sum(bool(v) for v in (self.symbol, self.source_codes, self.instrument_keys)) != 1
                or type(self.source_codes) is not tuple or type(self.instrument_keys) is not tuple
                or len(self.source_codes) > 200 or len(self.instrument_keys) > 200):
            raise DataStoreError('INVALID_VALUE')
        if self.symbol is not None:
            _symbol(self.symbol)
        for code in self.source_codes:
            _symbol(code)
        for key in self.instrument_keys:
            parse_instrument_key(key)
        if (self.start is None) != (self.end is None) or self.start is not None and (
                type(self.start) is not date or type(self.end) is not date or self.start > self.end):
            raise DataStoreError('INVALID_VALUE')


def parse_instrument_key(key: str) -> UUID:
    try:
        if type(key) is not str or not key.startswith('instrument:'):
            raise ValueError()
        value = UUID(key.removeprefix('instrument:'))
        if key != 'instrument:' + str(value):
            raise ValueError()
        return value
    except (ValueError, AttributeError):
        raise MarketError('IDENTITY_UNKNOWN') from None


def _mapping(row: dict) -> InstrumentCodeMapping:
    if row['asset_class'] not in ('etf', 'fund-etf') or row['status'] not in ('active', 'deprecated'):
        raise MarketError('IDENTITY_UNAVAILABLE')
    try:
        return InstrumentCodeMapping(**{name: row[name] for name in (
            'instrument_id', 'source', 'source_code', 'trading_code', 'valid_from', 'valid_to',
            'mapping_source', 'evidence', 'known_at', 'observed_at', 'source_revision',
            'fact_version', 'logical_fact_key', 'supersedes_fact_id')}, fact_id=row['id'])
    except ValueError:
        raise MarketError('IDENTITY_UNAVAILABLE') from None


def _symbol(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value):
        raise DataStoreError('INVALID_VALUE')
    return value.upper()


def _signed(key: bytes, body: dict) -> str:
    payload = native_json(body).encode()
    if len(payload) > 6000:
        raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
    return base64.urlsafe_b64encode(payload + hmac.digest(key, payload, 'sha256')).decode()


def _cursor(key: bytes, token: str, condition: str) -> dict:
    if type(token) is not str or not 1 <= len(token) <= MAX_CURSOR_BYTES:
        raise DataStoreError('INVALID_CURSOR')
    try:
        raw = base64.b64decode(token, altchars=b'-_', validate=True)
        payload, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(key, payload, 'sha256')):
            raise ValueError()
        body = json.loads(payload)
        if (type(body) is not dict or set(body) != {'v', 'condition', 'read_token', 'position'}
                or body['v'] != 1 or body['condition'] != condition
                or type(body['read_token']) is not str or type(body['position']) is not dict):
            raise ValueError()
        return body
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise DataStoreError('INVALID_CURSOR') from None


class MarketReadScope:
    """Private bounded current state for one domain request; never persisted."""
    def __init__(self, service, store, keys, conditions, identity, captured, read_at, deadline):
        self.service, self.store, self.keys = service, store, keys
        self.conditions, self.identity, self.captured = conditions, identity, captured
        self.read_at, self.deadline = read_at, deadline
        self.condition = fingerprint({'owner': service.owner_scope, 'bindings': keys,
                                      'conditions': conditions, 'identity': identity.__dict__ if identity else None})
        # The digest is keyed: it is a current equality marker, not a readable
        # internal identity/quality or supplier confirmation payload.
        self.read_token = hmac.new(service.signing_key, native_json(
            {'condition': self.condition, 'state': captured}).encode(), hashlib.sha256).hexdigest()
        self.position: dict | None = None
        self.rows_read = self.bytes_read = 0
        self.closed = False

    def check(self):
        if self.closed:
            raise DataStoreError('INVALID_VALUE')
        if self.service.cancelled():
            raise DataStoreError('OPERATION_CANCELLED')
        if time.monotonic() >= self.deadline:
            raise DataStoreError('QUERY_TIMEOUT')

    def read(self, binding_key: str, query: Query) -> Page:
        """D09-D11 use explicit formal queries, always qualified and bounded."""
        self.check()
        if binding_key not in self.keys or not query.require_qualified:
            raise DataStoreError('INVALID_VALUE')
        binding = self.service.bindings[binding_key]
        # A domain cannot silently switch the installed business representation.
        if not any(c == 'representation' and op == '=' and v == binding.representation
                   for c, op, v in query.filters):
            raise DataStoreError('INVALID_VALUE')
        page = self.store.read_many([(binding.entry.spec, query)], cancelled=self.service.cancelled)[0]
        if str(page.generation) != self.captured['datasets'][binding_key]['generation']:
            raise DataStoreError('DATA_CHANGED')
        self.rows_read += len(page.rows)
        self.bytes_read += len(native_json(page.rows).encode())
        if (self.rows_read > min(100_000, self.store.limits.commit_rows)
                or self.bytes_read > min(MAX_RESPONSE_BYTES, self.store.limits.query_bytes)):
            raise DataStoreError('QUERY_BUDGET_EXCEEDED')
        self.check()
        return page

    def objects(self, binding_key: str, page: Page) -> list[dict]:
        """Decode complete current objects via the registered typed layout."""
        self.check()
        binding = self.service.bindings[binding_key]
        if binding_key not in self.keys or page.dataset != binding.entry.spec.name:
            raise DataStoreError('INVALID_VALUE')
        groups: dict[tuple, list] = {}
        for row in page.rows:
            groups.setdefault(tuple(row[k] for k in ('representation', 'subject', 'object_key')), []).append(
                restore_row(binding.entry.spec.schema, row))
        try:
            return [binding.entry.layout.decode(rows) for rows in groups.values()]
        except NativeInputError:
            raise DataStoreError('FILE_INVALID') from None

    def make_cursor(self, position: dict) -> str:
        self.check()
        return _signed(self.service.signing_key, {'v': 1, 'condition': self.condition,
                       'read_token': self.read_token, 'position': position})

    def mapping_segments(self, instrument_key: str) -> tuple[InstrumentCodeMapping, ...]:
        """Shared evidenced mapping check, without execution rules or account gates."""
        self.check()
        instrument_id = parse_instrument_key(instrument_key)
        if self.identity is None:
            raise MarketError('IDENTITY_UNAVAILABLE')
        start = self.identity.start or self.read_at.astimezone(ZoneInfo('Asia/Shanghai')).date()
        end = self.identity.end or start
        rows = [r for r in self.captured['identity'] if r['instrument_id'] == instrument_id]
        try:
            return order_mapping_segments([_mapping(r) for r in rows], start_date=start, end_date=end,
                data_cutoff=self.read_at, instrument_id=instrument_id, source='tushare')
        except ValueError:
            raise MarketError('IDENTITY_UNAVAILABLE') from None

    def meta(self, *, scope: str, business_as_of: date | None = None, limitations=()) -> ReadMeta:
        self.check()
        return ReadMeta(read_token=self.read_token, read_at=self.read_at, scope=scope,
            business_as_of=business_as_of,
            source_labels=[self.service.bindings[key].label for key in self.keys],
            limitations=list(limitations))

    def response(self, dto: BaseModel) -> BaseModel:
        """Build and charge the output before the short commit-boundary check."""
        self.check()
        if len(dto.model_dump_json().encode()) > min(MAX_RESPONSE_BYTES, self.store.limits.query_bytes):
            raise DataStoreError('QUERY_BUDGET_EXCEEDED')
        return dto


class MarketReadService:
    def __init__(self, settings: Settings, engine, *, owner_scope: str, cancelled: Callable[[], bool] | None = None):
        self.settings, self.engine, self.owner_scope = settings, engine, owner_scope
        self.signing_key = settings.cursor_signing_key.get_secret_value().encode()
        self.cancelled = cancelled or (lambda: False)
        self.bindings = BINDINGS

    def _identities(self, session: Session, query: IdentityQuery, read_at: datetime) -> list[dict]:
        """Fold corrections BEFORE applying symbol/effective predicates.

        Only existing immutable mappings and stable identities are read. The
        small exact current result includes row versions for change detection.
        No etf_codes, names, account metadata or newly manufactured ID is used.
        """
        tables = session.execute(text("SELECT to_regclass('instruments') IS NOT NULL "
            "AND to_regclass('instrument_code_mappings') IS NOT NULL")).scalar_one()
        if not tables:
            raise MarketError('IDENTITY_MAPPING_UNAVAILABLE')
        day = read_at.astimezone(ZoneInfo('Asia/Shanghai')).date()
        start, end = query.start or day, query.end or day
        if query.symbol is not None:
            selector = '(upper(trim(m.source_code))=:symbol OR upper(trim(m.trading_code))=:symbol)'
            params = {'symbol': _symbol(query.symbol)}
        elif query.source_codes:
            selector = 'upper(trim(m.source_code))=ANY(:codes)'
            params = {'codes': [_symbol(code) for code in query.source_codes]}
        else:
            selector = 'm.instrument_id=ANY(:ids)'
            params = {'ids': [parse_instrument_key(key) for key in query.instrument_keys]}
        result = session.execute(text('''
            WITH latest AS (
                SELECT DISTINCT ON (logical_fact_key) *, xmin::text AS row_version
                FROM instrument_code_mappings
                WHERE lower(trim(source))='tushare' AND known_at<=:read_at
                ORDER BY logical_fact_key, known_at DESC, fact_version DESC
            )
            SELECT m.id,m.instrument_id,m.source,m.source_code,m.trading_code,
                   m.valid_from,m.valid_to,m.mapping_source,m.evidence,m.known_at,
                   m.observed_at,m.source_revision,m.fact_version,m.logical_fact_key,
                   m.supersedes_fact_id,m.row_version,i.asset_class,i.status,
                   i.xmin::text AS identity_version
            FROM latest m JOIN instruments i ON i.id=m.instrument_id
            WHERE m.valid_from<=:end AND (m.valid_to IS NULL OR m.valid_to>:start)
              AND ''' + selector + '''
            ORDER BY m.instrument_id,m.logical_fact_key LIMIT :row_limit
        '''), {'read_at': read_at, 'start': start, 'end': end, 'row_limit': MAX_IDENTITY_ROWS + 1, **params})
        rows = [dict(row) for row in result.mappings()]
        if len(rows) > MAX_IDENTITY_ROWS or len(native_json(rows).encode()) > MAX_RESPONSE_BYTES:
            raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
        return rows

    def _capture(self, store, keys, identity, read_at, *, deadline: float, final=False) -> dict:
        with store.locks.read_many([self.bindings[k].entry.spec.name for k in keys],
                timeout_ms=store.limits.lock_timeout_ms, cancelled=self.cancelled):
            isolation = 'READ COMMITTED' if final else 'REPEATABLE READ'
            with self.engine.connect().execution_options(isolation_level=isolation) as connection, connection.begin():
                with Session(bind=connection) as session:
                    def checkpoint():
                        if self.cancelled():
                            raise DataStoreError('OPERATION_CANCELLED')
                        remaining = int((deadline - time.monotonic()) * 1000)
                        if remaining <= 0:
                            raise DataStoreError('QUERY_TIMEOUT')
                        session.execute(text("SELECT set_config('statement_timeout',:limit,true)"),
                            {'limit': str(min(5000, remaining))})

                    checkpoint()
                    if final:
                        session.execute(text("SET LOCAL lock_timeout='500ms'"))
                        session.execute(text('LOCK TABLE data_store_datasets,data_store_files,data_store_issues,'
                            'data_store_entry_status,data_store_legacy_restrictions,'
                            'data_store_legacy_maintenance IN SHARE MODE'))
                        if identity:
                            session.execute(text('LOCK TABLE instruments,instrument_code_mappings IN SHARE MODE'))
                    states = {}
                    for key in keys:
                        checkpoint()
                        binding = self.bindings[key]
                        admission = require_request_ready(session, binding.entry)
                        state = session.execute(text('SELECT generation,schema_id,rule FROM data_store_datasets '
                            'WHERE name=:d'), {'d': binding.entry.spec.name}).mappings().first()
                        if state is None:
                            raise DataStoreError('DATASET_MISSING')
                        store._spec_current(binding.entry.spec, state)
                        summary = session.execute(text('SELECT summary_json FROM data_store_entry_status '
                            'WHERE entry_id=:e'), {'e': binding.entry.id}).scalar_one_or_none()
                        try:
                            status = json.loads(summary) if summary else {}
                            overflow = status.get('overflow_restriction', {})
                        except (ValueError, AttributeError, TypeError):
                            raise DataStoreError('CATALOG_UNAVAILABLE') from None
                        # Existing xmin-based current fingerprint includes every
                        # issue/restriction mutation even if generation is unchanged.
                        states[key] = {'generation': str(state['generation']),
                            'schema': state['schema_id'], 'rule': state['rule'],
                            'phase': admission.phase, 'overflow': overflow,
                            'coverage_proof': status.get('full_coverage'),
                            'coverage_pending': status.get('coverage_pending'),
                            'quality': inventory_revision(session, 'issues', dataset=binding.entry.spec.name),
                            'binding': binding.contract_token}
                    checkpoint()
                    mappings = self._identities(session, identity, read_at) if identity else None
                    checkpoint()
                    return {'datasets': states, 'identity': mappings}

    @contextmanager
    def read_scope(self, keys: tuple[str, ...], *, conditions: dict, identity: IdentityQuery | None = None,
                   cursor: str | None = None, read_token: str | None = None) -> Iterator[MarketReadScope]:
        """Domain callers bind all identity/price/factor/calendar dependencies.

        D10 must supply fixed end/as_of in conditions for every continuation;
        D11 includes factor and calendar keys when they affect the result.
        Nothing in the cursor can request a previous generation.
        """
        if (type(keys) is not tuple or not 1 <= len(keys) <= 16 or len(set(keys)) != len(keys)
                or any(key not in self.bindings for key in keys)):
            raise MarketError('CAPABILITY_UNAVAILABLE')
        read_at = datetime.now(timezone.utc)
        deadline = time.monotonic() + 30
        # Freeze the small declared query identity before any I/O or callbacks.
        conditions = json.loads(control_json(conditions, max_bytes=16 * 1024))
        if type(conditions) is not dict:
            raise DataStoreError('INVALID_VALUE')
        with CurrentStore(self.engine, self.settings.data_store_root,
                          cursor_key=self.signing_key, initialize=False) as store:
            captured = self._capture(store, keys, identity, read_at, deadline=deadline)
            scope = MarketReadScope(self, store, keys, conditions, identity, captured, read_at, deadline)
            try:
                previous = _cursor(self.signing_key, cursor, scope.condition) if cursor else None
                if (previous and previous['read_token'] != scope.read_token) or (read_token and read_token != scope.read_token):
                    raise DataStoreError('DATA_CHANGED')
                if previous:
                    scope.position = previous['position']
                scope.check()
                yield scope
                scope.check()
                try:
                    latest = self._capture(store, keys, identity, read_at, deadline=deadline, final=True)
                except (DataStoreError, MarketError) as error:
                    if error.code in {'DATA_STORE_REBUILDING', 'DATASET_MISSING', 'REBUILD_REQUIRED',
                                      'IDENTITY_MAPPING_UNAVAILABLE'}:
                        raise DataStoreError('DATA_CHANGED') from None
                    raise
                if latest != captured:
                    raise DataStoreError('DATA_CHANGED')
                scope.check()
            finally:
                scope.closed = True

    def _probe(self, key: str) -> Capability:
        binding = self.bindings[key]
        try:
            with self.read_scope((key,), conditions={'operation': 'capability'}) as scope:
                # One bounded read of the selected formal namespace, never each
                # instrument's history. This is NOT date/identity-wide acceptance.
                with scope.store.catalog.transaction() as connection:
                    partitions = connection.execute(text('SELECT DISTINCT partition_key FROM data_store_files '
                        'WHERE dataset=:d ORDER BY partition_key LIMIT :n'),
                        {'d': binding.entry.spec.name, 'n': scope.store.limits.query_partitions + 1}).scalars().all()
                if len(partitions) > scope.store.limits.query_partitions:
                    raise DataStoreError('QUERY_BUDGET_EXCEEDED')
                page = scope.read(key, Query(partitions=tuple(partitions) or (binding.partition('', 'current'),),
                    page_size=1, filters=(('representation', '=', binding.representation), ('member_key', '=', 'root'))))
                availability = 'available' if page.rows else 'unavailable'
            return Capability(name=key, availability=availability, metadata_known=True, request_checked=False,
                limitations=[Notice(code='REQUEST_CHECK_REQUIRED', message='已声明本地读取映射；具体标的、日期和质量须逐请求核查。')])
        except DataStoreError as error:
            availability = 'restricted' if error.code in {'DATA_RESTRICTED', 'REBUILD_REQUIRED', 'DATA_STORE_REBUILDING'} else 'unavailable'
            return Capability(name=key, availability=availability, request_checked=False,
                              limitations=[Notice(code=error.code, message=str(error))])

    def capabilities(self) -> CapabilitiesResult:
        results = {key: self._probe(key) for key in self.bindings}
        with Session(self.engine) as session:
            session.execute(text("SET LOCAL statement_timeout='5000ms'"))
            known = session.execute(text("SELECT to_regclass('instruments') IS NOT NULL "
                "AND to_regclass('instrument_code_mappings') IS NOT NULL")).scalar_one()
        results['identity_resolution'] = Capability(name='identity_resolution', availability='unknown' if known else 'unavailable',
            metadata_known=known, request_checked=False, limitations=[Notice(
                code='REQUEST_CHECK_REQUIRED' if known else 'IDENTITY_MAPPING_UNAVAILABLE',
                message='具体代码须核验当前可信映射和合格正式资料。' if known else '可信身份映射资料暂不可用。')])
        # D10 declares only the fixed E70 fund_daily display interpretation.
        # A global namespace probe never admits an arbitrary instrument/window.
        # Keep the formal provider_reported model and S3 execution rules intact.
        from .series import RAW_NOTE
        daily = results[ETF_DAILY]
        uncertain = {'QUERY_BUDGET_EXCEEDED', 'CONTROL_BUDGET_EXCEEDED', 'QUERY_TIMEOUT',
                     'LOCK_TIMEOUT', 'CATALOG_UNAVAILABLE', 'STORAGE_UNAVAILABLE'}
        availability = 'unknown' if any(n.code in uncertain for n in daily.limitations) else daily.availability
        results['raw'] = Capability(name='raw', availability=availability, request_checked=False,
            mapping_implemented=True, metadata_known=daily.metadata_known,
            limitations=[Notice(code='PRICE_BASIS_DECLARED', message=RAW_NOTE),
                Notice(code='SCOPED_RAW_VIEW', message='仅本地正式ETF fund_daily 不复权显示视图；股票、指数及E51未加入此视图。'),
                Notice(code='DERIVED_CALENDAR_REQUIRED', message='周/月/年完整性须另核验正式交易所资料、日历和具体日期；不保证任意窗口可读。'),
                *daily.limitations])
        for name in ('pre', 'post'):
            results[name] = Capability(name=name, availability='unavailable', request_checked=False,
                mapping_implemented=False,
                metadata_known=results[ETF_FACTORS].metadata_known,
                limitations=[Notice(code='FACTOR_ANCHOR_UNVERIFIED', message='已保留因子事实；复权基准、覆盖及锚点策略须独立核验。')])
        return CapabilitiesResult(catalogs=[CatalogOption(catalog_key=ETF_CATALOG,
            label='当前ETF资料', asset_type='etf', capability=results[ETF_CATALOG])],
            capabilities=list(results.values()), read_at=datetime.now(timezone.utc))

    def resolve(self, symbol: str, asset_type: str = 'etf') -> InstrumentResolution:
        symbol = _symbol(symbol)
        if asset_type != 'etf':
            raise MarketError('CAPABILITY_UNAVAILABLE')
        binding = self.bindings[ETF_CATALOG]
        identity = IdentityQuery(symbol, asset_type)
        with self.read_scope((ETF_CATALOG,), conditions={'operation': 'resolve', 'symbol': symbol,
                            'asset_type': asset_type}, identity=identity) as scope:
            rows = scope.captured['identity']
            if not rows:
                raise MarketError('IDENTITY_UNAVAILABLE')
            if len(rows) != 1:
                raise MarketError('IDENTITY_AMBIGUOUS')
            mapping = _mapping(rows[0])
            prefix = (binding.representation, mapping.source_code, mapping.source_code)
            page = scope.read(ETF_CATALOG, Query(partitions=(binding.partition(mapping.source_code, mapping.source_code),),
                lower=prefix, upper=(*prefix[:2], prefix[2] + '\x00'), page_size=2,
                filters=(('representation', '=', binding.representation),)))
            objects = scope.objects(ETF_CATALOG, page)
            if not objects:
                raise MarketError('IDENTITY_UNKNOWN')
            if len(objects) != 1 or page.next_cursor:
                raise MarketError('IDENTITY_AMBIGUOUS')
            if objects[0]['source_code'] != mapping.source_code or objects[0]['asset_type'] != 'fund-etf':
                raise MarketError('IDENTITY_UNAVAILABLE')
            result = InstrumentResolution(instrument_key='instrument:' + str(UUID(str(mapping.instrument_id))),
                symbol=mapping.source_code, asset_type='etf', meta=scope.meta(scope='current ETF identity',
                limitations=[Notice(code='CURRENT_IDENTITY_ONLY', message='身份解析不证明任意历史区间代码映射或价格可读。')]))
            scope.response(result)
        return result


def create_market_service(settings: Settings, engine, *, owner_scope: str, cancelled=None) -> MarketReadService:
    """The single application/domain factory; no success resolver injection."""
    return MarketReadService(settings, engine, owner_scope=owner_scope, cancelled=cancelled)

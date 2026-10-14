"""Atomic source-local publication and optimistic protection against stale jobs."""

from dataclasses import dataclass
from collections import Counter
from datetime import datetime
import json
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, defer

from app.data_ingestion.models.tonghuashun import (
    TonghuashunCollectionState as State, TonghuashunObservation as Observation,
    TonghuashunTicker as Ticker,
)
from app.data_ingestion.tonghuashun.contracts import FUND_TYPES, CollectionError, CollectionConflict, content_hash, exact_json
from app.data_ingestion.tonghuashun.ticker_diagnostics import (
    TickerAssetTypeConflict, DirectoryPublicationRejected, PairPublicationRejected, TARGETS,
)

SERIES_KEYS = {"etf_daily": "date_ms", "stock_daily": "date_ms", "index_daily": "date_ms",
    "fund_nav": "nav_date", "stock_income": "period_end_ms", "stock_balance": "period_end_ms",
    "stock_cash_flow": "period_end_ms", "stock_indicators": "report",
    "fund_performance_history": "date_ms", "rank_trend": "date_ms", "fund_news": "id",
    "fund_stock_history": "report_key", "fund_bond_history": "report_key"}


def materialize(session: Session, observation: Observation) -> dict:
    """Reconstruct exactly one immutable version, never the current head.

    Chains are bounded at publication time. Scope checks also prevent corrupt
    references from mixing symbols, dates or source-local datasets during reads.
    """
    chain, seen, cursor = [], set(), observation
    while True:
        if cursor.id in seen or len(chain) > 30:
            raise CollectionError("采集版本引用异常，无法还原该版本。")
        if (cursor.dataset, cursor.subject, cursor.variant) != (observation.dataset, observation.subject, observation.variant):
            raise CollectionError("采集版本引用了其他数据范围。")
        seen.add(cursor.id)
        chain.append(cursor)
        if cursor.base_observation_id is None:
            break
        cursor = session.get(Observation, cursor.base_observation_id)
        if cursor is None:
            raise CollectionError("采集版本缺少基础快照。")
    data = json.loads(chain.pop().data_json, parse_float=Decimal)
    for version in reversed(chain):
        delta = json.loads(version.data_json, parse_float=Decimal)
        key = delta["key_field"]
        rows = {row[key]: row for row in data["item"]}
        for identity in delta["removed"]:
            rows.pop(identity, None)
        rows.update({row[key]: row for row in delta["upserts"]})
        data = {**delta["metadata"], "item": [rows[k] for k in sorted(rows)]}
    if content_hash(data) != observation.content_hash:
        raise CollectionError("采集版本内容校验失败。")
    return data


def archive_data(dataset: str, data: dict, previous: Observation | None, old: dict | None):
    """Use a delta only when it is materially smaller than a full anchor."""
    full = exact_json(data)
    key = SERIES_KEYS.get(dataset)
    if key and previous and old and previous.chain_depth < 30:
        old_rows = {r[key]: r for r in old["item"]}
        new_rows = {r[key]: r for r in data["item"]}
        delta = {"key_field": key, "metadata": {k: v for k, v in data.items() if k != "item"},
            "upserts": [row for identity, row in new_rows.items() if identity not in old_rows or exact_json(row) != exact_json(old_rows[identity])],
            "removed": sorted(set(old_rows) - set(new_rows))}
        encoded = exact_json(delta)
        if len(encoded) < len(full) * 0.7:
            return encoded, previous.id, previous.chain_depth + 1
    return full, None, 0


@dataclass(frozen=True)
class Previous:
    revision: int
    data: dict | None
    succeeded_at: datetime | None
    status: str
    reconciled_at: datetime | None = None
    attempted_at: datetime | None = None


class CollectionRepository:
    def __init__(self, session: Session):
        self.session = session

    def read(self, dataset: str, subject: str, variant: str, *, with_data: bool = True) -> Previous:
        state = self.session.get(State, (dataset, subject, variant))
        observation = self.session.get(Observation, state.observation_id) if with_data and state and state.observation_id else None
        return Previous(state.revision if state else 0,
            materialize(self.session, observation) if observation else {} if state and state.observation_id else None,
            state.succeeded_at if state else None, state.status if state else "pending",
            state.reconciled_at if state else None, state.attempted_at if state else None)

    def _lock(self, dataset: str, subject: str, variant: str, expected: int, now: datetime):
        insert = pg_insert if self.session.bind.dialect.name == "postgresql" else sqlite_insert
        self.session.execute(insert(State).values(dataset=dataset, subject=subject, variant=variant,
            revision=0, status="pending", attempted_at=now).on_conflict_do_nothing())
        state = self.session.scalar(select(State).where(State.dataset == dataset,
            State.subject == subject, State.variant == variant).with_for_update().execution_options(populate_existing=True))
        if state.revision != expected:
            raise CollectionConflict("同一采集范围已被其他运行更新，本次结果未覆盖较新版本。")
        return state

    def publish(self, dataset: str, subject: str, variant: str, *, expected: int,
                data: dict, requests: list[dict], now: datetime, ticker_rows: list[dict] | None = None,
                reconcile: bool = False, verify_category_change: bool = False,
                observed_at: datetime | None = None, _paired_heads=None):
        source_time = observed_at or now
        directory_scope = dataset == 'tickers' and variant == 'default' and subject in FUND_TYPES
        verify_category_change = bool(verify_category_change and directory_scope)
        if directory_scope:
            # Order the existing finite category-head locks BEFORE the ordinary
            # scope CAS. Supplier I/O remains outside publication transactions.
            # This freezes counterpart originals without opposing lock orders.
            self.session.scalars(select(State).where(State.dataset == 'tickers',
                State.variant == 'default', State.subject.in_(FUND_TYPES))
                .order_by(State.subject).with_for_update()
                .execution_options(populate_existing=True)).all()
        state = self._lock(dataset, subject, variant, expected, now)
        previous = self.session.get(Observation, state.observation_id) if state.observation_id else None
        old = materialize(self.session, previous) if previous else None
        if directory_scope:
            # Every ordinary, replayed and paired directory must pass the same
            # admission under the ordered locks and expected revision CAS.
            # Reuse the exact materialized old head; do not stage an original or
            # change a registry row before rejecting a regressing snapshot.
            self._admit_directory_head(state, data, original=previous, current_data=old)
        digest = content_hash(data)
        row_count = len(data.get("item", data.get("abilities", [])))
        old_rows = old.get("item", []) if old else []
        old_counts = Counter(content_hash(row) for row in old_rows)
        new_counts = Counter(content_hash(row) for row in data.get("item", []))
        unchanged = sum((old_counts & new_counts).values())
        removed = sum((old_counts - new_counts).values())
        encoded, base_id, depth = archive_data(dataset, data, previous, old)
        version = Observation(id=uuid4(), dataset=dataset, subject=subject, variant=variant,
            observed_at=source_time, request_json=exact_json(requests), data_json=encoded,
            content_hash=digest, row_count=row_count, base_observation_id=base_id, chain_depth=depth)
        if _paired_heads is not None:
            # publish_pair alone stages these two complete native originals in
            # this same transaction, before either registry change is allowed.
            if (not verify_category_change or subject not in TARGETS
                    or set(_paired_heads) != set(TARGETS)):
                raise PairPublicationRejected('pair_publication', 'scope_mismatch', subject)
            version = _paired_heads[subject]
            if ((version.dataset, version.subject, version.variant) != (dataset, subject, variant)
                    or version.content_hash != digest or version.observed_at != source_time
                    or version.request_json != exact_json(requests)
                    or version.data_json != exact_json(data)):
                raise PairPublicationRejected('pair_publication', 'original_hash_mismatch', subject,
                    actual_content_hash=digest, actual_request_hash=content_hash(requests))
        else:
            self.session.add(version)
            self.session.flush()
        if ticker_rows is not None:
            from .directory_proof import complete_directory, category_change_proven, proof_context_bytes
            positive, proof_checked, original_memo = None, False, {}
            # Missing rows are intentionally retained. Absence from today's
            # catalogue cannot establish delisting or identifier retirement.
            for row, locked in self._locked_ticker_rows(ticker_rows):
                ticker = locked.get(row['thscode'])
                if ticker is None:
                    ticker = Ticker(thscode=row["thscode"], first_seen_at=source_time)
                    self.session.add(ticker)
                    locked[row['thscode']] = ticker
                elif ticker.asset_type != row["asset_type"]:
                    if verify_category_change and ticker.asset_type in FUND_TYPES:
                        if not proof_checked:
                            positive = complete_directory(subject, data, requests, source_time, digest)
                            original_memo['bytes'] = proof_context_bytes(positive)
                            proof_checked = True
                        if category_change_proven(self.session, ticker, positive, original_memo,
                                                  paired_heads=_paired_heads):
                            ticker.asset_type = row['asset_type']
                    if ticker.asset_type != row['asset_type'] and dataset == 'tickers' and variant == 'default' and subject in TARGETS:
                        # Stop at the first unproven conflict. The bounded
                        # diagnostic contains no response body or credentials.
                        raise TickerAssetTypeConflict(subject, source_code=row['thscode'],
                            old_asset_type=ticker.asset_type, new_asset_type=row['asset_type'],
                            provider_snapshot=data.get('timestamp'), directory_content_hash=digest)
                    elif ticker.asset_type != row['asset_type']:
                        raise CollectionError("已有标的的资产类型发生冲突，需要核对原始数据。")
                ticker.asset_type, ticker.name = row["asset_type"], row.get("name")
                ticker.exchange, ticker.raw_json = row.get("exchange"), exact_json(row)
                ticker.last_seen_at = source_time
        incomplete = bool(data.get("failed_requests"))
        state.observation_id = version.id
        state.status = "partial" if incomplete else "succeeded" if row_count else "empty"
        state.attempted_at = now
        if not incomplete:
            state.succeeded_at = now
        if reconcile and not incomplete:
            state.reconciled_at = now
        state.error_kind, state.revision = "partial_reports" if incomplete else None, state.revision + 1
        return {"version_id": str(version.id), "received": row_count,
                "changed": row_count - unchanged, "unchanged": unchanged, "removed": removed}

    def _locked_ticker_rows(self, rows):
        # Keep the same ORM changes and outer publication transaction. A finite
        # ordered lock batch avoids a SELECT and autoflush for every native row.
        # Newly created rows stay in the batch map, including duplicate keys.
        from .directory_proof import check_publication
        for offset in range(0, len(rows), 256):
            check_publication()
            batch = rows[offset:offset + 256]
            locked = {ticker.thscode: ticker for ticker in self.session.scalars(
                select(Ticker).where(Ticker.thscode.in_([row['thscode'] for row in batch]))
                .options(defer(Ticker.raw_json))
                .order_by(Ticker.thscode).with_for_update()
                .execution_options(populate_existing=True))}
            # A stale registry row may contain a large historical native body.
            # Load its exact raw claim only when category proof needs it. Expire
            # cached claims under the acquired lock before any deferred read.
            for ticker in locked.values():
                self.session.expire(ticker, ['raw_json'])
            for row in batch:
                yield row, locked

    def publish_pair(self, entries, *, now):
        """Atomically publish exactly two mutually blocked complete directories.

        This is one normal-batch transaction, not a candidate ledger. Every
        source/revision and the exact old registry claim remains independently
        checked. Any further conflict rolls both originals and both heads back.
        The caller commits only after this method returns both results.
        """
        from .directory_proof import release_exception_frames
        phase, subject = 'pair_head_admission', None
        ordered = states = heads = results = entry = data = requests = None
        try:
            if len(entries) != 2 or {entry.subject for entry in entries} != set(TARGETS):
                raise PairPublicationRejected(phase, 'scope_mismatch')
            ordered = sorted(entries, key=lambda entry: entry.subject)
            self.session.scalars(select(State).where(State.dataset == 'tickers',
                State.variant == 'default', State.subject.in_(FUND_TYPES))
                .order_by(State.subject).with_for_update()
                .execution_options(populate_existing=True)).all()
            states = {}
            for entry in ordered:
                subject = entry.subject
                try:
                    states[subject] = self._lock('tickers', subject, 'default', entry.revision, now)
                except CollectionConflict as error:
                    current = self.session.get(State, ('tickers', subject, 'default'))
                    raise PairPublicationRejected(phase, 'revision_changed', subject,
                        current_revision=current.revision if current else None) from error
            # Both revision CAS checks and all ordered head locks precede any
            # staged original. Current-head proof never coexists with the
            # two candidate membership sets retained by the next helper.
            for entry in ordered:
                subject = entry.subject
                data, requests, _, _ = self._pair_payload(entry, phase)
                self._admit_directory_head(states[subject], data)
                del data, requests
            phase, subject = 'pair_membership_validation', None
            self._pair_memberships(ordered)
            # The helper releases ALL catalogue aliases, even on exceptions.
            # Normal publication now gets the existing 32 MiB proof half alone.
            phase, heads = 'pair_original_staging', {}
            for entry in ordered:
                subject = entry.subject
                data, requests, encoded, receipt = self._pair_payload(entry, phase)
                heads[subject] = Observation(id=uuid4(), dataset='tickers', subject=subject,
                    variant='default', observed_at=entry.observed_at, request_json=receipt,
                    data_json=encoded, content_hash=content_hash(data), row_count=len(data['item']),
                    base_observation_id=None, chain_depth=0)
                self.session.add(heads[subject])
                del data, requests
            self.session.flush()
            phase, results = 'pair_publication', {}
            for entry in ordered:
                subject = entry.subject
                data, requests = entry.payload()
                results[subject] = self.publish('tickers', subject, 'default',
                    expected=entry.revision, data=data, requests=requests, now=now,
                    observed_at=entry.observed_at, ticker_rows=data['item'],
                    verify_category_change=True, _paired_heads=heads)
                del data, requests
            return results
        except CollectionError as error:
            release_exception_frames(error)
            if isinstance(error, PairPublicationRejected):
                raise
            if isinstance(error, DirectoryPublicationRejected):
                raise PairPublicationRejected(phase, error.reason, error.subject, **error.context) from error
            reason = ('revision_changed' if isinstance(error, CollectionConflict) else
                      'unproven_category_change' if isinstance(error, TickerAssetTypeConflict) else
                      'publication_rejected')
            raise PairPublicationRejected(phase, reason, subject,
                ticker_conflict=error.diagnostic if isinstance(error, TickerAssetTypeConflict) else None) from error
        except BaseException as error:
            release_exception_frames(error)
            raise
        finally:
            # Saved exceptions may retain this active frame. Drop every owner
            # here as well as clearing already-unwound helper/publisher frames.
            entries = ordered = states = heads = results = entry = data = requests = None
            encoded = receipt = head = current = snapshot = None

    def _pair_payload(self, entry, phase):
        from .directory_proof import NATIVE_BYTES
        try:
            data, requests = entry.payload()
        except (CollectionError, ValueError, TypeError) as error:
            reason = ('incomplete_original' if entry.data is None and entry.requests is None
                      and entry.packed is None else 'original_hash_mismatch')
            raise PairPublicationRejected(phase, reason, entry.subject) from error
        digest, request_hash = content_hash(data), content_hash(requests)
        if digest != entry.conflict['directory_content_hash'] or request_hash != entry.request_hash:
            raise PairPublicationRejected(phase, 'original_hash_mismatch', entry.subject,
                actual_content_hash=digest, actual_request_hash=request_hash)
        encoded, receipt = exact_json(data), exact_json(requests)
        if max(len(encoded.encode()), len(receipt.encode())) > NATIVE_BYTES:
            raise PairPublicationRejected(phase, 'incomplete_original', entry.subject)
        return data, requests, encoded, receipt

    def _admit_directory_head(self, state, data, *, original=None, current_data=None):
        """One monotonic admission for all fund-directory publication paths."""
        current = self._committed_directory_snapshot(state, original=original, data=current_data)
        snapshot = data.get('timestamp')
        if current is not None and (type(snapshot) is not int or not current <= snapshot < 2**63):
            head = self.session.get(Observation, state.observation_id)
            raise DirectoryPublicationRejected('snapshot_regression', state.subject,
                current_snapshot=current, current_revision=state.revision, current_head_hash=head.content_hash)

    def _committed_directory_snapshot(self, state, *, original=None, data=None):
        from .directory_proof import complete_directory, NATIVE_BYTES
        requests = catalogue = None
        try:
            if state.observation_id is None:
                return None
            original = original or self.session.get(Observation, state.observation_id)
            if (original is None or (original.dataset, original.subject, original.variant) !=
                    ('tickers', state.subject, 'default')
                    or max(len(original.data_json.encode()), len(original.request_json.encode())) > NATIVE_BYTES):
                raise DirectoryPublicationRejected('committed_head_unverifiable', state.subject)
            if data is None:
                data = materialize(self.session, original)
            requests = json.loads(original.request_json, parse_float=Decimal)
            catalogue = complete_directory(state.subject, data, requests, original.observed_at, original.content_hash)
            if catalogue is None:
                raise DirectoryPublicationRejected('committed_head_unverifiable', state.subject)
            return catalogue.version
        except CollectionError as error:
            if isinstance(error, DirectoryPublicationRejected):
                raise
            raise DirectoryPublicationRejected('committed_head_unverifiable', state.subject) from error
        finally:
            original = data = requests = catalogue = None

    def _pair_memberships(self, entries):
        from .directory_proof import complete_directory, CONTEXT_BYTES, _bounded_size
        first = second = catalogue = data = requests = None
        try:
            for entry in entries:
                data, requests = entry.payload()
                catalogue = complete_directory(entry.subject, data, requests, entry.observed_at)
                if catalogue is None:
                    raise PairPublicationRejected('pair_membership_validation', 'incomplete_original', entry.subject)
                if first is None:
                    first = catalogue
                else:
                    second = catalogue
            if _bounded_size((first.codes, second.codes), CONTEXT_BYTES // 2 - 4096) is None:
                raise PairPublicationRejected('pair_membership_validation', 'context_budget_exceeded')
            if first.version != second.version:
                raise PairPublicationRejected('pair_membership_validation', 'snapshot_mismatch')
            if not first.codes.isdisjoint(second.codes):
                raise PairPublicationRejected('pair_membership_validation', 'membership_overlap')
        finally:
            first = second = catalogue = data = requests = None

    def fail(self, dataset: str, subject: str, variant: str, *, expected: int,
             kind: str, now: datetime):
        state = self._lock(dataset, subject, variant, expected, now)
        state.status, state.attempted_at, state.error_kind = "failed", now, kind
        state.revision += 1
        # Last-good observation and succeeded_at remain untouched, so a failed
        # refresh never becomes an empty collection or a successful checkpoint.

    def subjects(self, assets: tuple[str, ...], *, current_only: bool = False) -> list[str]:
        rows = self.session.execute(select(Ticker.thscode, Ticker.asset_type).where(
            Ticker.asset_type.in_(assets)).order_by(Ticker.thscode)).all()
        if not current_only:
            return [code for code, _ in rows]
        # Retaining historical identities is not evidence that the provider
        # still accepts them. Automatic collection follows each asset's last
        # complete directory snapshot; explicit historical requests retain
        # access to old identities. A failed refresh uses the last good head,
        # and installations without directory snapshots keep their seed scope.
        current = {}
        for asset in assets:
            previous = self.read('tickers', asset, 'default')
            if previous.data is not None:
                current[asset] = {row['thscode'] for row in previous.data['item']}
        return [code for code, asset in rows if asset not in current or code in current[asset]]

    def related(self, field: str) -> list[str]:
        observations = self.session.scalars(select(Observation).join(State,
            State.observation_id == Observation.id).where(State.dataset == "fund_profile"))
        identifiers = set()
        for observation in observations:
            for row in json.loads(observation.data_json).get("item", []):
                values = ([row.get("company_id")] if field == "company_id" else
                          [m.get("manager_id") for m in row.get("manager_info", []) if isinstance(m, dict)])
                for value in values:
                    if isinstance(value, str) and value and len(value) <= 64:
                        identifiers.add(value)
        return sorted(identifiers)

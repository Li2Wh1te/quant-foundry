"""Complete-directory evidence and bounded reuse during one normal batch.

No page journal, cross-run cache, candidate table or supplier request is added.
Membership uses the complete, contiguous native-directory contract. The optional reuse
fits inside its existing 64 MiB ephemeral-context ceiling; native originals keep
their existing 32 MiB parsing ceiling. Exceeding either cannot relax proof.
"""
from dataclasses import dataclass, replace
from datetime import UTC
import sys

from sqlalchemy import select

from app.data_ingestion.models.tonghuashun import (
    TonghuashunCollectionState as State, TonghuashunObservation as Observation,
)
from .contracts import ASSET_TYPES, CollectionError, content_hash, exact_json, validate_ticker
from .acquisition import Acquisition, DirectorySnapshotChanged
from .ticker_diagnostics import TARGETS

NATIVE_BYTES = 32 * 1024 * 1024
CONTEXT_BYTES = 64 * 1024 * 1024


class DirectoryAcquisition(Acquisition):
    """Preserve only one complete attempt's contiguous request receipts."""

    def read(self, interface, params):
        data = super().read(interface, params)
        if interface == 'meta.tickers.list':
            snapshot, rows = data.get('timestamp'), data.get('item')
            if type(snapshot) is not int or not 0 <= snapshot < 2**63:
                raise CollectionError('标的目录缺少有效的上游快照时间。')
            if (data.get('complete') is False or data.get('next_cursor') not in (None, '')
                    or data.get('failed_requests') or (data.get('has_more') is True
                    and isinstance(rows, list) and len(rows) < params['limit'])):
                raise CollectionError('标的目录分页不完整，本次未推进完成标记。')
        return data

    def directory(self, spec, asset):
        for attempt in range(3):
            start = len(self.requests)
            try:
                return self._directory_snapshot(spec, asset)
            except DirectorySnapshotChanged:
                # Discard failed-attempt proof without hiding actual reads.
                del self.requests[start:]
                if attempt == 2:
                    raise
        raise AssertionError('unreachable directory attempt')

@dataclass(frozen=True, slots=True)
class _Catalogue:
    source: object
    version: int
    codes: frozenset[str]


def _complete_catalogue(source, requests):
    if ((source.source, source.dataset, source.variant) != ('tonghuashun', 'tickers', 'default')
            or source.subject not in ASSET_TYPES or source.failure or source.order_kind != 'observation'):
        return None
    from app.data_store.adapters.record_adapters import convert
    body = source.content
    if not isinstance(body, dict) or not isinstance(body.get('item'), list) or not body['item']:
        return None
    version = body.get('timestamp')
    if (type(version) is not int or not 0 <= version < 2**63
            or body.get('complete') is False or body.get('has_more') is True
            or body.get('next_cursor') not in (None, '') or body.get('failed_requests')):
        return None
    if not isinstance(requests, list) or not requests or len(requests) > 100:
        return None
    size = None
    for index, request in enumerate(requests):
        if not isinstance(request, dict) or request.get('interface') != 'meta.tickers.list':
            return None
        parameters = request.get('parameters')
        if not isinstance(parameters, dict) or parameters.get('asset_type') != source.subject:
            return None
        limit, offset = parameters.get('limit'), parameters.get('offset')
        if (type(limit) is not int or not 0 < limit <= 10_000
                or type(offset) is not int or offset != index * limit
                or size is not None and size != limit):
            return None
        size = limit
    # The checked collector publishes one immutable anchor only after a
    # terminal short page and a consistent provider version on every page.
    # Contiguous request offsets plus the preserved total must agree with
    # that terminal page; one full physical page cannot prove absence.
    if not (len(requests) - 1) * size <= len(body['item']) < len(requests) * size:
        return None
    codes = set()
    for raw in body['item']:
        if not isinstance(raw, dict):
            return None
        validate_ticker(raw, source.subject)
        converted = convert(source, raw)
        code = converted['body']['source_code']
        if code in codes:
            return None
        codes.add(code)
    return _Catalogue(replace(source, content={}), version, frozenset(codes))


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def complete_directory(asset, data, requests, observed_at, digest=None):
    # Names, a requested range and incomplete pages never prove membership.
    from app.data_store.adapters.canonical import NativeInputError
    from app.data_store.adapters.contracts import LocalInput
    try:
        source = LocalInput('tonghuashun', 'tickers', asset, 'default',
                            aware(observed_at), data, digest or content_hash(data))
        catalogue = _complete_catalogue(source, requests)
        if catalogue is None or _bounded_size(catalogue.codes, CONTEXT_BYTES // 2 - 1024) is None:
            return None
        return catalogue
    except (CollectionError, NativeInputError, ValueError, TypeError, KeyError):
        return None


def _original(session, observation, memo, *, with_claims=False):
    key = (observation.id, with_claims)
    if key not in memo:
        remaining = CONTEXT_BYTES // 2 - memo.get('bytes', 0) - 1024
        if remaining < 0:
            return None
        from .repository import materialize
        import json
        from decimal import Decimal
        if (len(observation.data_json.encode()) > NATIVE_BYTES
                or len(observation.request_json.encode()) > NATIVE_BYTES):
            memo[key] = None
            memo['bytes'] = memo.get('bytes', 0) + 1024
        else:
            try:
                body = materialize(session, observation)
                requests = json.loads(observation.request_json, parse_float=Decimal)
                complete = complete_directory(observation.subject, body, requests,
                                              observation.observed_at, observation.content_hash)
                # Keep membership and exact row digests, never native bodies.
                hashes = ({row['thscode']: content_hash(row) for row in body['item']}
                          if complete and with_claims else {})
                size = _bounded_size((complete.codes, hashes), remaining) if complete else None
                memo[key] = (complete, hashes) if size is not None else None
                memo['bytes'] = memo.get('bytes', 0) + 1024 + (size or 0)
            except (CollectionError, ValueError, TypeError, KeyError):
                memo[key] = None
                memo['bytes'] = memo.get('bytes', 0) + 1024
    return memo[key]


def category_change_proven(session, ticker, positive, memo, *, paired_heads=None):
    """Exact old claim plus complete same-snapshot positive/negative originals.

    The caller holds category-head and ticker locks. No stale, ambiguous or
    mismatching source can authorize a registry change. A name is never a class.
    """
    if positive is None or ticker.thscode not in positive.codes:
        return False
    negative = (paired_heads or {}).get(ticker.asset_type)
    if negative is None:
        state = session.get(State, ('tickers', ticker.asset_type, 'default'))
        negative = session.get(Observation, state.observation_id) if state and state.observation_id else None
    if (negative is None or (negative.dataset, negative.subject, negative.variant) !=
            ('tickers', ticker.asset_type, 'default')):
        return False
    checked = _original(session, negative, memo)
    if checked is None:
        return False
    absence, _ = checked
    if (absence.version != positive.version or ticker.thscode in absence.codes
            or not aware(ticker.last_seen_at) < aware(negative.observed_at)
            or not aware(ticker.last_seen_at) < aware(positive.source.observed_at)):
        return False
    # Registry last_seen_at was written atomically with its actual source row.
    # Equality scopes this bounded lookup; UUID ordering cannot choose a claim.
    prior = session.scalars(select(Observation).where(
        Observation.dataset == 'tickers', Observation.subject == ticker.asset_type,
        Observation.variant == 'default', Observation.observed_at == ticker.last_seen_at
    ).limit(2)).all()
    if len(prior) != 1:
        return False
    checked = _original(session, prior[0], memo, with_claims=True)
    if checked is None:
        return False
    former, hashes = checked
    import hashlib
    return (former.version < positive.version and ticker.thscode in former.codes
            and hashes.get(ticker.thscode) == hashlib.sha256(ticker.raw_json.encode()).hexdigest())


def _bounded_size(value, remaining):
    # Counting aliases twice is conservative; no unbounded visited-object index.
    size = sys.getsizeof(value)
    if size > remaining:
        return None
    children = (value.items() if isinstance(value, dict) else
                value if isinstance(value, (list, tuple, set, frozenset)) else ())
    for child in children:
        cost = _bounded_size(child, remaining - size)
        if cost is None:
            return None
        size += cost
    return size


def _bounded_json(value, maximum):
    """The exact_json contract, stopping before an oversized retained copy."""
    def tokens(item):
        if isinstance(item, dict):
            yield '{'
            for index, (key, child) in enumerate(sorted(item.items())):
                if index:
                    yield ','
                yield exact_json(key)
                yield ':'
                yield from tokens(child)
            yield '}'
        elif isinstance(item, (list, tuple)):
            yield '['
            for index, child in enumerate(item):
                if index:
                    yield ','
                yield from tokens(child)
            yield ']'
        else:
            yield exact_json(item)
    result = bytearray()
    for token in tokens(value):
        if len(token) > maximum - len(result):
            return None
        encoded = token.encode()
        if len(encoded) > maximum - len(result):
            return None
        result.extend(encoded)
    return bytes(result)


@dataclass(slots=True)
class _Replay:
    subject: str
    revision: int
    data: dict | None
    requests: list | None
    observed_at: object
    fetched_count: int
    conflict: dict
    request_hash: str
    size: int
    packed: tuple[bytes, bytes] | None = None

    def payload(self):
        if self.data is not None and self.requests is not None:
            return self.data, self.requests
        if self.packed is None:
            raise CollectionError('目录复用原件已释放。')
        import json
        from decimal import Decimal
        data = json.loads(self.packed[0], parse_float=Decimal)
        requests = json.loads(self.packed[1], parse_float=Decimal)
        if (content_hash(data) != self.conflict['directory_content_hash']
                or content_hash(requests) != self.request_hash):
            raise CollectionError('目录复用原件与请求回执不一致。')
        return data, requests

    def release(self):
        # ready() tuples and traceback frames may still hold this owner. Drop
        # its borrowed payload references as well as removing the map entry;
        # never mutate the original body, rows or request receipts themselves.
        self.data = None
        self.requests = None
        self.packed = None


class DirectoryReplays:
    """At most the two E40 target directories, released with this invocation."""
    def __init__(self):
        self.entries = {}
        self.bytes = 0
        self.used = False

    def retain(self, subject, revision, data, requests, observed_at, fetched_count, conflict):
        if (subject not in TARGETS or subject in self.entries or not conflict
                or complete_directory(subject, data, requests, observed_at,
                                      conflict.get('directory_content_hash')) is None):
            return False
        # Split the existing 64 MiB context ceiling with publication proofs.
        # A fixed 4 KiB also reserves the at-most-two entry/diagnostic/hash keys.
        remaining = CONTEXT_BYTES // 2 - self.bytes - 4096
        if remaining < 0:
            return False
        size = _bounded_size((data, requests), remaining)
        packed = None
        if size is None:
            # A full OTC catalogue may exceed the retained-object ceiling as
            # Python dictionaries while its exact native JSON fits comfortably.
            # Retain only those immutable bytes; decode a normal publication
            # working payload when needed. Both existing byte ceilings remain.
            packed = (_bounded_json(data, NATIVE_BYTES), _bounded_json(requests, NATIVE_BYTES))
            if any(item is None for item in packed):
                return False
            size = _bounded_size(packed, remaining)
            if size is None:
                return False
        size += 4096
        self.entries[subject] = _Replay(subject, revision,
                                        None if packed else data, None if packed else requests,
                                        observed_at, fetched_count, dict(conflict),
                                        content_hash(requests), size, packed)
        self.bytes += size
        self.used = True
        return True

    def ready(self, asset, data, requests, observed_at):
        negative = complete_directory(asset, data, requests, observed_at)
        if negative is None:
            return ()
        ready = []
        for entry in self.entries.values():
            if (entry.conflict['old_asset_type'] != asset
                    or entry.conflict['provider_snapshot'] != negative.version
                    or entry.conflict['source_code'] in negative.codes):
                continue
            try:
                original, receipt = entry.payload()
            except (CollectionError, ValueError, TypeError):
                continue
            if (content_hash(original) == entry.conflict['directory_content_hash']
                    and content_hash(receipt) == entry.request_hash):
                ready.append(entry)
        return tuple(ready)

    def pair(self):
        """Only a complete reciprocal ETF/OTC pair can break the head cycle."""
        if set(self.entries) != set(TARGETS):
            return ()
        entries = tuple(self.entries[asset] for asset in TARGETS)
        for entry, counterpart in (entries, entries[::-1]):
            try:
                data, requests = counterpart.payload()
            except (CollectionError, ValueError, TypeError):
                return ()
            if entry not in self.ready(counterpart.subject, data, requests, counterpart.observed_at):
                return ()
        return entries

    def remove(self, subject):
        entry = self.entries.pop(subject)
        self.bytes -= entry.size
        entry.release()

    def clear(self):
        for entry in self.entries.values():
            entry.release()
        self.entries.clear()
        self.bytes = 0


def proof_context_bytes(catalogue):
    return _bounded_size(catalogue.codes, CONTEXT_BYTES // 2 - 1024) + 1024 if catalogue else 0


def release_exception_frames(error):
    """Release borrowed objects while preserving exception identities/stacks."""
    import traceback
    pending, cleared = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in cleared:
            continue
        cleared.add(id(current))
        traceback.clear_frames(current.__traceback__)
        pending.extend(chain for chain in (current.__cause__, current.__context__)
                       if chain is not None)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)


def check_publication():
    """Check the existing scheduler control at bounded publication boundaries.

    The last allowed request may still publish; the request cap governs new
    supplier reads. Cancellation and elapsed-time expiry always abort writes.
    """
    import time
    from .control import CollectionYield, control
    monitor = control()
    if monitor is None:
        return
    try:
        monitor.check()
    except CollectionYield as error:
        if (error.reason == 'budget' and time.monotonic() < monitor.deadline
                and monitor.detail['requests'] <= monitor.max_requests):
            return
        raise

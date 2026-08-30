"""ETF cash-dividend ingestion primitives."""
from datetime import date
from uuid import uuid4, uuid5, NAMESPACE_URL
from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.schemas.corporate_action import normalize_fund_div_row

FUND_DIV_STATUS_VERSION = "tushare_fund_div_status@1"
FUND_DIV_CASH_DATE_VERSION = "tushare_fund_div_cash_date@1"

def fetch_fund_div(client: TushareClient, *, ann_date: str | None = None, ts_code: str | None = None,
                   start_date: str | None = None, end_date: str | None = None):
    return client.fund_div(ann_date=ann_date, ts_code=ts_code, start_date=start_date, end_date=end_date)

def normalize_fund_div(rows):
    """Normalize rows while preserving invalid source values for audit."""
    if hasattr(rows, "to_dict"): rows = rows.to_dict("records")
    return [normalize_fund_div_row(dict(r)) for r in rows]

def logical_fact_key(item) -> str:
    raw = getattr(item, "raw", {})
    return "tushare:fund_div:fund_div:{code}:{ann}:{year}:{base}".format(
        code=item.ts_code, ann=item.ann_date or raw.get("ann_date"),
        year=raw.get("base_year") or "", base=raw.get("base_date") or "")

def detect_logical_key_conflicts(items):
    """Return keys whose candidate identity maps to differing source payloads."""
    grouped = {}
    for item in items:
        key = logical_fact_key(item)
        grouped.setdefault(key, set()).add(item.source_hash)
    return tuple(key for key, hashes in grouped.items() if len(hashes) > 1)

def cash_effective_date(item, *, next_open_session=None):
    d = item.payment_date
    if d is None: return None
    return next_open_session(d) if next_open_session else d

def derive_cash_effective_session(item, *, calendar_id, timezone_name, calendar_definition, next_open_session):
    """Map payment date to the named calendar's same/next open session."""
    if not calendar_id or not timezone_name or not calendar_definition:
        raise ValueError("corporate_action_calendar_unresolved")
    payment = cash_effective_date(item)
    if payment is None:
        raise ValueError("corporate_action_cash_date_unresolved")
    try:
        session = next_open_session(calendar_id, payment)
    except TypeError:
        session = next_open_session(payment)
    if session is None:
        raise ValueError("corporate_action_cash_date_unresolved")
    return session

def sync_fund_div(client, *, session=None, checkpoint_repo=None, sync_key=None,
                  scope_key="fund_div", checkpoint_cursor=None,
                  instrument_map=None, **kwargs):
    """Fetch and optionally persist one atomic synchronization unit.

    Persistence is deliberately duck-typed so worker tests can provide a fake
    session; callers own the surrounding transaction and commit/rollback.
    """
    rows = fetch_fund_div(client, **kwargs)
    raw_rows = rows.to_dict("records") if hasattr(rows, "to_dict") else [dict(r) for r in rows]
    # Save the complete response, including an empty response, before parsing.
    if session is not None:
        from app.data_ingestion.models.corporate_action import CorporateActionSourceFact
        snap = CorporateActionSourceFact(id=uuid4(), source="tushare", endpoint="fund_div",
            query_kind="ann_date" if kwargs.get("ann_date") else "ts_code" if kwargs.get("ts_code") else "range",
            query_value=kwargs.get("ann_date") or kwargs.get("ts_code"), ts_code=kwargs.get("ts_code") or "*",
            ann_date=None, payload={"rows": raw_rows}, source_hash=__import__("hashlib").sha256(
                __import__("json").dumps(raw_rows, sort_keys=True, default=str).encode()).hexdigest())
        session.add(snap)
    items = normalize_fund_div(raw_rows)
    conflicts = detect_logical_key_conflicts(items)
    failed = len(conflicts)
    persisted = 0
    if session is not None and instrument_map is not None:
        from app.data_ingestion.models.corporate_action import CorporateActionFact
        for item in items:
            if item.status != "implemented":
                failed += 1
                continue
            instrument_id = instrument_map.get(item.ts_code)
            if instrument_id is None:
                failed += 1
                continue
            key = logical_fact_key(item)
            previous = None
            try:
                previous = session.query(CorporateActionFact).filter_by(logical_fact_key=key).order_by(CorporateActionFact.fact_version.desc()).first()
            except Exception:
                previous = None
            if previous is not None and previous.evidence.get("source_hash") == item.source_hash:
                continue
            version = (previous.fact_version + 1) if previous is not None else 1
            fact = CorporateActionFact(event_id=uuid4(), logical_fact_key=key,
                fact_version=version, supersedes_fact_id=(previous.event_id if previous else None),
                instrument_id=instrument_id, action_type="cash_dividend", record_date=item.record_date,
                ex_date=item.ex_date, source_payment_date=item.source_payment_date_raw,
                source_arrival_date=item.source_arrival_date_raw, cash_effective_date=item.payment_date,
                cash_effective_phase="after_open_match", cash_amount_per_unit=item.amount,
                currency=item.currency, cash_date_rule=item.cash_date_rule,
                source="tushare", quality="complete", evidence={"source_hash": item.source_hash,
                "status_rule": FUND_DIV_STATUS_VERSION, "raw": item.raw})
            session.add(fact); persisted += 1
    advanced = False
    if checkpoint_repo is not None and sync_key and not failed:
        checkpoint_repo.advance(sync_key=sync_key, scope_key=scope_key,
                                cursor=checkpoint_cursor or {}, expected_version=None)
        advanced = True
    return {"items": items, "fetched": len(raw_rows), "changed": persisted or max(0, len(items)-failed),
            "unchanged": 0, "failed": failed, "skipped_non_target": 0,
            "checkpoint_advanced": advanced, "conflicts": conflicts}


def sync_fund_div_full(client, *, ts_codes, session=None, checkpoint_repo=None,
                       sync_key=None, instrument_map=None, **kwargs):
    """Scan every resolved ETF source code and commit only after all succeed.

    The helper intentionally treats a page at the client limit as potentially
    truncated; callers must paginate or report failure instead of claiming
    complete coverage.  A checkpoint is advanced once for the aggregate batch.
    """
    aggregate = {"items": [], "fetched": 0, "changed": 0, "unchanged": 0,
                 "failed": 0, "skipped_non_target": 0, "checkpoint_advanced": False,
                 "conflicts": []}
    for code in tuple(ts_codes):
        result = sync_fund_div(client, session=session, instrument_map=instrument_map,
                               ts_code=code, **kwargs)
        for key in ("items", "conflicts"):
            aggregate[key].extend(result.get(key, ()))
        for key in ("fetched", "changed", "unchanged", "failed", "skipped_non_target"):
            aggregate[key] += int(result.get(key, 0) or 0)
    if session is not None and aggregate["failed"] == 0 and instrument_map:
        from app.data_ingestion.models.corporate_action import CorporateActionCoverageFact
        by_instrument = {}
        for item in aggregate["items"]:
            iid = instrument_map.get(item.ts_code)
            if iid is not None:
                by_instrument[iid] = by_instrument.get(iid, 0) + 1
        for iid in set(instrument_map.values()):
            session.add(CorporateActionCoverageFact(
                id=uuid4(), instrument_id=iid, action_type="cash_dividend",
                start_date=kwargs.get("start_date") or date.min,
                end_date=kwargs.get("end_date") or date.today(),
                status="complete", event_count=by_instrument.get(iid, 0),
                evidence={"query_kind": "ts_code", "source": "tushare", "ts_codes": list(ts_codes)},
                validation_rule="tushare_fund_div_coverage@1", summary={"full_scan": True},
            ))
    if checkpoint_repo is not None and sync_key and aggregate["failed"] == 0:
        checkpoint_repo.advance(sync_key=sync_key, scope_key="fund_div",
                                cursor={"ts_codes": list(ts_codes)}, expected_version=None)
        aggregate["checkpoint_advanced"] = True
    return aggregate

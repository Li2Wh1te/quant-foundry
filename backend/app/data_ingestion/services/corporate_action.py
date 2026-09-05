"""ETF cash-dividend ingestion primitives.

The ingestion boundary preserves the source response, then materializes a
normalized fact only after a caller supplies the named-calendar resolver that
will produce the cash-effective session.  A missing resolver is deliberately a
hard failure: falling back to the source payment date would create an
unqualified accounting fact.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
import hashlib
from uuid import UUID, uuid4

from app.data_ingestion.clients.tushare import TushareClient
from app.data_ingestion.schemas.corporate_action import CorporateActionInput, normalize_fund_div_row

FUND_DIV_STATUS_VERSION = "tushare_fund_div_status@1"
FUND_DIV_CASH_DATE_VERSION = "tushare_fund_div_cash_date@1"
FUND_DIV_TIMING_VERSION = "after_open_match@1"

CashEffectiveCalendarResolver = Callable[
    [CorporateActionInput, UUID], Mapping[str, object]
]


def fetch_fund_div(
    client: TushareClient,
    *,
    ann_date: str | None = None,
    ts_code: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    return client.fund_div(
        ann_date=ann_date,
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
    )


def normalize_fund_div(rows):
    """Normalize rows while preserving invalid source values for audit."""
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    return [normalize_fund_div_row(dict(row)) for row in rows]


def logical_fact_key(item) -> str:
    raw = getattr(item, "raw", {})
    return "tushare:fund_div:fund_div:{code}:{ann}:{year}:{base}".format(
        code=item.ts_code,
        ann=item.ann_date or raw.get("ann_date"),
        year=raw.get("base_year") or "",
        base=raw.get("base_date") or "",
    )


def detect_logical_key_conflicts(items):
    """Return keys whose candidate identity maps to differing source payloads."""
    grouped = {}
    for item in items:
        key = logical_fact_key(item)
        grouped.setdefault(key, set()).add(item.source_hash)
    return tuple(key for key, hashes in grouped.items() if len(hashes) > 1)


def cash_effective_date(item, *, next_open_session=None):
    """Return the source-selected business date without calendar guessing."""
    payment = item.payment_date
    if payment is None:
        return None
    return next_open_session(payment) if next_open_session else payment


def derive_cash_effective_session(
    item,
    *,
    calendar_id,
    timezone_name,
    calendar_definition,
    next_open_session,
):
    """Map the selected source date to the named calendar's next open session.

    ``next_open_session`` follows the settlement gateway contract and accepts
    an ``after_session`` value.  Passing the preceding natural date includes
    the source date itself when that date is open, while still mapping a
    weekend or holiday to the next official session.
    """
    if not calendar_id or not timezone_name or not calendar_definition:
        raise ValueError("corporate_action_calendar_unresolved")
    payment = cash_effective_date(item)
    if payment is None:
        raise ValueError("corporate_action_cash_date_unresolved")
    try:
        session = next_open_session(
            calendar_id,
            after_session=payment - timedelta(days=1),
        )
    except TypeError:
        # Keep the small helper usable with legacy test gateways while the
        # production gateway remains keyword-compatible with settlement.
        try:
            session = next_open_session(calendar_id, payment - timedelta(days=1))
        except TypeError:
            session = next_open_session(payment)
    if session is None or session < payment:
        raise ValueError("corporate_action_cash_date_unresolved")
    return session

def _cash_input_failure(item: CorporateActionInput) -> str | None:
    """Return the first missing or impossible cash-dividend field."""
    if item.ex_date is None:
        return "corporate_action_ex_date_unresolved"
    if item.record_date is None:
        return "corporate_action_record_date_unresolved"
    if item.amount is None or item.amount <= 0:
        return "corporate_action_amount_invalid"
    if item.payment_date is None:
        return "corporate_action_cash_date_unresolved"
    if not item.ex_date <= item.record_date <= item.payment_date:
        return "corporate_action_date_order_invalid"
    return None


def _derive_persisted_cash_session(
    item: CorporateActionInput,
    instrument_id: UUID,
    calendar_resolver: CashEffectiveCalendarResolver | None,
) -> tuple[date, dict[str, str]]:
    """Resolve one source date through the instrument's named calendar."""
    if calendar_resolver is None:
        raise ValueError("corporate_action_calendar_unresolved")
    context = calendar_resolver(item, instrument_id)
    if not isinstance(context, Mapping):
        raise ValueError("corporate_action_calendar_unresolved")
    effective = derive_cash_effective_session(
        item,
        calendar_id=context.get("calendar_id"),
        timezone_name=context.get("timezone"),
        calendar_definition=context.get("calendar_definition"),
        next_open_session=context.get("next_open_session"),
    )
    definition = context.get("calendar_definition")
    definition_version = getattr(definition, "definition_version", definition)
    evidence = {
        "calendar_id": str(context["calendar_id"]),
        "timezone": str(context["timezone"]),
        "calendar_definition": str(definition_version),
        "cash_date_rule": FUND_DIV_CASH_DATE_VERSION,
        "source_cash_date_rule": str(item.cash_date_rule or ""),
        "timing_rule": FUND_DIV_TIMING_VERSION,
        "selected_source_date": (
            "earpay_date" if item.source_arrival_date_raw is not None else "pay_date"
        ),
        "cash_effective_date": effective.isoformat(),
    }
    return effective, evidence


def sync_fund_div(
    client,
    *,
    session=None,
    checkpoint_repo=None,
    sync_key=None,
    scope_key="fund_div",
    checkpoint_cursor=None,
    checkpoint_version=None,
    instrument_map=None,
    calendar_resolver: CashEffectiveCalendarResolver | None = None,
    accepted_at: datetime | None = None,
    **kwargs,
):
    """Fetch and optionally persist one atomic synchronization unit.

    Persistence is deliberately duck-typed so worker tests can provide a fake
    session; callers own the surrounding transaction and commit/rollback.
    Invalid status, identity, dates, calendar evidence, and derivation are
    returned as failures and prevent checkpoint advancement.
    """
    accepted = accepted_at or datetime.now(UTC)
    if accepted.tzinfo is None or accepted.utcoffset() is None:
        raise ValueError("accepted_at must be timezone-aware")
    accepted = accepted.astimezone(UTC)
    rows = fetch_fund_div(client, **kwargs)
    raw_rows = (
        rows.to_dict("records") if hasattr(rows, "to_dict") else [dict(row) for row in rows]
    )
    # Save the complete response, including an empty response, before parsing.
    if session is not None:
        from app.data_ingestion.models.corporate_action import CorporateActionSourceFact
        import hashlib
        import json

        source_hash = hashlib.sha256(
            json.dumps(raw_rows, sort_keys=True, default=str).encode()
        ).hexdigest()
        existing_snapshot = None
        query = getattr(session, "query", None)
        query_kind = (
            "ann_date" if kwargs.get("ann_date")
            else "ts_code" if kwargs.get("ts_code")
            else "range"
        )
        query_value = kwargs.get("ann_date") or kwargs.get("ts_code")
        if callable(query):
            existing_snapshot = query(CorporateActionSourceFact).filter_by(
                source="tushare",
                endpoint="fund_div",
                query_kind=query_kind,
                query_value=query_value,
                source_hash=source_hash,
            ).first()
        if existing_snapshot is None:
            session.add(
                CorporateActionSourceFact(
                    id=uuid4(),
                    source="tushare",
                    endpoint="fund_div",
                    query_kind=query_kind,
                    query_value=query_value,
                    ts_code=kwargs.get("ts_code") or "*",
                    ann_date=None,
                    payload={"rows": raw_rows},
                    source_hash=source_hash,
                    source_revision=source_hash,
                    observed_at=accepted,
                )
            )
    items = normalize_fund_div(raw_rows)
    conflicts = detect_logical_key_conflicts(items)
    conflicting_keys = set(conflicts)
    failures: list[dict[str, str]] = [
        {"logical_fact_key": key, "reason": "conflicting_source_payloads"}
        for key in conflicts
    ]
    persisted = 0
    unchanged = 0
    if session is not None and instrument_map is not None:
        from app.data_ingestion.models.corporate_action import CorporateActionFact

        for item in items:
            key = logical_fact_key(item)
            if key in conflicting_keys:
                continue
            if item.status != "implemented":
                failures.append({"ts_code": item.ts_code, "reason": "source_status_not_implemented"})
                continue
            instrument_id = instrument_map.get(item.ts_code)
            if instrument_id is None:
                failures.append({"ts_code": item.ts_code, "reason": "instrument_mapping_missing"})
                continue
            input_failure = _cash_input_failure(item)
            if input_failure is not None:
                failures.append({"ts_code": item.ts_code, "reason": input_failure})
                continue
            try:
                effective, derivation_evidence = _derive_persisted_cash_session(
                    item, instrument_id, calendar_resolver
                )
            except (TypeError, ValueError) as exc:
                failures.append(
                    {
                        "ts_code": item.ts_code,
                        "reason": str(exc) or type(exc).__name__,
                    }
                )
                continue
            previous = None
            try:
                previous = (
                    session.query(CorporateActionFact)
                    .filter_by(logical_fact_key=key)
                    .order_by(CorporateActionFact.fact_version.desc())
                    .first()
                )
            except Exception:
                previous = None
            if previous is not None and (previous.evidence or {}).get("source_hash") == item.source_hash:
                unchanged += 1
                continue
            version = (previous.fact_version + 1) if previous is not None else 1
            session.add(
                CorporateActionFact(
                    event_id=uuid4(),
                    logical_fact_key=key,
                    fact_version=version,
                    supersedes_fact_id=(previous.event_id if previous else None),
                    instrument_id=instrument_id,
                    action_type="cash_dividend",
                    record_date=item.record_date,
                    ex_date=item.ex_date,
                    source_payment_date=item.source_payment_date_raw,
                    source_arrival_date=item.source_arrival_date_raw,
                    cash_effective_date=effective,
                    cash_effective_phase="after_open_match",
                    cash_amount_per_unit=item.amount,
                    currency=item.currency,
                    entitlement_rule="record_date_entitlement",
                    cash_date_rule=FUND_DIV_CASH_DATE_VERSION,
                    timing_rule=FUND_DIV_TIMING_VERSION,
                    source="tushare",
                    source_revision=item.source_hash,
                    valid_from=item.ex_date,
                    valid_to=(item.ex_date + timedelta(days=1)) if item.ex_date else None,
                    known_at=accepted,
                    observed_at=accepted,
                    quality="complete",
                    evidence={
                        "source_hash": item.source_hash,
                        "source_revision": item.source_hash,
                        "known_at": accepted.isoformat(),
                        "observed_at": accepted.isoformat(),
                        "status_rule": FUND_DIV_STATUS_VERSION,
                        "raw": item.raw,
                        **derivation_evidence,
                    },
                )
            )
            persisted += 1
    failed = len(failures)
    advanced = False
    checkpoint = None
    if checkpoint_repo is not None and sync_key and not failed:
        checkpoint = checkpoint_repo.advance(
            sync_key=sync_key,
            scope_key=scope_key,
            cursor=checkpoint_cursor or {},
            expected_version=checkpoint_version,
        )
        advanced = True
    return {
        "items": items,
        "fetched": len(raw_rows),
        "changed": persisted,
        "unchanged": unchanged,
        "failed": failed,
        "failures": failures,
        "skipped_non_target": 0,
        "checkpoint_advanced": advanced,
        "checkpoint_after": checkpoint.cursor if advanced else None,
        "conflicts": conflicts,
    }


def sync_fund_div_full(
    client,
    *,
    ts_codes,
    session=None,
    checkpoint_repo=None,
    sync_key=None,
    instrument_map=None,
    checkpoint_version=None,
    calendar_resolver: CashEffectiveCalendarResolver | None = None,
    accepted_at: datetime | None = None,
    **kwargs,
):
    """Scan every resolved ETF source code and commit only after all succeed.

    The helper intentionally treats a page at the client limit as potentially
    truncated; callers must paginate or report failure instead of claiming
    complete coverage.  A checkpoint is advanced once for the aggregate batch.
    """
    accepted = accepted_at or datetime.now(UTC)
    if accepted.tzinfo is None or accepted.utcoffset() is None:
        raise ValueError("accepted_at must be timezone-aware")
    accepted = accepted.astimezone(UTC)
    aggregate = {
        "items": [],
        "fetched": 0,
        "changed": 0,
        "unchanged": 0,
        "failed": 0,
        "failures": [],
        "skipped_non_target": 0,
        "checkpoint_advanced": False,
        "conflicts": [],
    }
    for code in tuple(ts_codes):
        result = sync_fund_div(
            client,
            session=session,
            instrument_map=instrument_map,
            calendar_resolver=calendar_resolver,
            accepted_at=accepted,
            checkpoint_repo=None,
            sync_key=None,
            ts_code=code,
            **kwargs,
        )
        for key in ("items", "conflicts", "failures"):
            aggregate[key].extend(result.get(key, ()))
        for key in ("fetched", "changed", "unchanged", "failed", "skipped_non_target"):
            aggregate[key] += int(result.get(key, 0) or 0)
    if session is not None and aggregate["failed"] == 0 and instrument_map:
        from app.data_ingestion.models.corporate_action import CorporateActionCoverageFact

        def coverage_date(value, fallback):
            if value in (None, ""):
                return fallback
            return value if isinstance(value, date) else date.fromisoformat(str(value))

        revisions = sorted(
            item.source_hash
            for item in aggregate["items"]
            if getattr(item, "source_hash", None)
        )
        coverage_revision = hashlib.sha256("|".join(revisions).encode()).hexdigest()
        by_instrument = {}
        for item in aggregate["items"]:
            iid = instrument_map.get(item.ts_code)
            if iid is not None:
                by_instrument[iid] = by_instrument.get(iid, 0) + 1
        for iid in set(instrument_map.values()):
            session.add(CorporateActionCoverageFact(
                id=uuid4(), instrument_id=iid, action_type="cash_dividend",
                start_date=coverage_date(kwargs.get("start_date"), date.min),
                end_date=coverage_date(kwargs.get("end_date"), date.today()),
                status="complete", event_count=by_instrument.get(iid, 0),
                source="tushare",
                source_revision=coverage_revision,
                known_at=accepted,
                observed_at=accepted,
                evidence={"query_kind": "ts_code", "source": "tushare", "ts_codes": list(ts_codes), "source_revision": coverage_revision},
                validation_rule="tushare_fund_div_coverage@1", summary={"full_scan": True},
            ))
    if checkpoint_repo is not None and sync_key and aggregate["failed"] == 0:
        cursor = {
            **(kwargs.get("checkpoint_cursor") or {}),
            "ts_codes": list(ts_codes),
        }
        checkpoint = checkpoint_repo.advance(sync_key=sync_key, scope_key="fund_div",
                                              cursor=cursor,
                                              expected_version=checkpoint_version)
        aggregate["checkpoint_advanced"] = True
        aggregate["checkpoint_after"] = checkpoint.cursor
    return aggregate

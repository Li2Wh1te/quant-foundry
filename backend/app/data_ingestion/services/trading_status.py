"""Daily trading-status (suspension) ingestion primitives."""

from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from typing import Mapping

from app.data_ingestion.schemas.trading_status import normalize_suspend_row


def fetch_suspend_d(client, **kwargs):
    return client.suspend_d(**kwargs)


def normalize_suspend(rows):
    raw = rows.to_dict("records") if hasattr(rows, "to_dict") else rows
    return [normalize_suspend_row(dict(row)) for row in raw]


def _accepted_at(value: datetime | None) -> datetime:
    """Normalize the ingestion acceptance timestamp to aware UTC."""

    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("accepted_at must be timezone-aware")
    return value.astimezone(UTC)


def sync_suspend_d(
    client,
    *,
    session=None,
    checkpoint_repo=None,
    sync_key=None,
    scope_key="trading_status",
    checkpoint_cursor=None,
    checkpoint_version=None,
    instrument_map: Mapping[str, object] | None = None,
    accepted_at: datetime | None = None,
    coverage_confirmed: bool = False,
    **kwargs,
):
    """Fetch and persist versioned facts; coverage requires explicit confirmation."""

    if not isinstance(coverage_confirmed, bool):
        raise ValueError("coverage_confirmed must be a boolean")
    rows = fetch_suspend_d(client, **kwargs)
    raw_rows = rows.to_dict("records") if hasattr(rows, "to_dict") else [dict(row) for row in rows]
    items = normalize_suspend(raw_rows)
    accepted = _accepted_at(accepted_at)
    changed = 0
    unchanged = 0
    if session is not None:
        from app.data_ingestion.models.trading_calendar import (
            TradingStatusCoverageFact,
            TradingStatusFact,
            TradingStatusFactRevisionAudit,
            TradingStatusSourceFact,
        )

        source_hash = hashlib.sha256(
            json.dumps(
                raw_rows, sort_keys=True, default=str, ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        # The lightweight ingestion test doubles do not expose a query API;
        # production SQL sessions do, so only the latter receive the raw
        # response snapshot without changing the legacy fake-session shape.
        if callable(getattr(session, "query", None)):
            query_kind = "ts_code" if kwargs.get("ts_code") else "range"
            query_value = (
                str(kwargs["ts_code"])
                if query_kind == "ts_code"
                else f"{kwargs.get('start_date', '')}:{kwargs.get('end_date', '')}"
            )
            existing_snapshot = session.query(TradingStatusSourceFact).filter_by(
                source="tushare",
                endpoint="suspend_d",
                query_kind=query_kind,
                query_value=query_value,
                source_hash=source_hash,
            ).first()
            if existing_snapshot is None:
                session.add(
                    TradingStatusSourceFact(
                        source="tushare",
                        endpoint="suspend_d",
                        query_kind=query_kind,
                        query_value=query_value,
                        payload={"rows": raw_rows},
                        source_hash=source_hash,
                        source_revision=source_hash,
                        observed_at=accepted,
                    )
                )

        for item in items:
            raw = item.raw or {}
            values = {
                "instrument_id": instrument_map.get(item.ts_code)
                if instrument_map is not None
                else None,
                "dimension": "suspension",
                "status": item.status,
                "valid_from": item.trade_date,
                "valid_to": item.trade_date + timedelta(days=1),
                "source": item.source,
                "source_revision": item.source_revision,
                "quality_status": item.quality_status,
                "raw": raw,
                "observed_at": accepted,
                # ``known_at`` is the platform acceptance time.  It is not
                # backdated to the trade date, so historical backfills remain
                # invisible to PIT queries before this ingestion instant.
                "known_at": accepted,
            }
            existing = session.get(TradingStatusFact, (item.ts_code, item.trade_date))
            if existing is not None and values["instrument_id"] is None:
                # An incomplete catalogue snapshot must not erase a stable
                # identity that was already attached to this fact.
                values["instrument_id"] = getattr(existing, "instrument_id", None)
            if existing is None:
                session.add(
                    TradingStatusFact(
                        ts_code=item.ts_code,
                        trade_date=item.trade_date,
                        **values,
                    )
                )
                changed += 1
                continue
            previous_revision = getattr(existing, "source_revision", None)
            content_fields = tuple(
                field for field in values if field not in {"known_at", "observed_at"}
            )
            changed_fields = sorted(
                field
                for field in content_fields
                if getattr(existing, field, None) != values[field]
            )
            if not changed_fields:
                unchanged += 1
                continue
            previous = {
                field: getattr(existing, field, None)
                for field in values
            }
            for field, value in values.items():
                setattr(existing, field, value)
            if previous_revision != item.source_revision:
                session.add(
                    TradingStatusFactRevisionAudit(
                        ts_code=item.ts_code,
                        trade_date=item.trade_date,
                        previous_instrument_id=previous.get("instrument_id"),
                        previous_dimension=previous.get("dimension") or "suspension",
                        previous_status=previous.get("status") or "unknown",
                        previous_valid_from=previous.get("valid_from"),
                        previous_valid_to=previous.get("valid_to"),
                        previous_source=previous.get("source") or "tushare",
                        previous_quality_status=previous.get("quality_status") or "complete",
                        previous_known_at=previous.get("known_at"),
                        previous_observed_at=previous.get("observed_at"),
                        previous_raw=previous.get("raw", {}) or {},
                        previous_source_revision=previous_revision,
                        source_revision=item.source_revision or "",
                        accepted_at=accepted,
                        change_kind=(
                            "metadata_backfill"
                            if previous_revision is None
                            else "correction"
                        ),
                        changed_fields=changed_fields,
                    )
                )
            changed += 1

        # A row response, including an empty response, is not by itself a
        # proof that the requested interval was fully scanned.  The caller
        # must explicitly confirm the provider query's completeness before a
        # negative-space coverage fact is persisted.
        if (
            coverage_confirmed
            and instrument_map is not None
            and not any(item.quality_status != "complete" for item in items)
        ):
            start_value = kwargs.get("start_date")
            end_value = kwargs.get("end_date")
            start_date = (
                start_value
                if isinstance(start_value, date)
                else date.fromisoformat(str(start_value))
                if start_value
                else min((item.trade_date for item in items), default=accepted.date())
            )
            end_date = (
                end_value
                if isinstance(end_value, date)
                else date.fromisoformat(str(end_value))
                if end_value
                else max((item.trade_date for item in items), default=start_date)
            )
            revision = hashlib.sha256(
                "|".join(
                    sorted(
                        item.source_revision
                        for item in items
                        if item.source_revision is not None
                    )
                ).encode()
            ).hexdigest()
            counts: dict[str, int] = {}
            for item in items:
                counts[item.ts_code] = counts.get(item.ts_code, 0) + 1
            scoped_instrument_map = instrument_map
            requested_code = kwargs.get("ts_code")
            if requested_code:
                scoped_instrument_map = {
                    requested_code: instrument_map.get(requested_code)
                }
            for ts_code, instrument_id in scoped_instrument_map.items():
                if instrument_id is None:
                    continue
                session.add(
                    TradingStatusCoverageFact(
                        instrument_id=instrument_id,
                        dimension="suspension",
                        start_date=start_date,
                        end_date=end_date,
                        status="complete",
                        event_count=counts.get(ts_code, 0),
                        source="tushare",
                        source_revision=revision,
                        known_at=accepted,
                        observed_at=accepted,
                        evidence={
                            "endpoint": "suspend_d",
                            "query_kind": "ts_code" if requested_code else "range",
                            "source_code": ts_code,
                            "source_revision": revision,
                        },
                        validation_rule="tushare_suspend_d_coverage@1",
                        summary={
                            "coverage_confirmed": True,
                            "query_scope": "source_code" if requested_code else "market",
                        },
                    )
                )
    else:
        changed = len(items)

    advanced = False
    if checkpoint_repo is not None and sync_key and not any(
        item.quality_status != "complete" for item in items
    ):
        checkpoint = checkpoint_repo.advance(
            sync_key=sync_key,
            scope_key=scope_key,
            cursor=checkpoint_cursor or {},
            expected_version=checkpoint_version,
        )
        advanced = True
    else:
        checkpoint = None
    return {
        "items": items,
        "fetched": len(items),
        "changed": changed,
        "unchanged": unchanged,
        "failed": sum(item.quality_status != "complete" for item in items),
        "checkpoint_advanced": advanced,
        "checkpoint_scope": "trading_status",
        "checkpoint_after": checkpoint.cursor if advanced else None,
        "coverage_status": (
            "complete"
            if coverage_confirmed
            and instrument_map is not None
            and not any(item.quality_status != "complete" for item in items)
            else "unknown"
        ),
        "evidence": {
            "source": "tushare",
            "endpoint": "suspend_d",
            "row_count": len(items),
            "accepted_at": accepted.isoformat(),
            "source_revisions": sorted(
                {item.source_revision for item in items if item.source_revision}
            ),
        },
    }

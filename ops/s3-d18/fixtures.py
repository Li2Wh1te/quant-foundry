"""Fixed D18 inputs: a single synthetic CurrentStore, never production data."""

from copy import deepcopy
from datetime import date, timedelta
from dataclasses import dataclass
import numpy as np
import pyarrow as pa
from support_sdk import spec, MINUTE, MIDNIGHT
from app.backtest_service.data_gateway import MARKET_SCHEMA
from app.data_store.schema import DatasetSpec

MODEL = "synthetic_d18_full_integration"
DAY = 1440 * MINUTE


@dataclass(frozen=True)
class Load:
    name: str
    securities: int
    days: int
    bars: int = 1
    ticks: int = 0
    kind: str = "bar"

    @property
    def rows(self):
        return self.ticks or self.securities * self.days * self.bars

    @property
    def universe(self):
        return [f"S{i:04}.SH" for i in range(self.securities)]


FULL = (
    Load("B1", 5000, 2520),
    Load("B2", 200, 252, 240),
    Load("B3-trade", 1, 1, ticks=10000000, kind="trade_tick"),
    Load("B3-quote", 1, 1, ticks=10000000, kind="quote_tick"),
    Load("B4", 30, 1260),
)
SMOKE = (
    Load("B1", 8, 25),
    Load("B2", 3, 3, 240),
    Load("B3-trade", 1, 1, ticks=1000, kind="trade_tick"),
    Load("B3-quote", 1, 1, ticks=1000, kind="quote_tick"),
    Load("B4", 3, 25),
)


def calendar(days):
    template = spec(days=1, securities=("S0000.SH",))["calendar"][0]
    rows = []
    stamps = [date(2016, 1, 1) + timedelta(days=i) for i in range(days)]
    for i, stamp in enumerate(stamps):
        row = deepcopy(template)
        shift = (stamp - date(2026, 1, 1)).days * DAY
        row["session"]["key"] = f"S{i+1}"
        row["date"] = stamp.isoformat()
        for key in ("local_midnight_ns", "before_open_ns", "after_close_ns"):
            row[key] = str(int(row[key]) + shift)
        for key in ("open_ns", "close_ns"):
            row["session"][key] = str(int(row["session"][key]) + shift)
        for key in ("market_windows", "bar_windows"):
            for window in row[key]:
                for field in ("start_ns", "end_ns"):
                    window[field] = str(int(window[field]) + shift)
        week = i // 5
        row["week"] = dict(
            id=f"fixture-week-{week}",
            trading_day=i % 5 + 1,
            total_trading_days=min(5, days - week * 5),
        )
        month = stamp.strftime("%Y-%m")
        members = [s for s in stamps if s.strftime("%Y-%m") == month]
        row["month"] = dict(
            id=month,
            trading_day=members.index(stamp) + 1,
            total_trading_days=len(members),
        )
        rows.append(row)
    return rows


def template(load):
    value = spec(
        days=1, securities=("S0000.SH",), frequency="tick" if load.ticks else "1d"
    )
    ordered_calendar = calendar(load.days + 1)
    value["calendar"] = ordered_calendar[:-1]
    value["following_session"] = ordered_calendar[-1]
    value["next_session"] = ordered_calendar[-1]["session"]["key"]
    value["calendar_reference"] = "synthetic-d18-calendar"
    value["synthetic_model"] = MODEL
    value["facts"] = []
    value["stream_session_facts"] = True
    for rule in value["rules"]:
        rule["origin"]["reference"] = MODEL
        rule["effective"] = {"from": "2010-01-01", "through": "2030-12-31"}
    value["config"].update(
        start=value["calendar"][0]["date"],
        end=value["calendar"][-1]["date"],
        universe=load.universe,
        initial_cash="10000000",
        reference_calendar=value["calendar_reference"],
        parameters={"window": 20},
        participation_rate="0.10",
        result_sampling="session_close",
    )
    value.update(
        data_budget_bytes=512 * 1024**2,
        data_max_rows=110000,
        max_active_orders=1000,
        max_commands_per_boundary=1000,
        chunk_events=10000,
        wall_time_seconds=3600,
    )
    value["source"] = {
        "kind": "ipc",
        "socket_path": "/run/qf/ipc/data.sock",
        "binding": "market",
        "request": {},
    }
    return value


def facts(load, calendar_row, offset, maximum, commissions):
    seed = spec(days=1, securities=("S0000.SH",))["facts"][0]
    result = []
    for security in load.universe[offset : offset + maximum]:
        row = deepcopy(seed)
        row["instrument"]["security"] = row["day"]["security"] = security
        row["day"].update(
            date=calendar_row["date"], session=calendar_row["session"]["key"]
        )
        row["day"]["listing"].update(
            first_trading_date="2010-01-01", verified_through="2030-12-31"
        )
        row["origin"]["reference"] = MODEL
        row["disposal_settlement_date"] = calendar_row["date"]
        for side in ("buy", "sell"):
            row[side + "_fees"].update(synthetic_model=MODEL, **commissions[side])
        result.append(row)
    next_offset = offset + len(result)
    return dict(
        session=calendar_row["session"]["key"],
        facts=result,
        next_offset=next_offset if next_offset < load.securities else None,
    )


def dataset(load):
    keys = (
        "time_ns",
        "security",
        "source_session",
        "channel",
        "sequence",
        "stable_input_sequence",
    )
    fields = [
        pa.field(
            f.name,
            f.type,
            nullable=f.name not in keys,
            metadata=(
                {b"max_utf8_bytes": b"32"}
                if pa.types.is_string(f.type)
                else {b"unit": b"epoch_ns"} if f.name.endswith("_ns") else None
            ),
        )
        for f in MARKET_SCHEMA
    ]
    return DatasetSpec(
        "d18_" + load.name.lower().replace("-", "_"),
        pa.schema(fields, metadata=MARKET_SCHEMA.metadata),
        keys,
        "synthetic_d18",
        {"price_basis": "raw", "price_currency": "CNY", "quantity_unit": "shares"},
    )


def batches(load, cal, first, last, schema):
    """At most 10k input rows. Never constructs the full event list."""
    symbols = load.universe
    if load.ticks:
        for start in range(first, last, 10000):
            n = min(10000, last - start)
            seq = np.arange(start + 1, start + n + 1, dtype=np.uint64)
            midnight = int(cal[0]["local_midnight_ns"])
            stamp = (
                midnight + 570 * MINUTE + 123 + ((seq.astype(np.int64) - 1) // 2) * 1000
            )
            block = (seq // 100000) % 2
            bid = np.where(block == 0, "10.00", "10.01")
            ask = np.where(block == 0, "10.01", "10.02")
            values = dict(
                kind=[load.kind] * n,
                security=[symbols[0]] * n,
                session=["S1"] * n,
                source_session=["S1"] * n,
                channel=["fixture"] * n,
                time_ns=stamp,
                sequence=seq,
                stable_input_sequence=seq,
                price=["10.01"] * n if load.kind == "trade_tick" else None,
                quantity=(
                    np.full(n, 100, dtype=np.int64)
                    if load.kind == "trade_tick"
                    else None
                ),
                bid=bid if load.kind == "quote_tick" else None,
                ask=ask if load.kind == "quote_tick" else None,
                bid_quantity=(
                    np.full(n, 100, dtype=np.int64)
                    if load.kind == "quote_tick"
                    else None
                ),
                ask_quantity=(
                    np.full(n, 100, dtype=np.int64)
                    if load.kind == "quote_tick"
                    else None
                ),
            )
            yield pa.RecordBatch.from_arrays(
                [
                    (
                        pa.array(values[f.name], type=f.type)
                        if values.get(f.name) is not None
                        else pa.nulls(n, f.type)
                    )
                    for f in schema
                ],
                schema=schema,
            )
        return
    for day in range(first, last):
        midnight = int(cal[day]["local_midnight_ns"])
        session = f"S{day+1}"
        for minute in range(load.bars):
            begin = (
                570
                if load.bars == 1
                else (570 + minute if minute < 120 else 780 + minute - 120)
            )
            end = 900 if load.bars == 1 else begin + 1
            prices = [
                f"{10+(i*7+day*3)%10}.{(day+i)%100:02}" for i in range(load.securities)
            ]
            values = dict(
                kind=["bar"] * load.securities,
                security=symbols,
                session=[session] * load.securities,
                source_session=[session] * load.securities,
                channel=["fixture"] * load.securities,
                time_ns=np.full(
                    load.securities, midnight + end * MINUTE, dtype=np.int64
                ),
                interval_start_ns=np.full(
                    load.securities, midnight + begin * MINUTE, dtype=np.int64
                ),
                interval_end_ns=np.full(
                    load.securities, midnight + end * MINUTE, dtype=np.int64
                ),
                open=prices,
                high=prices,
                low=prices,
                close=prices,
                quantity=np.full(load.securities, 1000000, dtype=np.int64),
                sequence=np.full(load.securities, minute + 1, dtype=np.uint64),
                stable_input_sequence=np.full(
                    load.securities, minute + 1, dtype=np.uint64
                ),
            )
            yield pa.RecordBatch.from_arrays(
                [
                    (
                        pa.array(values[f.name], type=f.type)
                        if f.name in values
                        else pa.nulls(load.securities, f.type)
                    )
                    for f in schema
                ],
                schema=schema,
            )

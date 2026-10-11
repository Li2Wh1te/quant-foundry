"""Small explicit D14 synthetic business inputs, never production mappings."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

from support_sdk import spec, MINUTE, event_key


def example_spec(entry):
    config = entry["config"]
    source = spec(days=12, securities=tuple(config["universe"]), frequency=config["frequency"])
    first = date.fromisoformat(config["start"])
    shift = (first - date(2026, 1, 1)).days * 1440 * MINUTE
    dates = [first + timedelta(days=i) for i in range(12)]
    source["calendar_reference"] = "synthetic-d14-calendar"
    source["synthetic_model"] = "synthetic_d14_examples_not_market_data"
    source["config"] = deepcopy(config)
    for i, row in enumerate(source["calendar"]):
        row["date"] = dates[i].isoformat()
        for name in ("local_midnight_ns", "before_open_ns", "after_close_ns"):
            row[name] = str(int(row[name]) + shift)
        for name in ("open_ns", "close_ns"):
            row["session"][name] = str(int(row["session"][name]) + shift)
        for kind in ("market_windows", "bar_windows"):
            for window in row[kind]:
                for name in ("start_ns", "end_ns"):
                    window[name] = str(int(window[name]) + shift)
        week = i // 5
        row["week"] = dict(id=f"synthetic-week-{week}", trading_day=i % 5 + 1, total_trading_days=min(5, 12-week*5))
        month = dates[i].strftime("%Y-%m")
        members = [d for d in dates if d.strftime("%Y-%m") == month]
        row["month"] = dict(id=month, trading_day=members.index(dates[i])+1, total_trading_days=len(members))
    etf = entry["example"] in ("etf_rotation.py", "intraday.py", "basic_tick.py")
    product = "equity_etf" if etf else "main_board_stock"
    source["rules"][0]["product"] = product
    source["rules"][0]["origin"]["reference"] = source["synthetic_model"]
    for facts in source["facts"]:
        i = int(facts["day"]["session"][1:])-1
        facts["day"]["date"] = dates[i].isoformat()
        facts["disposal_settlement_date"] = dates[i].isoformat()
        facts["instrument"]["product"] = product
        if etf:
            facts["day"]["fund_price_band"] = "ten_percent"
        facts["origin"]["reference"] = source["synthetic_model"]
        for side in ("buy", "sell"):
            facts[side+"_fees"]["synthetic_model"] = source["synthetic_model"]
    events = []
    for event in source["source"]["events"]:
        row = event["event"]
        i = int(row["session"][1:])-1
        for name in ("time_ns", "interval_start_ns", "interval_end_ns"):
            if name in row:
                row[name] = str(int(row[name])+shift)
        row["identity"]["channel"] = "synthetic-d14"
        row["quantity"] = 100000
        if config["frequency"] == "60m":
            n = int(row["identity"]["sequence"])-1
            begin = (570, 630, 780, 840)[n]
            midnight = int(source["calendar"][i]["local_midnight_ns"])
            row["interval_start_ns"], row["interval_end_ns"] = str(midnight+begin*MINUTE), str(midnight+(begin+60)*MINUTE)
        if event["kind"] == "bar":
            if config["frequency"] == "1d":
                price = Decimal(10 + i if i < 4 else 16-i) if row["security"] == config["universe"][0] else Decimal(10-i if i < 4 else 6+i)
            else:
                price = Decimal(10 + int(row["identity"]["sequence"]))
            row.update(open=str(price), close=str(price), high=str(price+1), low=str(price-1))
        events.append(event)
    events.sort(key=event_key)
    source["source"]["events"] = events
    return source


def fundamental_rows(source):
    rows = []
    first = int(source["calendar"][0]["before_open_ns"])-1
    second = int(next(c for c in source["calendar"] if c["date"] == "2026-02-01")["before_open_ns"])+1
    for security, a, b in zip(source["config"]["universe"], ("100", "10"), ("1", "200")):
        for n, (at, value) in enumerate(((first, a), (second, b))):
            rows.append(dict(security=security, instrument_id="synthetic-"+security, record_id=f"synthetic-report-{n}",
                field="net_profit", value_kind="decimal", value=value, unit="CNY", report_period="2025-12-31",
                time_ns=at, effective_from_ns=at, effective_until_ns=None))
    return rows

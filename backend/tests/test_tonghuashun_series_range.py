"""Effective series bounds are validated before any source request."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.data_ingestion.tonghuashun.contracts import (
    CollectionError,
    CollectionParameters,
    DATASETS,
    date_ms,
)
from app.data_ingestion.tonghuashun.research_acquisition import Unit, series


NOW = datetime(2026, 9, 14, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
SUBJECT = "FIXTURE.OF"


class RecordedReads:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def read(self, interface, parameters):
        self.calls.append((interface, dict(parameters)))
        return self.reply(parameters)


@pytest.mark.parametrize("dataset", ["fund_performance_history", "rank_trend"])
@pytest.mark.parametrize("offset", [0, 1])
def test_start_after_automatic_end_is_rejected_before_request(dataset, offset):
    # end_date is deliberately omitted: model validation cannot compare it
    # with the source-specific automatic cutoff yet.
    parameters = CollectionParameters(start_date=NOW.date() + timedelta(days=offset))
    previous = {"item": [{"date_ms": date_ms(date(2026, 9, 1)), "value": 17}]}
    original = deepcopy(previous)
    reads = RecordedReads(lambda query: pytest.fail("invalid range reached the source"))

    with pytest.raises(CollectionError, match="查询开始日期晚于有效结束日期"):
        series(reads, DATASETS[dataset], Unit(SUBJECT, {}), parameters, previous, NOW)

    assert reads.calls == []
    assert previous == original


def test_equal_effective_bounds_after_late_cutoff_are_allowed():
    now = NOW.replace(hour=23)
    key = date_ms(now.date())
    reads = RecordedReads(lambda query: {"item": [{"date_ms": key, "value": 17}]})

    result = series(
        reads,
        DATASETS["fund_performance_history"],
        Unit(SUBJECT, {}),
        CollectionParameters(start_date=now.date()),
        None,
        now,
    )

    assert [query for _, query in reads.calls] == [
        {"thscode": SUBJECT, "start": key, "end": key}
    ]
    assert result["item"] == [{"date_ms": key, "value": 17}]


def test_future_selected_series_does_not_accept_empty_without_a_request():
    reads = RecordedReads(lambda query: pytest.fail("invalid range reached the source"))

    with pytest.raises(CollectionError, match="查询开始日期晚于有效结束日期"):
        series(
            reads,
            DATASETS["rank_trend"],
            Unit("FIXTURE.SH", {}),
            CollectionParameters(start_date=NOW.date() + timedelta(days=1)),
            None,
            NOW.replace(hour=23),
        )

    assert reads.calls == []


def test_empty_early_history_windows_preserve_later_data():
    key = date_ms(NOW.date() - timedelta(days=1))
    reads = RecordedReads(
        lambda query: {
            "item": [{"date_ms": key, "value": 17}]
            if query["start"] <= key <= query["end"]
            else []
        }
    )

    result = series(
        reads,
        DATASETS["fund_performance_history"],
        Unit(SUBJECT, {}),
        CollectionParameters(),
        None,
        NOW,
    )

    assert len(reads.calls) > 1
    assert result["item"] == [{"date_ms": key, "value": 17}]
    assert result["requested_end"] == "2026-09-13"


def test_valid_but_empty_range_keeps_existing_no_coverage_error():
    reads = RecordedReads(lambda query: {"item": []})

    with pytest.raises(CollectionError, match="历史业绩范围未返回数据"):
        series(
            reads,
            DATASETS["fund_performance_history"],
            Unit(SUBJECT, {}),
            CollectionParameters(start_date=date(2026, 9, 1)),
            None,
            NOW,
        )

    assert reads.calls


def test_out_of_range_rows_are_still_rejected():
    reads = RecordedReads(
        lambda query: {"item": [{"date_ms": query["end"] + 86400000, "value": 17}]}
    )

    with pytest.raises(CollectionError, match="历史指标包含重复日期或范围外数据"):
        series(
            reads,
            DATASETS["fund_performance_history"],
            Unit(SUBJECT, {}),
            CollectionParameters(start_date=date(2026, 9, 1)),
            None,
            NOW,
        )

    assert reads.calls

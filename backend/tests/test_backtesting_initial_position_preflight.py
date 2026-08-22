"""Tests for the initial-position preflight service and its report contract."""

from datetime import date, timedelta
from decimal import Decimal
import unittest
from uuid import UUID, uuid4

from app.backtesting.domain import PositionSide
from app.backtesting.preflight import (
    BacktestPreflightGateway,
    CheckStatus,
    FactCheckOutcome,
    IdentityMappingEntry,
    InstrumentRulesFacts,
    InitialPositionPreflightIssue,
    InitialPositionPreflightResult,
    InitialPositionPreflightService,
    PreflightStatus,
    RawValuationPrice,
    ResolvedInstrument,
    SettlementAndSellRules,
    SettlementRuleKind,
)
from app.backtesting.spec import BacktestSpec, InitialPositionInput


# Deterministic trading calendar: all August and September 2026 weekdays.
def _build_sessions() -> list[date]:
    sessions = []
    current = date(2026, 8, 1)
    while current < date(2026, 10, 1):
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions


SESSIONS = _build_sessions()
START = date(2026, 8, 21)  # a Friday and a known trading session
END = date(2026, 9, 30)
HOLIDAY = date(2026, 8, 22)  # Saturday between START and the next session
NEXT_SESSION = date(2026, 8, 24)  # Monday after HOLIDAY


class FakeGateway:
    """In-memory gateway returning configurable PIT facts.

    ``price_session_override`` simulates a stale read: the returned price is
    bound to an earlier session than requested.  ``price_value`` may be set
    to zero/negative values to test invalid-price blocking.  Call recording
    lets tests prove which valuation sessions were actually queried.
    """

    def __init__(
        self,
        instrument_ids: list[UUID],
        *,
        price_session_override: date | None = None,
        price_value: str | None = None,
    ) -> None:
        self.instrument_ids = list(instrument_ids)
        self.calendar_id = "XSHG"
        self.price_session_override = price_session_override
        self.price_value = price_value or "12.34"
        self.identity_mapping_counts: dict[UUID, int] = {}
        # Optional (valid_from, valid_to) windows per instrument; None means
        # open-ended, mirroring IdentityMappingEntry semantics.
        self.identity_windows: dict[UUID, tuple[date | None, date | None]] = {}
        self.rules_present: dict[UUID, bool] = {}
        self.requires_trading_status: dict[UUID, bool] = {}
        self.settlement_rule_ids: dict[UUID, str | None] = {}
        self.settlement_rule_kinds: dict[UUID, str | None] = {}
        self.sell_rule_ids: dict[UUID, str | None] = {}
        self.corporate_action_outcomes: dict[UUID, bool] = {}
        self.trading_status_outcomes: dict[UUID, bool] = {}
        self.sessions = list(SESSIONS)
        self.valuation_price_calls: list[tuple[UUID, date]] = []
        for instrument_id in self.instrument_ids:
            self.identity_mapping_counts[instrument_id] = 1
            self.rules_present[instrument_id] = True
            self.requires_trading_status[instrument_id] = True
            self.settlement_rule_ids[instrument_id] = "T1"
            self.settlement_rule_kinds[instrument_id] = (
                SettlementRuleKind.T1_BEFORE_OPEN_MATCH.value
            )
            self.sell_rule_ids[instrument_id] = "SELL-T0"
            self.corporate_action_outcomes[instrument_id] = True
            self.trading_status_outcomes[instrument_id] = True

    # -- protocol implementation -----------------------------------------
    def resolve_instrument(
        self, instrument_id: UUID, *, as_of: date
    ) -> ResolvedInstrument | None:
        if instrument_id not in self.instrument_ids:
            return None
        return ResolvedInstrument(
            instrument_id=instrument_id, calendar_id=self.calendar_id
        )

    def resolve_identity_mapping(self, instrument_id: UUID, *, as_of: date):
        count = self.identity_mapping_counts.get(instrument_id, 0)
        valid_from, valid_to = self.identity_windows.get(
            instrument_id, (date(2000, 1, 1), None)
        )
        return [
            IdentityMappingEntry(
                symbol=f"SYM{index}",
                valid_from=valid_from if valid_from else date(2000, 1, 1),
                valid_to=valid_to,
            )
            for index in range(count)
        ]

    def resolve_instrument_rules(
        self, instrument_id: UUID, *, as_of: date
    ) -> InstrumentRulesFacts | None:
        if not self.rules_present.get(instrument_id, False):
            return None
        return InstrumentRulesFacts(
            rule_package_id="cn-etf@1",
            requires_trading_status_facts=self.requires_trading_status[
                instrument_id
            ],
        )

    def resolve_settlement_and_sell_rules(
        self, instrument_id: UUID, *, as_of: date
    ) -> SettlementAndSellRules | None:
        if (
            instrument_id not in self.settlement_rule_ids
            and instrument_id not in self.sell_rule_ids
        ):
            return None
        return SettlementAndSellRules(
            settlement_rule_id=self.settlement_rule_ids.get(instrument_id),
            sell_rule_id=self.sell_rule_ids.get(instrument_id),
            settlement_rule_kind=self.settlement_rule_kinds.get(instrument_id),
        )

    def find_first_trading_session_on_or_after(
        self, calendar_id: str, start_date: date
    ) -> date | None:
        for session in self.sessions:
            if session >= start_date:
                return session
        return None

    def get_raw_valuation_price(
        self, instrument_id: UUID, session: date
    ) -> RawValuationPrice | None:
        self.valuation_price_calls.append((instrument_id, session))
        if session not in self.sessions:
            raise AssertionError(f"price queried for non-session {session}")
        bound_session = self.price_session_override or session
        return RawValuationPrice(session=bound_session, price=Decimal(self.price_value))

    def check_required_corporate_actions(
        self, instrument_id: UUID, *, start_date: date, end_date: date
    ) -> FactCheckOutcome:
        complete = self.corporate_action_outcomes.get(instrument_id, False)
        return FactCheckOutcome(complete=complete)

    def check_required_trading_status(
        self, instrument_id: UUID, *, start_date: date, end_date: date
    ) -> FactCheckOutcome:
        complete = self.trading_status_outcomes.get(instrument_id, False)
        return FactCheckOutcome(complete=complete)


class PreflightTestCase(unittest.TestCase):
    """Shared helpers for building specs against a fake world."""

    def setUp(self) -> None:
        self.instrument_a = uuid4()
        self.instrument_b = uuid4()

    def make_position(
        self,
        instrument_id: UUID | None = None,
        quantity: Decimal | int | str = "100",
        available_quantity: Decimal | int | str = "40",
        average_price: Decimal | int | str | None = "10.50",
    ) -> InitialPositionInput:
        return InitialPositionInput(
            instrument_id=instrument_id or self.instrument_a,
            side=PositionSide.LONG,
            quantity=quantity,
            available_quantity=available_quantity,
            average_price=average_price,
        )

    def make_spec(
        self,
        positions: list[InitialPositionInput],
        *,
        start: date = START,
        end: date = END,
        dynamic_universe: bool = False,
    ) -> BacktestSpec:
        return BacktestSpec(
            start_date=start,
            end_date=end,
            initial_cash="1000000",
            initial_positions=positions,
            dynamic_universe=dynamic_universe,
        )


class ReadyPathTestCase(PreflightTestCase):
    def test_ready_when_all_facts_are_complete(self) -> None:
        spec = self.make_spec(
            [
                self.make_position(),
                self.make_position(instrument_id=self.instrument_b),
            ]
        )
        report = InitialPositionPreflightService(
            FakeGateway([self.instrument_a, self.instrument_b])
        ).run(spec)

        self.assertIs(report.status, PreflightStatus.READY)
        self.assertFalse(report.blocked)
        self.assertEqual(report.valuation_session, START)
        self.assertEqual(len(report.checked_positions), 2)
        self.assertEqual(report.issues, ())
        for result in report.checked_positions:
            self.assertIs(result.status, PreflightStatus.READY)
            self.assertEqual(result.raw_valuation_price, Decimal("12.34"))
            self.assertIs(result.identity_status, CheckStatus.OK)
            self.assertIs(result.rules_status, CheckStatus.OK)
            self.assertIs(result.settlement_status, CheckStatus.OK)
            self.assertIs(result.corporate_action_status, CheckStatus.OK)
            self.assertIs(result.trading_status, CheckStatus.OK)

    def test_report_implements_gateway_protocol_statically(self) -> None:
        # The fake is a pure in-memory object; it must satisfy the same
        # protocol a real data-layer adapter would implement.
        gateway = FakeGateway([self.instrument_a])
        self.assertIsInstance(gateway, BacktestPreflightGateway)

    def test_user_inputs_are_not_rewritten_in_results(self) -> None:
        spec = self.make_spec(
            [
                self.make_position(
                    quantity=1000,
                    available_quantity=250,
                    average_price="9.123456",
                )
            ]
        )
        report = InitialPositionPreflightService(FakeGateway([self.instrument_a])).run(spec)
        result = report.checked_positions[0]
        self.assertEqual(result.quantity, Decimal("1000"))
        self.assertEqual(result.available_quantity, Decimal("250"))
        self.assertEqual(result.average_price, Decimal("9.123456"))


class ValuationSessionTestCase(PreflightTestCase):
    def test_start_on_trading_day_uses_same_day_raw_price(self) -> None:
        spec = self.make_spec([self.make_position()])
        gateway = FakeGateway([self.instrument_a])
        report = InitialPositionPreflightService(gateway).run(spec)

        self.assertEqual(report.valuation_session, START)
        # Exactly one price read, bound to start_date itself.
        self.assertEqual(gateway.valuation_price_calls, [(self.instrument_a, START)])

    def test_start_on_holiday_uses_next_trading_session(self) -> None:
        spec = self.make_spec([self.make_position()], start=HOLIDAY)
        gateway = FakeGateway([self.instrument_a])
        report = InitialPositionPreflightService(gateway).run(spec)

        self.assertEqual(report.valuation_session, NEXT_SESSION)
        self.assertEqual(
            gateway.valuation_price_calls, [(self.instrument_a, NEXT_SESSION)]
        )
        self.assertIs(report.status, PreflightStatus.READY)

    def test_no_future_session_blocks_the_report(self) -> None:
        spec = self.make_spec(
            [self.make_position()],
            start=date(2027, 1, 15),
            end=date(2027, 3, 31),
        )
        gateway = FakeGateway([self.instrument_a])
        gateway.sessions = []  # calendar exhausted before the run starts
        report = InitialPositionPreflightService(gateway).run(spec)

        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertIsNone(report.valuation_session)
        codes = [issue.code for issue in report.issues]
        self.assertIn("NO_TRADING_SESSION", codes)
        result = report.checked_positions[0]
        self.assertIs(result.status, PreflightStatus.BLOCKED)
        self.assertIsNone(result.raw_valuation_price)

    def test_valuation_session_after_end_date_blocks(self) -> None:
        # start Saturday 2026-08-22, end Sunday 2026-08-23: the first trading
        # session (Monday 2026-08-24) falls outside the inclusive run window.
        spec = self.make_spec(
            [self.make_position()],
            start=HOLIDAY,
            end=date(2026, 8, 23),
        )
        gateway = FakeGateway([self.instrument_a])
        report = InitialPositionPreflightService(gateway).run(spec)

        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertIsNone(report.valuation_session)
        codes = [issue.code for issue in report.issues]
        self.assertIn("VALUATION_SESSION_AFTER_END_DATE", codes)
        result = report.checked_positions[0]
        self.assertIs(result.status, PreflightStatus.BLOCKED)
        self.assertIsNone(result.raw_valuation_price)
        # No price may be read for a session outside the run window.
        self.assertEqual(gateway.valuation_price_calls, [])

    def test_no_previous_value_fallback_is_read_ever(self) -> None:
        # Remove the start session itself from the price source: preflight
        # must block instead of silently reading an earlier session's price.
        spec = self.make_spec([self.make_position()])
        gateway = FakeGateway([self.instrument_a])
        original_getter = gateway.get_raw_valuation_price

        def missing_at_start(instrument_id: UUID, session: date):
            gateway.valuation_price_calls.append((instrument_id, session))
            if session == START:
                return None
            return original_getter(instrument_id, session)

        gateway.get_raw_valuation_price = missing_at_start  # type: ignore[method-assign]
        report = InitialPositionPreflightService(gateway).run(spec)

        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = [issue.code for issue in report.issues]
        self.assertEqual(codes, ["VALUATION_PRICE_MISSING"])
        # No other session was ever queried as a fallback.
        self.assertEqual(gateway.valuation_price_calls, [(self.instrument_a, START)])

    def test_mistyped_gateway_prices_block_without_raising(self) -> None:
        # A misbehaving adapter must produce a blocked report, never an
        # AttributeError or other crash inside the service.
        class BadPriceGateway(FakeGateway):
            def __init__(self, bad_price, **kwargs):
                super().__init__(**kwargs)
                self.bad_price = bad_price

            def get_raw_valuation_price(self, instrument_id, session):
                self.valuation_price_calls.append((instrument_id, session))
                return self.bad_price  # float or plain garbage value

        for bad_price in (12.34, "not-a-number"): 
            with self.subTest(price=bad_price):
                spec = self.make_spec([self.make_position()])
                gateway = BadPriceGateway(
                    bad_price, instrument_ids=[self.instrument_a]
                )
                report = InitialPositionPreflightService(gateway).run(spec)
                self.assertIs(report.status, PreflightStatus.BLOCKED)
                got_codes = [issue.code for issue in report.issues]
                if bad_price is None:
                    self.assertEqual(got_codes, ["VALUATION_PRICE_MISSING"])
                else:
                    self.assertEqual(got_codes, ["VALUATION_PRICE_INVALID"])
                result = report.checked_positions[0]
                self.assertIsNone(result.raw_valuation_price)

    def test_mistyped_gateway_session_blocks_without_raising(self) -> None:
        from app.backtesting.preflight import DomainValidationError

        # The value object itself rejects a non-date session.
        with self.assertRaises(DomainValidationError):
            RawValuationPrice(session="bad", price="12")

        # And a gateway returning such an object produces a blocked report
        # instead of an AttributeError inside the service.
        class BrokenShapeGateway(FakeGateway):
            def get_raw_valuation_price(self, instrument_id, session):
                self.valuation_price_calls.append((instrument_id, session))
                raw = RawValuationPrice.__new__(RawValuationPrice)
                object.__setattr__(raw, "session", "bad")
                object.__setattr__(raw, "price", Decimal("12"))
                return raw

        spec = self.make_spec([self.make_position()])
        report = InitialPositionPreflightService(BrokenShapeGateway([
            self.instrument_a
        ])).run(spec)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        got_codes = [issue.code for issue in report.issues]
        self.assertEqual(got_codes, ["VALUATION_PRICE_INVALID"])
        self.assertIsNone(report.checked_positions[0].raw_valuation_price)

    def test_blocked_report_hides_top_level_valuation_session(self) -> None:
        # Two positions: one fully ready, one blocked after its valuation
        # succeeded.  The top-level session must be None instead of the
        # surviving minimum.
        spec = self.make_spec(
            [
                self.make_position(),
                self.make_position(instrument_id=self.instrument_b),
            ]
        )
        gateway = FakeGateway([self.instrument_a, self.instrument_b])
        gateway.corporate_action_outcomes[self.instrument_b] = False
        report = InitialPositionPreflightService(gateway).run(spec)

        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertIsNone(report.valuation_session)
        statuses = {
            result.instrument_id: result.valuation_session
            for result in report.checked_positions
        }
        # Per-position sessions keep their own resolved values; only the
        # report-level session is suppressed.
        self.assertEqual(statuses[self.instrument_a], START)
        self.assertEqual(statuses[self.instrument_b], START)

    def test_adjusted_or_zero_prices_are_not_accepted_as_valid(self) -> None:
        # A stale price bound to an earlier session (e.g. adjusted series or
        # previous-value substitution) must be rejected explicitly.
        stale_gateway = FakeGateway(
            [self.instrument_a], price_session_override=date(2026, 8, 20)
        )
        stale_report = InitialPositionPreflightService(stale_gateway).run(
            self.make_spec([self.make_position()])
        )
        codes = [issue.code for issue in stale_report.issues]
        self.assertEqual(codes, ["VALUATION_PRICE_STALE"])
        self.assertIs(stale_report.status, PreflightStatus.BLOCKED)

        for bad_value in ("0", "-3.5", "NaN"):
            with self.subTest(price=bad_value):
                gateway = FakeGateway([self.instrument_a], price_value=bad_value)
                report = InitialPositionPreflightService(gateway).run(
                    self.make_spec([self.make_position()])
                )
                got_codes = [issue.code for issue in report.issues]
                self.assertEqual(got_codes, ["VALUATION_PRICE_INVALID"])
                self.assertIs(report.status, PreflightStatus.BLOCKED)


class FactCompletenessTestCase(PreflightTestCase):
    def assert_blocked_with_codes(
        self, gateway: FakeGateway, expected_codes: set[str]
    ) -> None:
        spec = self.make_spec([self.make_position()])
        report = InitialPositionPreflightService(gateway).run(spec)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(report.issues)
        got_codes = {issue.code for issue in report.issues}
        self.assertTrue(
            expected_codes <= got_codes,
            f"expected subset {expected_codes} of {got_codes}",
        )
        for issue in report.issues:
            self.assertEqual(issue.instrument_id, self.instrument_a)
            self.assertIsNotNone(issue.field)
            self.assertTrue(issue.message)
        self.assertIs(report.checked_positions[0].status, PreflightStatus.BLOCKED)

    def test_missing_instrument_blocks_identity_chain(self) -> None:
        spec = self.make_spec([self.make_position()])
        gateway = FakeGateway([])  # instrument unknown at target time
        report = InitialPositionPreflightService(gateway).run(spec)
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertIn("INSTRUMENT_NOT_FOUND", [
            issue.code for issue in report.issues
        ])

    def test_missing_identity_mapping_blocks(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.identity_mapping_counts[self.instrument_a] = 0
        self.assert_blocked_with_codes(gateway, {"IDENTITY_MAPPING_MISSING"})

    def test_conflicting_identity_mapping_blocks(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.identity_mapping_counts[self.instrument_a] = 2
        self.assert_blocked_with_codes(gateway, {"IDENTITY_MAPPING_CONFLICT"})

    def test_missing_rules_package_blocks(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.rules_present[self.instrument_a] = False
        self.assert_blocked_with_codes(gateway, {"RULES_PACKAGE_MISSING"})

    def test_whole_settlement_packet_missing_blocks_both_rules(self) -> None:
        spec = self.make_spec([self.make_position()])
        gateway = FakeGateway([self.instrument_a])
        gateway.settlement_rule_ids.pop(self.instrument_a)
        gateway.sell_rule_ids.pop(self.instrument_a)
        report = InitialPositionPreflightService(gateway).run(spec)
        codes = {issue.code for issue in report.issues}
        self.assertIn("SETTLEMENT_RULE_MISSING", codes)
        self.assertIn("SELL_RULE_MISSING", codes)

    def test_individual_missing_settlement_or_sell_rule_blocks(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.settlement_rule_ids[self.instrument_a] = None
        self.assert_blocked_with_codes(gateway, {"SETTLEMENT_RULE_MISSING"})

        gateway = FakeGateway([self.instrument_a])
        gateway.sell_rule_ids[self.instrument_a] = None
        self.assert_blocked_with_codes(gateway, {"SELL_RULE_MISSING"})

    def test_unsupported_settlement_kind_blocks(self) -> None:
        for bad_kind in ("t_plus_2_unknown", "", None):
            with self.subTest(kind=bad_kind):
                gateway = FakeGateway([self.instrument_a])
                gateway.settlement_rule_kinds[self.instrument_a] = bad_kind
                self.assert_blocked_with_codes(
                    gateway, {"SETTLEMENT_RULE_UNSUPPORTED"}
                )

    def test_supported_settlement_kind_stays_ready(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.settlement_rule_kinds[self.instrument_a] = (
            SettlementRuleKind.T1_BEFORE_OPEN_MATCH.value
        )
        spec = self.make_spec([self.make_position()])
        report = InitialPositionPreflightService(gateway).run(spec)
        self.assertIs(report.status, PreflightStatus.READY)

    def test_identity_mapping_window_not_covering_start_blocks(self) -> None:
        # Entries exist but their validity windows do not cover start_date;
        # the service must not trust them as PIT-valid.
        gateway = FakeGateway([self.instrument_a])
        gateway.identity_mapping_counts[self.instrument_a] = 1
        gateway.identity_windows[self.instrument_a] = (
            date(2020, 1, 1),
            date(2025, 12, 31),
        )
        self.assert_blocked_with_codes(gateway, {"IDENTITY_MAPPING_MISSING"})

    def test_identity_mapping_window_covering_start_is_accepted(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.identity_windows[self.instrument_a] = (date(2000, 1, 1), END)
        spec = self.make_spec([self.make_position()])
        report = InitialPositionPreflightService(gateway).run(spec)
        self.assertIs(report.status, PreflightStatus.READY)

    def test_missing_corporate_action_facts_block(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.corporate_action_outcomes[self.instrument_a] = False
        self.assert_blocked_with_codes(
            gateway, {"CORPORATE_ACTION_FACTS_MISSING"}
        )

    def test_missing_declared_trading_status_facts_block(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.trading_status_outcomes[self.instrument_a] = False
        self.assert_blocked_with_codes(gateway, {"TRADING_STATUS_FACTS_MISSING"})

    def test_trading_status_not_applicable_is_recorded_and_run_continues(self) -> None:
        gateway = FakeGateway([self.instrument_a])
        gateway.requires_trading_status[self.instrument_a] = False
        spec = self.make_spec([self.make_position()])
        report = InitialPositionPreflightService(gateway).run(spec)

        self.assertIs(report.status, PreflightStatus.READY)
        result = report.checked_positions[0]
        self.assertIs(result.trading_status, CheckStatus.NOT_APPLICABLE)
        self.assertEqual(
            [issue.code for issue in result.issues],
            [],
        )

    def test_dynamic_universe_never_skips_initial_position_checks(self) -> None:
        spec = self.make_spec(
            [
                self.make_position(),
                self.make_position(instrument_id=self.instrument_b),
            ],
            dynamic_universe=True,
        )
        gateway = FakeGateway([self.instrument_a, self.instrument_b])
        # Break facts only on the second position: a candidate-filtered
        # preflight would miss this; the mandatory scope must catch it.
        gateway.corporate_action_outcomes[self.instrument_b] = False
        report = InitialPositionPreflightService(gateway).run(spec)

        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertEqual(len(report.checked_positions), 2)
        blocked = [
            result
            for result in report.checked_positions
            if result.status is PreflightStatus.BLOCKED
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].instrument_id, self.instrument_b)
        codes = {issue.code for issue in report.issues}
        self.assertIn("CORPORATE_ACTION_FACTS_MISSING", codes)


class ReportContractTestCase(PreflightTestCase):
    def test_issue_ordering_is_stable(self) -> None:
        spec = self.make_spec(
            [
                self.make_position(instrument_id=self.instrument_b),
                self.make_position(instrument_id=self.instrument_a),
            ]
        )
        gateway = FakeGateway([self.instrument_a, self.instrument_b])
        gateway.identity_mapping_counts[self.instrument_a] = 0
        gateway.rules_present[self.instrument_b] = False
        report = InitialPositionPreflightService(gateway).run(spec)

        keys = [
            (str(issue.instrument_id), issue.code, issue.field or "")
            for issue in report.issues
        ]
        self.assertEqual(keys, sorted(keys))
        position_ids = [
            str(result.instrument_id) for result in report.checked_positions
        ]
        self.assertEqual(position_ids, sorted(position_ids))

    def test_equivalent_inputs_produce_identical_hashes(self) -> None:
        # Same semantic inputs built through different numeric types and a
        # different input ordering must hash identically.
        positions_int = [
            self.make_position(quantity=100, average_price="10.5"),
            self.make_position(
                instrument_id=self.instrument_b, quantity="200", average_price="3.30"
            ),
        ]
        positions_str = list(reversed(positions_int))
        spec_one = self.make_spec(positions_int)
        spec_two = self.make_spec(positions_str)

        report_one = InitialPositionPreflightService(
            FakeGateway([self.instrument_a, self.instrument_b])
        ).run(spec_one)
        report_two = InitialPositionPreflightService(
            FakeGateway([self.instrument_a, self.instrument_b])
        ).run(spec_two)

        self.assertEqual(report_one.report_hash, report_two.report_hash)
        self.assertEqual(
            report_one.canonical_content(), report_two.canonical_content()
        )
        self.assertEqual(len(report_one.report_hash), 64)

    def test_different_facts_change_the_hash(self) -> None:
        spec = self.make_spec([self.make_position()])
        ready_report = InitialPositionPreflightService(
            FakeGateway([self.instrument_a])
        ).run(spec)
        broken_gateway = FakeGateway([self.instrument_a])
        broken_gateway.rules_present[self.instrument_a] = False
        blocked_report = InitialPositionPreflightService(broken_gateway).run(spec)
        self.assertNotEqual(ready_report.report_hash, blocked_report.report_hash)

    def test_messages_do_not_affect_the_hash(self) -> None:
        from app.backtesting.preflight import IssueSeverity

        def build_report(message: str):
            issue = InitialPositionPreflightIssue(
                code="RULES_PACKAGE_MISSING",
                severity=IssueSeverity.ERROR,
                instrument_id=self.instrument_a,
                field="rule_package",
                message=message,
            )
            result = InitialPositionPreflightResult(
                instrument_id=self.instrument_a,
                side=PositionSide.LONG,
                quantity=Decimal("100"),
                available_quantity=Decimal("40"),
                average_price=Decimal("10.50"),
                valuation_session=None,
                raw_valuation_price=None,
                identity_status=CheckStatus.OK,
                rules_status=CheckStatus.BLOCKED,
                settlement_status=CheckStatus.OK,
                corporate_action_status=CheckStatus.OK,
                trading_status=CheckStatus.OK,
                status=PreflightStatus.BLOCKED,
                issues=(issue,),
            )
            from app.backtesting.preflight import InitialPositionPreflightReport

            return InitialPositionPreflightReport(
                status=PreflightStatus.BLOCKED,
                valuation_session=None,
                checked_positions=(result,),
                issues=(issue,),
                report_hash="",
            )

        # Same hashed content; only the display message wording differs.
        report_one = build_report("rule package missing")
        report_two = build_report("规则包缺失")
        self.assertNotEqual(
            report_one.issues[0].message, report_two.issues[0].message
        )
        self.assertEqual(report_one.report_hash, report_two.report_hash)
        self.assertNotIn("message", report_one.canonical_content()["issues"][0])

    def test_side_is_carried_into_results_and_affects_the_hash(self) -> None:
        def row(side: PositionSide) -> InitialPositionInput:
            return InitialPositionInput(
                instrument_id=self.instrument_a,
                side=side,
                quantity="100",
                available_quantity="40",
                average_price="10.5",
            )

        report_long = InitialPositionPreflightService(
            FakeGateway([self.instrument_a])
        ).run(self.make_spec([row(PositionSide.LONG)]))
        report_short = InitialPositionPreflightService(
            FakeGateway([self.instrument_a])
        ).run(self.make_spec([row(PositionSide.SHORT)]))

        self.assertIs(
            report_long.checked_positions[0].side, PositionSide.LONG
        )
        self.assertNotEqual(report_long.report_hash, report_short.report_hash)

    def test_zero_quantity_rows_never_appear_in_reports(self) -> None:
        zero_row = self.make_position(
            instrument_id=self.instrument_b,
            quantity="0",
            available_quantity="0",
            average_price=None,
        )
        spec = self.make_spec([zero_row, self.make_position()])
        report = InitialPositionPreflightService(
            FakeGateway([self.instrument_a])
        ).run(spec)

        self.assertIs(report.status, PreflightStatus.READY)
        self.assertEqual(len(report.checked_positions), 1)
        self.assertEqual(
            report.checked_positions[0].instrument_id, self.instrument_a
        )


if __name__ == "__main__":
    unittest.main()

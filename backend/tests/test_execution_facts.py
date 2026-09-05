"""Tests for execution-fact normalization and formal matching gates (04-03, 8A).

Covers the acceptance matrix of section 9.3 plus the explicit,
default-free ``MarketState`` construction path.
"""

from datetime import UTC, date, datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.data.facts import FactEvidence, TradingStatus
from app.backtesting.data.requests import QualityStatus
from app.backtesting.execution import (
    BarMarketExecutionModel,
    MatchContext,
    MarketState,
    Order,
    OrderStatus,
    PriceLimitStatus,
)
from app.backtesting.execution_facts import (
    CAPABILITY_DIMENSION_OPENING_AVAILABILITY as OPENING,
)
from app.backtesting.execution_facts import (
    CAPABILITY_DIMENSION_PRICE_LIMIT_TRADABILITY as LIMIT,
)
from app.backtesting.execution_facts import (
    CAPABILITY_DIMENSION_SUSPENSION as SUSPENSION,
)
from app.backtesting.execution_facts import (
    DirectionalAvailability,
    OpeningState,
    PriceLimitState,
    SuspensionState,
    evaluate_execution_facts,
    market_state_from_execution_facts,
)

INSTRUMENT_ID = uuid4()
CALENDAR_ID = "SSE"
SESSION = date(2026, 8, 25)
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)
OPEN_TS = datetime(2026, 8, 25, 1, 30, tzinfo=timezone.utc)

FORMAL_APPLICABILITY = {
    SUSPENSION: "required",
    OPENING: "required",
    LIMIT: "required",
}


def make_status(
    dimension: str,
    status: str,
    *,
    attributes: dict | None = None,
    quality: QualityStatus = QualityStatus.COMPLETE,
    known_at: datetime | None = datetime(2025, 12, 1, tzinfo=UTC),
    valid_from: date = date(2026, 1, 1),
    valid_to: date | None = None,
) -> TradingStatus:
    """One trading-status fact carrying its dimension marker."""

    payload = {"dimension": dimension}
    payload.update(attributes or {})
    return TradingStatus(
        instrument_id=INSTRUMENT_ID,
        status=status,
        valid_from=valid_from,
        evidence=FactEvidence(
            source="exchange_status_feed",
            observed_at=datetime(2026, 8, 21, tzinfo=UTC),
            quality_status=quality,
            known_at=known_at,
            source_revision="rev-7",
        ),
        attributes=payload,
        valid_to=valid_to,
    )


LIMIT_FACT = dict(attributes={"buy_allowed": False, "sell_allowed": True})


def full_required_facts(**overrides):
    """A complete visible fact set for one session."""

    return [
        make_status(SUSPENSION, "tradable"),
        make_status(OPENING, "available"),
        make_status(LIMIT, "none", **{
            "attributes": {"buy_allowed": True, "sell_allowed": True}
        }),
    ]


class NormalizationGateTestCase(unittest.TestCase):
    """Missing, incomplete, conflicting, and invisible required facts."""

    def evaluate(self, applicability=None, facts=None):
        return evaluate_execution_facts(
            INSTRUMENT_ID,
            calendar_id=CALENDAR_ID,
            session_date=SESSION,
            applicability=(
                FORMAL_APPLICABILITY if applicability is None else applicability
            ),
            status_facts=full_required_facts() if facts is None else facts,
            data_cutoff=CUTOFF,
            rule_package_reference="china_listed_etf_rules@1",
        )

    def _assert_single_issue(self, result, code, dimension):
        resolved, issues = result
        self.assertIsNone(resolved)
        matching = [i for i in issues if i.code == code]
        self.assertEqual(len(matching), 1, [i.code for i in issues])
        self.assertEqual(matching[0].dimension, dimension)

    def test_complete_facts_normalize_to_typed_states(self) -> None:
        resolved, issues = self.evaluate()
        self.assertEqual(issues, ())
        assert resolved is not None
        self.assertEqual(resolved.suspension_state, SuspensionState.TRADABLE)
        self.assertEqual(resolved.opening_state, OpeningState.AVAILABLE)
        self.assertEqual(resolved.buy_allowed, DirectionalAvailability.YES)
        self.assertEqual(resolved.sell_allowed, DirectionalAvailability.YES)
        self.assertEqual(resolved.price_limit_status, PriceLimitState.NONE)
        # Evidence records provenance for the report/snapshot surface.
        suspension_evidence = resolved.evidence[SUSPENSION]
        self.assertEqual(suspension_evidence["source"], "exchange_status_feed")
        self.assertEqual(
            suspension_evidence["rule_package_reference"],
            "china_listed_etf_rules@1",
        )

    def test_required_suspension_fact_missing_blocks(self) -> None:
        self._assert_single_issue(
            self.evaluate(facts=[]),
            "trading_status_fact_missing",
            SUSPENSION,
        )

    def test_required_opening_availability_missing_blocks(self) -> None:
        facts = [
            make_status(SUSPENSION, "tradable"),
            make_status(LIMIT, "none", **{
                "attributes": {"buy_allowed": True, "sell_allowed": True}
            }),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "opening_availability_fact_missing",
            OPENING,
        )

    def test_required_price_limit_direction_missing_blocks(self) -> None:
        facts = [
            make_status(SUSPENSION, "tradable"),
            make_status(OPENING, "available"),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "price_limit_tradability_fact_missing",
            LIMIT,
        )

    def test_known_at_after_cutoff_is_invisible_and_blocks(self) -> None:
        facts = [
            make_status(SUSPENSION, "tradable", known_at=datetime(2026, 8, 23, tzinfo=UTC)),
            make_status(OPENING, "available"),
            make_status(LIMIT, "none", **{
                "attributes": {"buy_allowed": True, "sell_allowed": True}
            }),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "trading_status_fact_missing",
            SUSPENSION,
        )

    def test_known_at_none_is_not_strict_evidence_and_blocks(self) -> None:
        facts = [
            make_status(SUSPENSION, "tradable", known_at=None),
            make_status(OPENING, "available"),
            make_status(LIMIT, "none", **{
                "attributes": {"buy_allowed": True, "sell_allowed": True}
            }),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "trading_status_fact_missing",
            SUSPENSION,
        )

    def test_non_complete_quality_blocks(self) -> None:
        facts = [
            make_status(SUSPENSION, "tradable", quality=QualityStatus.PARTIAL),
            make_status(OPENING, "available"),
            make_status(LIMIT, "none", **{
                "attributes": {"buy_allowed": True, "sell_allowed": True}
            }),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "trading_status_fact_not_complete",
            SUSPENSION,
        )

    def test_conflicting_suspension_facts_block(self) -> None:
        facts = [
            make_status(SUSPENSION, "tradable"),
            make_status(SUSPENSION, "suspended", valid_from=date(2026, 8, 25)),
            make_status(OPENING, "available"),
            make_status(LIMIT, "none", **{
                "attributes": {"buy_allowed": True, "sell_allowed": True}
            }),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "trading_status_fact_conflict",
            SUSPENSION,
        )

    def test_directional_disagreement_with_same_limit_blocks(self) -> None:
        # Same limit status ("none") but different directional availability:
        # the whole (status, buy, sell) triple must conflict, regardless of
        # input order — no first-write-wins.
        fact_a = make_status(LIMIT, "none", attributes={
            "buy_allowed": False, "sell_allowed": True,
        })
        fact_b = make_status(
            LIMIT, "none", valid_from=date(2026, 8, 25),
            attributes={"buy_allowed": True, "sell_allowed": True},
        )

        for facts in ([fact_a, fact_b], [fact_b, fact_a]):
            resolved, issues = self.evaluate(facts=facts)
            self.assertIsNone(resolved)
            matching = [
                i for i in issues
                if i.code == "trading_status_fact_conflict"
                and i.dimension == LIMIT
            ]
            self.assertEqual(len(matching), 1, [i.code for i in issues])

    def test_declared_not_applicable_with_present_facts_blocks(self) -> None:
        # A dimension declared away must never be silently overridden by
        # facts that arrived anyway: the disagreement fails closed.
        applicability = {SUSPENSION: "required", OPENING: "required", LIMIT: "not_applicable"}
        facts = [
            make_status(SUSPENSION, "tradable"),
            make_status(OPENING, "available"),
            make_status(LIMIT, "up", attributes={
                "buy_allowed": False, "sell_allowed": True,
            }),
        ]

        resolved, issues = evaluate_execution_facts(
            INSTRUMENT_ID,
            calendar_id=CALENDAR_ID,
            session_date=SESSION,
            applicability=applicability,
            status_facts=facts,
            data_cutoff=CUTOFF,
            rule_package_reference="china_listed_etf_rules@1",
        )

        self.assertIsNone(resolved)
        matching = [
            i for i in issues
            if i.code == "trading_status_fact_conflict" and i.dimension == LIMIT
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].details["applicability"], "not_applicable")

    def test_unknown_status_value_blocks(self) -> None:
        facts = [
            make_status(SUSPENSION, "halted_maybe"),
            make_status(OPENING, "available"),
            make_status(LIMIT, "none", **{
                "attributes": {"buy_allowed": True, "sell_allowed": True}
            }),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "trading_status_fact_conflict",
            SUSPENSION,
        )

    def test_missing_dimension_marker_blocks(self) -> None:
        facts = [
            make_status("something_else", "tradable"),
            make_status(OPENING, "available"),
            make_status(LIMIT, "none", **{
                "attributes": {"buy_allowed": True, "sell_allowed": True}
            }),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "trading_status_fact_conflict",
            "something_else",
        )

    def test_foreign_instrument_facts_block_with_stable_code(self) -> None:
        # A complete suspension fact belonging to another instrument must
        # never gate this instrument's session.
        stranger = uuid4()
        foreign = TradingStatus(
            instrument_id=stranger,
            status="tradable",
            valid_from=date(2026, 1, 1),
            evidence=FactEvidence(
                source="exchange_status_feed",
                observed_at=datetime(2026, 8, 21, tzinfo=UTC),
                quality_status=QualityStatus.COMPLETE,
                known_at=datetime(2025, 12, 1, tzinfo=UTC),
            ),
            attributes={"dimension": SUSPENSION},
        )

        resolved, issues = self.evaluate(facts=[foreign])
        self.assertIsNone(resolved)
        matching = [
            i for i in issues
            if i.code == "trading_status_fact_instrument_mismatch"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            matching[0].details["fact_instrument_id"], str(stranger)
        )
        self.assertEqual(matching[0].details["instrument_id"], str(INSTRUMENT_ID))

    def test_price_limit_fact_without_direction_flags_block(self) -> None:
        facts = [
            make_status(SUSPENSION, "tradable"),
            make_status(OPENING, "available"),
            make_status(LIMIT, "up"),
        ]
        self._assert_single_issue(
            self.evaluate(facts=facts),
            "trading_status_fact_not_complete",
            LIMIT,
        )

    def test_missing_declaration_fails_closed(self) -> None:
        self._assert_single_issue(
            self.evaluate(applicability={SUSPENSION: "required", OPENING: "required"}),
            "trading_status_declaration_missing",
            LIMIT,
        )


class NotApplicableTestCase(unittest.TestCase):
    """Explicit declarations skip facts but are always recorded."""

    def test_declared_not_applicable_needs_no_fact(self) -> None:
        applicability = {SUSPENSION: "required", OPENING: "required", LIMIT: "not_applicable"}
        facts = [
            make_status(SUSPENSION, "tradable"),
            make_status(OPENING, "available"),
        ]

        resolved, issues = evaluate_execution_facts(
            INSTRUMENT_ID,
            calendar_id=CALENDAR_ID,
            session_date=SESSION,
            applicability=applicability,
            status_facts=facts,
            data_cutoff=CUTOFF,
            rule_package_reference="china_listed_etf_rules@1",
        )

        self.assertEqual(issues, ())
        assert resolved is not None
        self.assertEqual(resolved.price_limit_status, PriceLimitState.NOT_APPLICABLE)
        self.assertEqual(
            resolved.buy_allowed, DirectionalAvailability.NOT_APPLICABLE
        )
        limit_evidence = resolved.evidence[LIMIT]
        self.assertEqual(limit_evidence["applicability"], "not_applicable")
        self.assertEqual(
            limit_evidence["rule_package_reference"],
            "china_listed_etf_rules@1",
        )

    def test_declared_not_applicable_allows_both_sides_to_match(self) -> None:
        # "Not applicable" is not "not tradable": with the limit dimension
        # declared away, buy and sell orders must both keep matching, and
        # no directional expiry reason may appear.
        applicability = {SUSPENSION: "required", OPENING: "required", LIMIT: "not_applicable"}
        facts = [
            make_status(SUSPENSION, "tradable"),
            make_status(OPENING, "available"),
        ]
        resolved, issues = evaluate_execution_facts(
            INSTRUMENT_ID,
            calendar_id=CALENDAR_ID,
            session_date=SESSION,
            applicability=applicability,
            status_facts=facts,
            data_cutoff=CUTOFF,
            rule_package_reference="china_listed_etf_rules@1",
        )
        self.assertEqual(issues, ())

        state = market_state_from_execution_facts(
            resolved,
            open_price="10",
            price_tick="0.01",
            timestamp=OPEN_TS,
            contract_multiplier="10",
        )

        # The permissive mapping is explicit, never a silent default.
        self.assertTrue(state.buy_allowed)
        self.assertTrue(state.sell_allowed)
        self.assertFalse(state.is_suspended)
        self.assertTrue(state.open_available)
        self.assertIs(state.price_limit_status, PriceLimitStatus.NONE)
        self.assertEqual(state.contract_multiplier, Decimal("10"))
        # The declaration stays auditable in the provenance record.
        self.assertEqual(state.facts_basis[LIMIT]["applicability"], "not_applicable")

        from app.backtesting.fees import FeeCalculator, FeeSchedule
        from app.backtesting.slippage import BpsSlippageModel

        model = BarMarketExecutionModel(
            slippage_model=BpsSlippageModel(slippage_bps="0", price_tick="0.01"),
            fee_calculator=FeeCalculator(schedule=FeeSchedule(version=1, key="test", fee_rules=())),
        )
        context = MatchContext(currency="CNY", available_cash="100000")
        buy = Order(
            order_id=uuid4(), intent_id=uuid4(), instrument_id=INSTRUMENT_ID,
            side="buy", quantity="100", submitted_at=OPEN_TS,
        )
        sell = Order(
            order_id=uuid4(), intent_id=uuid4(), instrument_id=INSTRUMENT_ID,
            side="sell", quantity="100", submitted_at=OPEN_TS,
        )
        context.available_quantities[INSTRUMENT_ID] = Decimal("100")

        result = model.match([buy, sell], {INSTRUMENT_ID: state}, context)

        self.assertEqual(len(result.fills), 2)
        for order in (buy, sell):
            self.assertEqual(order.status, OrderStatus.FILLED)

    def test_declared_opening_not_applicable_does_not_expire_orders(self) -> None:
        applicability = {SUSPENSION: "required", OPENING: "not_applicable", LIMIT: "required"}
        facts = [
            make_status(SUSPENSION, "tradable"),
            make_status(LIMIT, "none", **{
                "attributes": {"buy_allowed": True, "sell_allowed": True}
            }),
        ]
        resolved, issues = evaluate_execution_facts(
            INSTRUMENT_ID,
            calendar_id=CALENDAR_ID,
            session_date=SESSION,
            applicability=applicability,
            status_facts=facts,
            data_cutoff=CUTOFF,
        )
        self.assertEqual(issues, ())
        self.assertEqual(resolved.opening_state.value, "not_applicable")

        state = market_state_from_execution_facts(
            resolved,
            open_price="10",
            price_tick="0.01",
            timestamp=OPEN_TS,
        )

        self.assertTrue(state.open_available)
        self.assertEqual(state.facts_basis[OPENING]["applicability"], "not_applicable")


class FormalMarketStateTestCase(unittest.TestCase):
    """Explicit construction and directional matching semantics."""

    def _facts(self, suspension="tradable", opening="available", limit=("none", True, True)):
        status, buy, sell = limit
        resolved, issues = evaluate_execution_facts(
            INSTRUMENT_ID,
            calendar_id=CALENDAR_ID,
            session_date=SESSION,
            applicability={SUSPENSION: "required", OPENING: "required", LIMIT: "required"},
            status_facts=[
                make_status(SUSPENSION, suspension),
                make_status(OPENING, opening),
                make_status(LIMIT, status, attributes={
                    "buy_allowed": buy, "sell_allowed": sell,
                }),
            ],
            data_cutoff=CUTOFF,
        )
        self.assertEqual(issues, ())
        assert resolved is not None
        return resolved

    def _model_and_context(self):
        from app.backtesting.fees import FeeCalculator, FeeSchedule
        from app.backtesting.slippage import BpsSlippageModel

        model = BarMarketExecutionModel(
            slippage_model=BpsSlippageModel(slippage_bps="0", price_tick="0.01"),
            fee_calculator=FeeCalculator(
                schedule=FeeSchedule(version=1, key="test", fee_rules=())
            ),
        )
        context = MatchContext(currency="CNY", available_cash="100000")
        return model, context

    def _order(self, side):
        return Order(
            order_id=uuid4(),
            intent_id=uuid4(),
            instrument_id=INSTRUMENT_ID,
            side=side,
            quantity="100",
            submitted_at=OPEN_TS,
        )

    def test_builder_sets_every_field_explicitly_and_records_provenance(self) -> None:
        state = market_state_from_execution_facts(
            self._facts(),
            open_price="10",
            price_tick="0.01",
            timestamp=OPEN_TS,
        )
        self.assertFalse(state.is_suspended)
        self.assertTrue(state.open_available)
        self.assertTrue(state.buy_allowed)
        self.assertTrue(state.sell_allowed)
        self.assertIs(state.price_limit_status, PriceLimitStatus.NONE)
        # Provenance travels into the market state for run snapshots and
        # leads with the locating identity.
        self.assertIn(SUSPENSION, state.facts_basis)
        self.assertEqual(
            state.facts_basis[SUSPENSION]["source"], "exchange_status_feed"
        )
        self.assertEqual(state.facts_basis["instrument_id"], str(INSTRUMENT_ID))
        self.assertEqual(state.facts_basis["calendar_id"], CALENDAR_ID)
        self.assertEqual(state.facts_basis["session_date"], SESSION.isoformat())

    def test_suspended_session_expires_orders_with_stable_reason(self) -> None:
        state = market_state_from_execution_facts(
            self._facts(suspension="suspended"),
            open_price="10",
            price_tick="0.01",
            timestamp=OPEN_TS,
        )
        model, context = self._model_and_context()
        order = self._order("buy")

        result = model.match([order], {INSTRUMENT_ID: state}, context)

        self.assertEqual(result.fills, ())
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertEqual(order.status_reason, "instrument_suspended")

    def test_open_unavailable_expires_instead_of_using_previous_close(self) -> None:
        state = market_state_from_execution_facts(
            self._facts(opening="unavailable"),
            open_price=None,
            price_tick="0.01",
            timestamp=OPEN_TS,
        )
        model, context = self._model_and_context()
        order = self._order("buy")

        result = model.match([order], {INSTRUMENT_ID: state}, context)

        self.assertEqual(result.fills, ())
        self.assertEqual(order.status_reason, "open_unavailable")
        # The previous close was never substituted.
        self.assertIsNone(state.open_price)

    def test_up_limit_blocks_buy_but_allows_sell(self) -> None:
        up_state = market_state_from_execution_facts(
            self._facts(limit=("up", False, True)),
            open_price="10",
            price_tick="0.01",
            timestamp=OPEN_TS,
        )
        self.assertIs(up_state.price_limit_status, PriceLimitStatus.UP)
        model, context = self._model_and_context()
        buy = self._order("buy")
        sell = self._order("sell")
        context.available_quantities[INSTRUMENT_ID] = Decimal("100")

        result = model.match([buy, sell], {INSTRUMENT_ID: up_state}, context)

        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].side.value, "sell")
        self.assertEqual(buy.status, OrderStatus.EXPIRED)
        self.assertEqual(buy.status_reason, "buy_unavailable_at_price_limit")

    def test_down_limit_blocks_sell_but_allows_buy(self) -> None:
        down_state = market_state_from_execution_facts(
            self._facts(limit=("down", True, False)),
            open_price="10",
            price_tick="0.01",
            timestamp=OPEN_TS,
        )
        model, context = self._model_and_context()
        sell = self._order("sell")

        result = model.match([sell], {INSTRUMENT_ID: down_state}, context)

        self.assertEqual(result.fills, ())
        self.assertEqual(sell.status_reason, "sell_unavailable_at_price_limit")

    def test_ohlc_shaping_a_limit_does_not_change_matching(self) -> None:
        # A bar that *looks* like a limit-up close carries no fact weight:
        # with an explicit none/up-down fact set of none/allow-both, both
        # sides keep matching normally.
        state = market_state_from_execution_facts(
            self._facts(limit=("none", True, True)),
            open_price="10",
            price_tick="0.01",
            timestamp=OPEN_TS,
        )
        model, context = self._model_and_context()
        buy = self._order("buy")

        result = model.match([buy], {INSTRUMENT_ID: state}, context)

        self.assertEqual(len(result.fills), 1)


if __name__ == "__main__":
    unittest.main()

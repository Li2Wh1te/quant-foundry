use super::support::*;
use qf_core::matching::FillAllowance;

#[test]
fn same_ns_order_after_event_cannot_replay_trigger_but_next_sequence_is_eligible() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    let mut matcher = ticks(ExecutionModel::TradeTickV1, "1", "0");
    let first = print(row, 1, "10", 100);
    tick_commit(&mut m, &mut matcher, &first, &rules(row, "A"));
    let new = buy(&mut m, 10, 1, first.key().time_ns);
    // Independent matcher also checks eligibility; timestamp equality alone
    // cannot select the trigger, or reject the later sequence.
    let mut another = ticks(ExecutionModel::TradeTickV1, "1", "0");
    assert!(
        another
            .consume_with_budget(&first, std::slice::from_ref(&new), &rules(row, "A"), &m)
            .unwrap()
            .fills
            .is_empty()
    );
    let second = print(row, 2, "10", 100);
    assert_eq!(
        tick_commit(&mut m, &mut matcher, &second, &rules(row, "A")).fills[0].quantity,
        q(10)
    );
    code(
        matcher.consume_with_budget(&second, &[], &rules(row, "A"), &m),
        ErrorCode::InvalidContract,
    );
    let mut older = print(row, 3, "10", 100);
    if let MarketEvent::TradeTick(t) = &mut older {
        t.time_ns = ns(first.key().time_ns.get() - 1);
    }
    code(
        matcher.consume_with_budget(&older, &[], &rules(row, "A"), &m),
        ErrorCode::InvalidContract,
    );
}

#[test]
fn explicit_price_direction_limits_slippage_and_zero_print() {
    let row = &rows()[0];
    // Hand oracles for the configured adverse one-tick rounding. A violated
    // limit does not invent a different price by capping the slipped print.
    for (side, limit, bps, expected) in [
        (Side::Buy, None, "5", Some("10.01")),
        (Side::Sell, None, "5", Some("9.99")),
        (Side::Buy, Some("10"), "5", None),
        (Side::Sell, Some("10"), "5", None),
        (Side::Buy, Some("10"), "0", Some("10")),
        (Side::Sell, Some("10"), "0", Some("10")),
        (
            Side::Buy,
            None,
            "0.0000000000000000000000000001",
            Some("10.01"),
        ),
    ] {
        let mut m = manager("10000", Facts::default());
        if side == Side::Sell {
            seed(&mut m, 10);
        }
        let now = time(row, 572);
        submit_at(&mut m, "A", side, 10, limit, TimeInForce::Gtc, 2, now);
        let mut event = print(row, 2, "10", 100);
        if let MarketEvent::TradeTick(t) = &mut event {
            t.time_ns = now;
        }
        let out = tick_commit(
            &mut m,
            &mut ticks(ExecutionModel::TradeTickV1, "1", bps),
            &event,
            &rules(row, "A"),
        );
        assert_eq!(out.fills.first().map(|f| f.price), expected.map(p));
    }
    for (side, price) in [(Side::Buy, "11"), (Side::Sell, "9")] {
        let mut m = manager("10000", Facts::default());
        if side == Side::Sell {
            seed(&mut m, 10);
        }
        let now = time(row, 572);
        submit_at(&mut m, "A", side, 10, None, TimeInForce::Gtc, 2, now);
        let mut event = print(row, 2, price, 100);
        if let MarketEvent::TradeTick(t) = &mut event {
            t.time_ns = now;
        }
        let mut facts = day_facts(row, "A", TradingStatus::Trading);
        facts.price_limit = DailyPriceLimit::Limited {
            rate: d("0.1"),
            lower: p("9"),
            upper: p("11"),
        };
        assert!(
            tick_commit(
                &mut m,
                &mut ticks(ExecutionModel::TradeTickV1, "1", "0"),
                &event,
                &rules_with(row, "A", "0.01", facts).unwrap()
            )
            .fills
            .is_empty()
        );
    }
    let mut m = manager("10000", Facts::default());
    buy(&mut m, 10, 1, row.before_open_ns);
    assert!(
        tick_commit(
            &mut m,
            &mut ticks(ExecutionModel::TradeTickV1, "1", "0"),
            &print(row, 1, "10", 0),
            &rules(row, "A")
        )
        .fills
        .is_empty()
    );
}

#[test]
fn crossed_or_malformed_quotes_unknown_rules_and_raw_price_grid_fail_without_consumption() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    buy(&mut m, 10, 1, row.before_open_ns);
    let before = m.value(row.before_open_ns).unwrap();
    for event in [
        quote(row, 1, Some("11"), Some("10"), Some(100), Some(100)),
        quote(row, 1, None, Some("10"), Some(100), Some(100)),
    ] {
        let mut matcher = ticks(ExecutionModel::QuoteTickV1, "1", "0");
        code(
            matcher.consume_with_budget(&event, &m.open_orders(), &rules(row, "A"), &m),
            ErrorCode::InvalidContract,
        );
        let valid = quote(row, 1, Some("9"), Some("10"), Some(100), Some(100));
        assert_eq!(
            matcher
                .consume_with_budget(&valid, &m.open_orders(), &rules(row, "A"), &m)
                .unwrap()
                .fills[0]
                .quantity,
            q(10)
        );
    }
    let mut unknown = day_facts(row, "A", TradingStatus::Unknown);
    code(
        rules_with(row, "A", "0.01", unknown.clone()),
        ErrorCode::RuleUnavailable,
    );
    unknown.status = TradingStatus::Trading;
    unknown.price_limit = DailyPriceLimit::Unknown;
    code(
        rules_with(row, "A", "0.01", unknown),
        ErrorCode::RuleUnavailable,
    );
    code(
        ticks(ExecutionModel::TradeTickV1, "1", "0").consume_with_budget(
            &print(row, 1, "10.001", 10),
            &m.open_orders(),
            &rules(row, "A"),
            &m,
        ),
        ErrorCode::InvalidOrder,
    );
    assert_eq!(m.value(row.before_open_ns).unwrap(), before);
    let halted = rules_with(row, "A", "0.01", day_facts(row, "A", TradingStatus::Halted)).unwrap();
    assert!(
        ticks(ExecutionModel::TradeTickV1, "1", "0")
            .consume_with_budget(&print(row, 1, "10", 10), &m.open_orders(), &halted, &m)
            .unwrap()
            .fills
            .is_empty()
    );
}

struct Broken<'a> {
    manager: &'a Manager,
    id: Option<&'a str>,
    fail_later: bool,
}
impl FillBudget for Broken<'_> {
    fn allowance(
        &self,
        id: &str,
        qty: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        self.manager.allowance(id, qty, price, step)
    }
    fn trade_id(&self, offset: usize) -> QfResult<String> {
        self.id
            .map(|id| Ok(id.into()))
            .unwrap_or_else(|| self.manager.trade_id(offset))
    }
    fn candidate_allowance_after(
        &self,
        prior: &[Fill],
        candidate: &Fill,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        if self.fail_later && !prior.is_empty() {
            return Err(QfError::new(
                ErrorCode::RuleUnavailable,
                "synthetic_late_failure",
                "合成后序候选拒绝",
            ));
        }
        self.manager
            .candidate_allowance_after(prior, candidate, step)
    }
}
#[test]
fn late_budget_errors_and_invalid_duplicate_trade_ids_leave_quote_and_account_retryable() {
    let row = &rows()[0];
    for id in [
        Some(""),
        Some(" invalid "),
        Some("qf-trade-00000000000000000001"),
        None,
    ] {
        let mut m = manager("10000", Facts::default());
        buy(&mut m, 60, 1, row.before_open_ns);
        buy(&mut m, 60, 2, row.before_open_ns);
        let mut matcher = ticks(ExecutionModel::QuoteTickV1, "1", "0");
        let event = quote(row, 1, None, Some("10"), None, Some(100));
        let orders = m.open_orders();
        let before = m.value(row.before_open_ns).unwrap();
        code(
            matcher.consume_with_budget(
                &event,
                &orders,
                &rules(row, "A"),
                &Broken {
                    manager: &m,
                    id,
                    fail_later: id.is_none(),
                },
            ),
            if id.is_none() {
                ErrorCode::RuleUnavailable
            } else {
                ErrorCode::InvalidContract
            },
        );
        assert_eq!(m.open_orders(), orders);
        assert_eq!(m.value(row.before_open_ns).unwrap(), before);
        let out = tick_commit(&mut m, &mut matcher, &event, &rules(row, "A"));
        assert_eq!(
            out.fills
                .iter()
                .map(|f| f.quantity.get())
                .collect::<Vec<_>>(),
            vec![60, 40]
        );
    }
}

#[test]
fn next_session_requires_new_quote_and_gtc_remains_on_unique_account() {
    let calendar = rows();
    let row = &calendar[0];
    let next = &calendar[1];
    let mut m = manager("10000", Facts::default());
    let mut matcher = ticks(ExecutionModel::QuoteTickV1, "1", "0");
    buy(&mut m, 200, 1, row.before_open_ns);
    tick_commit(
        &mut m,
        &mut matcher,
        &quote(row, 1, None, Some("10"), None, Some(100)),
        &rules(row, "A"),
    );
    m.session_end(row).unwrap();
    m.expire_day(&row.session.key).unwrap();
    m.settle(&next.session.key).unwrap();
    m.session_start(next).unwrap();
    assert!(
        tick_commit(
            &mut m,
            &mut matcher,
            &quote(next, 1, None, None, None, None),
            &rules(next, "A")
        )
        .fills
        .is_empty()
    );
    let out = tick_commit(
        &mut m,
        &mut matcher,
        &quote(next, 2, None, Some("10"), None, Some(100)),
        &rules(next, "A"),
    );
    assert_eq!(out.fills[0].quantity, q(100));
    assert_eq!(out.fills[0].fee, d("0")); // same GTC minimum commission
}

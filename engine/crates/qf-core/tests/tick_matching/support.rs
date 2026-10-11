#![allow(dead_code)]
#[path = "../bar_matching/support.rs"]
mod bar_support;
pub use bar_support::*;
pub use qf_core::matching::tick::{TickMatcher, TickRules};
pub use qf_core::run::ExecutionModel;

pub fn tick_config(model: ExecutionModel, participation: &str, slippage: &str) -> RunConfig {
    RunConfig::from_json(
        &serde_json::json!({
            "api_schema":"qf.backtest.v2", "strategy_revision_id":MODEL, "account_id":MODEL,
            "initial_cash":"10000", "start":"2026-01-09", "end":"2026-02-11",
            "frequency":"tick", "universe":["A", "B"], "parameters":{},
            "execution_model":model, "reference_calendar":MODEL,
            "participation_rate":participation, "slippage_bps":slippage,
        })
        .to_string(),
    )
    .unwrap()
}
pub fn ticks(model: ExecutionModel, participation: &str, slippage: &str) -> TickMatcher {
    TickMatcher::from_run(&tick_config(model, participation, slippage)).unwrap()
}
pub fn print(row: &CalendarSession, seq: u64, price: &str, quantity: i64) -> MarketEvent {
    let mut event = tick(row, "A", seq, ns(time(row, 570).get() + 123), price);
    if let MarketEvent::TradeTick(t) = &mut event {
        t.quantity = q(quantity);
    }
    event
}
pub fn quote(
    row: &CalendarSession,
    seq: u64,
    bid: Option<&str>,
    ask: Option<&str>,
    bid_qty: Option<i64>,
    ask_qty: Option<i64>,
) -> MarketEvent {
    let MarketEvent::TradeTick(t) = print(row, seq, "10", 0) else {
        unreachable!()
    };
    MarketEvent::QuoteTick(QuoteTick {
        security: t.security,
        session: t.session,
        identity: t.identity,
        time_ns: t.time_ns,
        bid: bid.map(p),
        ask: ask.map(p),
        bid_quantity: bid_qty.map(q),
        ask_quantity: ask_qty.map(q),
        units: t.units,
    })
}
pub fn tick_commit(
    m: &mut Manager,
    matcher: &mut TickMatcher,
    event: &MarketEvent,
    rules: &TickRules,
) -> MatchOutcome {
    let orders = m.active_orders(&event.key().security, 10_000).unwrap();
    let claim = m.claim_match(event, &orders).unwrap();
    let result = matcher
        .consume_with_budget(event, &orders, rules, m)
        .unwrap();
    let result = m.commit_match(claim, result).unwrap();
    m.mark(event).unwrap();
    result
}
pub fn buy(m: &mut Manager, quantity: i64, seq: u64, now: Nanoseconds) -> Order {
    submit_at(
        m,
        "A",
        Side::Buy,
        quantity,
        None,
        TimeInForce::Gtc,
        seq,
        now,
    )
}

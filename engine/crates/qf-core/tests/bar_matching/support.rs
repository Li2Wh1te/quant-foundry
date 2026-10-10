#![allow(dead_code)]
// Synthetic inputs only. OrderManager, Account, RuleBook, cumulative fees and
// BarMatcher are real implementations. No production market-data claim.
#[path = "../orders/support.rs"]
mod orders;
pub use orders::*;
#[path = "../time/support.rs"]
pub mod isolated;
pub use qf_core::matching::Matcher;
pub use qf_core::matching::bar::{BarMatcher, BarRules};
use qf_core::rules::date::EffectiveRange;
pub use qf_core::run::{Frequency, RunConfig};

pub fn config(frequency: Frequency, participation: &str, slippage: &str, cash: &str) -> RunConfig {
    RunConfig::from_json(
        &serde_json::json!({
            "api_schema":"qf.backtest.v2", "strategy_revision_id":MODEL, "account_id":MODEL,
            "initial_cash":cash, "start":"2026-01-09", "end":"2026-02-11",
            "frequency":frequency, "universe":["A", "B"], "parameters":{},
            "execution_model":"bar_next_interval_v1", "reference_calendar":MODEL,
            "participation_rate":participation, "slippage_bps":slippage,
        })
        .to_string(),
    )
    .unwrap()
}
pub fn matcher(participation: &str, slippage: &str) -> BarMatcher {
    BarMatcher::from_run(&config(Frequency::Minute, participation, slippage, "10000")).unwrap()
}
pub fn book(tick: &str) -> RuleBook {
    RuleBook::new(vec![TradingRule {
        exchange: Exchange::Shanghai,
        product: Product::MainBoardStock,
        effective: EffectiveRange {
            from: date("2000-01-01"),
            through: date("2030-01-01"),
        },
        origin: origin(),
        session_template: SessionTemplate::ChinaAuction,
        price_tick: p(tick),
        minimum_buy: q(1),
        buy_step: QuantityStep::new(1).unwrap(),
        minimum_sell: q(1),
        sell_step: QuantityStep::new(1).unwrap(),
        maximum_limit: q(i64::MAX),
        maximum_market: q(i64::MAX),
        sell_availability: SellAvailability::SameSession,
        normal_limit_rate: d("0.1"),
        risk_limit_rate: d("0.1"),
        ipo_no_limit_sessions: 0,
        relisting_no_limit: false,
        risk_rules_verified: true,
        risk_buy_cap: None,
        risk_requires_limit_order: false,
        continuous_cage: None,
        no_limit_call_policy: NoLimitCallPolicy::ShanghaiMainBoard,
        market_requires_daily_limit: false,
        band_rounding: Some(RoundingPolicy::HalfUp),
    }])
    .unwrap()
}
pub fn day_facts(row: &CalendarSession, symbol: &str, status: TradingStatus) -> TradeDayFacts {
    oracle::facts(row, &Facts::instrument(symbol), status)
}
pub fn rules(row: &CalendarSession, symbol: &str) -> BarRules {
    rules_with(
        row,
        symbol,
        "0.01",
        day_facts(row, symbol, TradingStatus::Trading),
    )
    .unwrap()
}
pub fn rules_with(
    row: &CalendarSession,
    symbol: &str,
    tick: &str,
    facts: TradeDayFacts,
) -> QfResult<BarRules> {
    assert_eq!(facts.session, row.session.key);
    BarRules::new(&book(tick), Facts::instrument(symbol), facts, usage())
}
pub fn bar(
    row: &CalendarSession,
    symbol: &str,
    start_minute: i64,
    end_minute: i64,
    sequence: u64,
    prices: [&str; 4],
    volume: Option<i64>,
) -> MarketEvent {
    let MarketEvent::TradeTick(tick) =
        tick(row, symbol, sequence, time(row, end_minute), prices[0])
    else {
        unreachable!()
    };
    MarketEvent::Bar(Bar {
        security: tick.security,
        session: tick.session,
        identity: tick.identity,
        interval_start_ns: time(row, start_minute),
        interval_end_ns: time(row, end_minute),
        open: p(prices[0]),
        high: p(prices[1]),
        low: p(prices[2]),
        close: p(prices[3]),
        quantity: volume.map(q),
        units: tick.units,
    })
}
#[allow(clippy::too_many_arguments)]
pub fn submit_at(
    m: &mut Manager,
    symbol: &str,
    side: Side,
    quantity: i64,
    limit: Option<&str>,
    tif: TimeInForce,
    sequence: u64,
    now: Nanoseconds,
) -> Order {
    let row = rows()
        .into_iter()
        .find(|r| r.before_open_ns <= now && now <= r.after_close_ns)
        .unwrap();
    let result = m
        .validate_and_reserve(&OrderIntent {
            security: sec(symbol),
            side,
            value: IntentValue::Quantity(q(quantity)),
            style: limit
                .map(|price| OrderStyle::Limit { price: p(price) })
                .unwrap_or(OrderStyle::Market),
            tif,
            submitted_at: command(&row, symbol, sequence, now),
        })
        .unwrap();
    assert!(result.accepted, "{result:?}");
    m.get_order(result.order_id.as_ref().unwrap()).unwrap()
}
pub fn commit(
    m: &mut Manager,
    matcher: &mut BarMatcher,
    event: &MarketEvent,
    rules: &BarRules,
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
pub fn seed(m: &mut Manager, quantity: i64) {
    let row = &rows()[0];
    submit_at(
        m,
        "A",
        Side::Buy,
        quantity,
        None,
        TimeInForce::Gtc,
        1,
        row.before_open_ns,
    );
    commit(
        m,
        &mut matcher("1", "0"),
        &bar(
            row,
            "A",
            570,
            571,
            1,
            ["10", "12", "9", "10"],
            Some(quantity),
        ),
        &rules(row, "A"),
    );
}
pub fn oracles() -> serde_json::Value {
    let data: serde_json::Value = serde_json::from_str(include_str!("cases.json")).unwrap();
    assert_eq!(data["kind"], MODEL);
    assert_eq!(data["production_executed"], false);
    data
}

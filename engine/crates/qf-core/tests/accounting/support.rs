#![allow(dead_code)]
use qf_core::accounting::*;
use qf_core::clock::calendar::MINUTE_NS;
use qf_core::clock::{CalendarSession, TradingPeriod, TradingSession};
use qf_core::matching::Fill;
use qf_core::orders::*;
use qf_core::rules::date::{EffectiveRange, RuleDate};
use qf_core::rules::fees::{FeeScope, InvestorKind, ScopedFeeConfig};
use qf_core::rules::market::*;
use qf_core::rules::{DatedFeeComponent, FeeConfig};
use qf_core::types::market::Currency;
use qf_core::types::*;
use qf_core::{ErrorCode, QfResult};

pub const MODEL: &str = "synthetic_business_oracles";
pub fn d(s: &str) -> ExactDecimal {
    s.parse().unwrap()
}
pub fn p(s: &str) -> Price {
    s.parse().unwrap()
}
pub fn q(n: i64) -> Quantity {
    Quantity::new(n).unwrap()
}
pub fn sec(s: &str) -> SecurityKey {
    SecurityKey::new(s).unwrap()
}
pub fn key(s: &str) -> SessionKey {
    SessionKey::new(s).unwrap()
}
pub fn ns(n: i64) -> Nanoseconds {
    Nanoseconds::new(n)
}
pub fn date(s: &str) -> RuleDate {
    s.parse().unwrap()
}
pub fn origin() -> RuleOrigin {
    RuleOrigin::Synthetic(MODEL.into())
}
pub fn usage() -> RuleUse {
    RuleUse::Synthetic(MODEL.into())
}
pub fn code<T>(result: QfResult<T>, expected: ErrorCode) {
    match result {
        Err(e) => assert_eq!(e.code, expected, "{e:?}"),
        Ok(_) => panic!("expected {expected:?}"),
    }
}
pub fn row(index: usize, value: &str) -> CalendarSession {
    // Explicit isolated clock mappings; these are not official market sessions.
    let midnight = i64::try_from(index).unwrap() * 1440 * MINUTE_NS;
    CalendarSession {
        session: TradingSession {
            key: key(&format!("S{}", index + 1)),
            exchange_timezone: "Asia/Shanghai".into(),
            open_ns: ns(midnight + 555 * MINUTE_NS),
            close_ns: ns(midnight + 900 * MINUTE_NS),
        },
        date: date(value),
        local_midnight_ns: ns(midnight),
        before_open_ns: ns(midnight + 550 * MINUTE_NS),
        after_close_ns: ns(midnight + 901 * MINUTE_NS),
        market_windows: vec![],
        bar_windows: vec![],
        week: TradingPeriod {
            id: format!("synthetic-week-{index}"),
            trading_day: 1,
            total_trading_days: 1,
        },
        month: TradingPeriod {
            id: value[..7].into(),
            trading_day: u16::try_from(index + 1).unwrap(),
            total_trading_days: 31,
        },
    }
    .with_template(SessionTemplate::ChinaAuction)
    .unwrap()
}
pub fn rows() -> Vec<CalendarSession> {
    ["2026-01-09", "2026-01-12", "2026-02-10", "2026-02-11"]
        .iter()
        .enumerate()
        .map(|(i, d)| row(i, d))
        .collect()
}
pub fn at(row: &CalendarSession, offset: i64) -> Nanoseconds {
    ns(row.session.open_ns.get() + offset)
}
pub fn fee(
    scope: FeeScope,
    rate: &str,
    minimum: &str,
    components: Vec<DatedFeeComponent>,
) -> ScopedFeeConfig {
    ScopedFeeConfig::new(
        scope,
        FeeConfig {
            commission_rate: d(rate),
            minimum_commission: d(minimum),
            currency: Currency::CNY,
            settlement_scale: 2,
            rounding: RoundingPolicy::HalfEven,
            components,
            synthetic_model: Some(MODEL.into()),
        },
        &usage(),
    )
    .unwrap()
}
pub fn facts(
    row: &CalendarSession,
    instrument: &Instrument,
    status: TradingStatus,
) -> TradeDayFacts {
    TradeDayFacts {
        security: instrument.security.clone(),
        date: row.date.clone(),
        session: row.session.key.clone(),
        listing: Some(ListingWindow {
            first_trading_date: date("2000-01-01"),
            last_trading_date: None,
            verified_through: date("2030-01-01"),
            kind: ListingKind::Ipo,
            session_ordinal: 1000,
        }),
        status,
        risk: RiskState::Normal,
        price_limit: DailyPriceLimit::Limited {
            rate: d("0.1"),
            lower: p("0.01"),
            upper: p("1000000000000"),
        },
        fund_price_band: if instrument.product.is_etf() {
            Some(FundPriceBand::TenPercent)
        } else {
            None
        },
        underlying_same_session: None,
        delisting_session_ordinal: None,
    }
}
pub fn terms(
    row: &CalendarSession,
    security: &str,
    turnover: SellAvailability,
    rate: &str,
    minimum: &str,
) -> AccountTerms {
    terms_with(
        row,
        security,
        turnover,
        rate,
        minimum,
        TradingStatus::Trading,
        SaleProceedsTiming::Immediate,
        vec![],
    )
}
#[allow(clippy::too_many_arguments)]
pub fn terms_with(
    row: &CalendarSession,
    security: &str,
    turnover: SellAvailability,
    rate: &str,
    minimum: &str,
    status: TradingStatus,
    proceeds: SaleProceedsTiming,
    components: Vec<DatedFeeComponent>,
) -> AccountTerms {
    let instrument = Instrument {
        security: sec(security),
        exchange: Exchange::Shanghai,
        product: Product::MainBoardStock,
    };
    let book = RuleBook::new(vec![TradingRule {
        exchange: instrument.exchange,
        product: instrument.product,
        effective: EffectiveRange {
            from: date("2000-01-01"),
            through: date("2030-01-01"),
        },
        origin: origin(),
        session_template: SessionTemplate::ChinaAuction,
        price_tick: p("0.000000000001"),
        minimum_buy: q(1),
        buy_step: QuantityStep::new(1).unwrap(),
        minimum_sell: q(1),
        sell_step: QuantityStep::new(1).unwrap(),
        maximum_limit: q(i64::MAX),
        maximum_market: q(i64::MAX),
        sell_availability: turnover,
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
    .unwrap();
    let scope = |side| FeeScope {
        instrument: instrument.clone(),
        side,
        investor: InvestorKind::ResidentIndividual,
        origin: origin(),
    };
    let buy = fee(scope(Side::Buy), rate, minimum, components.clone());
    let sell = fee(scope(Side::Sell), rate, minimum, components);
    let mut t = AccountTerms::resolve(
        instrument.clone(),
        &book,
        facts(row, &instrument, status),
        buy,
        sell,
        SaleProceedsRule {
            timing: proceeds,
            basis: "explicit isolated sale-cash usability oracle".into(),
            origin: origin(),
        },
        usage(),
    )
    .unwrap();
    // This fixture explicitly defines transfer settlement as the supplied date.
    t.disposal_settlement_date = Some(row.date.clone());
    t
}
pub fn account(
    cash: &str,
    calendar: &[CalendarSession],
    turnover: SellAvailability,
    minimum: &str,
) -> Account {
    let mut a = Account::new(
        d(cash),
        calendar.to_vec(),
        usage(),
        AccountLimits::default(),
    )
    .unwrap();
    a.install_terms(terms(&calendar[0], "A", turnover, "0", minimum))
        .unwrap();
    a.settle(&calendar[0].session.key).unwrap();
    a
}
pub fn request(
    row: &CalendarSession,
    id: &str,
    side: Side,
    quantity: i64,
    price: &str,
    time: Nanoseconds,
) -> ReservationRequest {
    ReservationRequest {
        order_id: id.into(),
        security: sec("A"),
        side,
        quantity: q(quantity),
        estimate_price: p(price),
        limit_price: None,
        tif: TimeInForce::Gtc,
        effective_session: row.session.key.clone(),
        submitted_ns: time,
    }
}
pub fn fill(
    id: &str,
    trade: &str,
    side: Side,
    quantity: i64,
    price: &str,
    time: Nanoseconds,
) -> Fill {
    Fill {
        trade_id: trade.into(),
        order_id: id.into(),
        security: sec("A"),
        side,
        quantity: q(quantity),
        price: p(price),
        fee: d("0"),
        execution_time_ns: time,
    }
}
pub fn execute(a: &mut Account, candidate: Fill) -> Fill {
    let assessed = a.quote_fill(&candidate).unwrap();
    a.apply_fill(&assessed).unwrap();
    assessed
}
pub fn mark(a: &mut Account, row: &CalendarSession, price: &str, time: Nanoseconds) {
    a.set_raw_mark(sec("A"), raw(row, price, time), time)
        .unwrap();
}
pub fn raw(row: &CalendarSession, price: &str, time: Nanoseconds) -> ValuationMark {
    ValuationMark {
        price: p(price),
        price_time_ns: time,
        known_ns: time,
        session: row.session.key.clone(),
        basis: PriceBasis::Raw,
        source: "synthetic raw oracle, not adjusted research".into(),
        origin: origin(),
    }
}
pub fn trade(
    a: &mut Account,
    row: &CalendarSession,
    id: &str,
    side: Side,
    quantity: i64,
    price: &str,
    offset: i64,
) -> Fill {
    a.reserve_order(request(row, id, side, quantity, price, at(row, offset)))
        .unwrap();
    let f = execute(
        a,
        fill(
            id,
            &format!("{id}-fill"),
            side,
            quantity,
            price,
            at(row, offset + 1),
        ),
    );
    mark(a, row, price, at(row, offset + 1));
    f
}
pub fn boundary(row: &CalendarSession, phase: ActionPhase) -> ActionBoundary {
    ActionBoundary {
        session: row.session.key.clone(),
        phase,
    }
}
pub fn close(a: &mut Account, row: &CalendarSession) -> CorporateActionReport {
    a.advance_corporate_actions(&boundary(row, ActionPhase::SessionClose))
        .unwrap()
}
pub fn open(
    a: &mut Account,
    row: &CalendarSession,
    turnover: SellAvailability,
    minimum: &str,
) -> CorporateActionReport {
    a.settle(&row.session.key).unwrap();
    a.install_terms(terms(row, "A", turnover, "0", minimum))
        .unwrap();
    a.advance_corporate_actions(&boundary(row, ActionPhase::BeforeOpen))
        .unwrap()
}
pub fn dividend(calendar: &[CalendarSession], tax: CashDividendTax) -> CorporateAction {
    CorporateAction {
        action_id: "cash-dividend".into(),
        security: sec("A"),
        public_ns: calendar[0].before_open_ns,
        record: boundary(&calendar[0], ActionPhase::SessionClose),
        ex: boundary(&calendar[1], ActionPhase::BeforeOpen),
        sequence: 1,
        entitlement: EntitlementRule::AllHeld,
        kind: CorporateActionKind::CashDividend {
            per_share: d("0.2"),
            payment: boundary(&calendar[2], ActionPhase::BeforeOpen),
            tax,
            settlement: CashSettlement {
                scale: 2,
                rounding: RoundingPolicy::HalfEven,
            },
        },
        ex_raw_mark: Some(raw(&calendar[1], "9.8", calendar[1].before_open_ns)),
        origin: origin(),
    }
}
pub fn share_tax() -> Option<ShareTaxFact> {
    Some(ShareTaxFact {
        investor: InvestorKind::ResidentIndividual,
        per_entitled_share: d("0"),
        settlement: CashSettlement {
            scale: 2,
            rounding: RoundingPolicy::HalfEven,
        },
        basis: "explicit synthetic share-tax zero, not official exemption".into(),
    })
}
pub fn split(calendar: &[CalendarSession], ratio: ShareRatio, sellable: usize) -> CorporateAction {
    CorporateAction {
        action_id: "split".into(),
        security: sec("A"),
        public_ns: calendar[0].before_open_ns,
        record: boundary(&calendar[0], ActionPhase::SessionClose),
        ex: boundary(&calendar[1], ActionPhase::BeforeOpen),
        sequence: 2,
        entitlement: EntitlementRule::AllHeld,
        kind: CorporateActionKind::Split {
            ratio,
            sellable_session: calendar[sellable].session.key.clone(),
            tax: share_tax(),
        },
        ex_raw_mark: Some(raw(&calendar[1], "5", calendar[1].before_open_ns)),
        origin: origin(),
    }
}
pub fn oracle(id: &str) -> serde_json::Value {
    let document: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../contracts/accounting_cases.json"
    ))
    .unwrap();
    assert_eq!(document["kind"], MODEL);
    assert_eq!(document["production_executed"], false);
    document["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == id)
        .unwrap()
        .clone()
}
pub fn decimal(value: &serde_json::Value) -> ExactDecimal {
    d(value.as_str().unwrap())
}

#![allow(dead_code)]
#[path = "../accounting/support.rs"]
pub mod oracle;
pub use oracle::{MODEL, code, d, date, ns, origin, p, q, raw, rows, sec, usage};
pub use qf_core::accounting::*;
pub use qf_core::clock::CalendarSession;
use qf_core::clock::calendar::MINUTE_NS;
pub use qf_core::engine::ExecutionPort;
pub use qf_core::matching::{Fill, FillBudget, MatchOutcome};
pub use qf_core::orders::*;
use qf_core::rules::date::EffectiveRange;
use qf_core::rules::fees::{FeeScope, InvestorKind};
pub use qf_core::rules::market::*;
pub use qf_core::types::*;
pub use qf_core::{ErrorCode, QfError, QfResult};
use std::collections::BTreeSet;

#[derive(Clone)]
pub struct Facts {
    pub unit: i64,
    pub minimum: i64,
    pub fee: String,
    pub turnover: SellAvailability,
    pub symbols: Vec<String>,
    pub halted: BTreeSet<String>,
    pub missing: BTreeSet<String>,
    pub permission: Option<bool>,
    pub risk: RiskState,
    pub risk_cap: Option<Quantity>,
}
impl Default for Facts {
    fn default() -> Self {
        Self {
            unit: 1,
            minimum: 1,
            fee: "1".into(),
            turnover: SellAvailability::SameSession,
            symbols: vec!["A".into(), "B".into()],
            halted: BTreeSet::new(),
            missing: BTreeSet::new(),
            permission: Some(true),
            risk: RiskState::Normal,
            risk_cap: None,
        }
    }
}
impl Facts {
    pub fn instrument(symbol: &str) -> Instrument {
        Instrument {
            security: sec(symbol),
            exchange: Exchange::Shanghai,
            product: if symbol == "INDEX" {
                Product::Index
            } else {
                Product::MainBoardStock
            },
        }
    }
    pub fn terms(&self, row: &CalendarSession, symbol: &str) -> QfResult<AccountTerms> {
        let instrument = Self::instrument(symbol);
        let book = RuleBook::new(vec![TradingRule {
            exchange: Exchange::Shanghai,
            product: Product::MainBoardStock,
            effective: EffectiveRange {
                from: date("2000-01-01"),
                through: date("2030-01-01"),
            },
            origin: origin(),
            session_template: SessionTemplate::ChinaAuction,
            price_tick: p("0.01"),
            minimum_buy: q(self.minimum),
            buy_step: QuantityStep::new(self.unit)?,
            minimum_sell: q(self.minimum),
            sell_step: QuantityStep::new(self.unit)?,
            maximum_limit: q(i64::MAX),
            maximum_market: q(i64::MAX),
            sell_availability: self.turnover,
            normal_limit_rate: d("0.1"),
            risk_limit_rate: d("0.1"),
            ipo_no_limit_sessions: 0,
            relisting_no_limit: false,
            risk_rules_verified: true,
            risk_buy_cap: self.risk_cap,
            risk_requires_limit_order: false,
            continuous_cage: None,
            no_limit_call_policy: NoLimitCallPolicy::ShanghaiMainBoard,
            market_requires_daily_limit: false,
            band_rounding: Some(RoundingPolicy::HalfUp),
        }])?;
        let status = if self.halted.contains(row.session.key.as_str()) {
            TradingStatus::Halted
        } else {
            TradingStatus::Trading
        };
        let scope = |side| FeeScope {
            instrument: instrument.clone(),
            side,
            investor: InvestorKind::ResidentIndividual,
            origin: origin(),
        };
        let mut day_facts = oracle::facts(row, &instrument, status);
        day_facts.risk = self.risk;
        let mut terms = AccountTerms::resolve(
            instrument.clone(),
            &book,
            day_facts,
            oracle::fee(scope(Side::Buy), "0", &self.fee, vec![]),
            oracle::fee(scope(Side::Sell), "0", &self.fee, vec![]),
            SaleProceedsRule {
                timing: SaleProceedsTiming::Immediate,
                basis: "synthetic explicit sale-cash timing".into(),
                origin: origin(),
            },
            usage(),
        )?;
        terms.disposal_settlement_date = Some(row.date.clone());
        Ok(terms)
    }
}
impl OrderFacts for Facts {
    fn session_terms(&mut self, row: &CalendarSession) -> QfResult<Vec<AccountTerms>> {
        self.symbols
            .iter()
            .filter(|s| {
                s.as_str() != "INDEX"
                    && !self
                        .missing
                        .contains(&format!("{}:{s}", row.session.key.as_str()))
            })
            .map(|s| self.terms(row, s))
            .collect()
    }
    fn admission(
        &self,
        symbol: &SecurityKey,
        _side: Side,
        _session: &CalendarSession,
        _now: Nanoseconds,
    ) -> QfResult<AdmissionFacts> {
        Ok(AdmissionFacts {
            instrument: Self::instrument(symbol.as_str()),
            submission_reference: None,
            permissions: TradePermissions {
                buy: self.permission,
                sell: self.permission,
                product_buy: self.permission,
                risk_disclosure: Some(true),
                delisting_buy: Some(true),
                risk_bought_or_open: Some(q(0)),
            },
        })
    }
    fn terminal_order(&self, _id: &str) -> QfResult<Order> {
        Err(QfError::new(
            ErrorCode::InvalidOrder,
            "order",
            "未知订单；此隔离事实源没有历史结果",
        ))
    }
}
pub type Manager = OrderManager<Facts>;
pub fn manager(cash: &str, facts: Facts) -> Manager {
    manager_with_limits(cash, facts, OrderLimits::default())
}
pub fn manager_with_limits(cash: &str, facts: Facts, limits: OrderLimits) -> Manager {
    let calendar = rows();
    let account =
        Account::new(d(cash), calendar.clone(), usage(), AccountLimits::default()).unwrap();
    let universe = facts.symbols.iter().map(|s| sec(s)).collect();
    let mut manager = OrderManager::new(account, facts, universe, limits).unwrap();
    manager.settle(&calendar[0].session.key).unwrap();
    manager.session_start(&calendar[0]).unwrap();
    for symbol in ["A", "B"] {
        manager
            .set_raw_mark(
                sec(symbol),
                raw(&calendar[0], "10", calendar[0].before_open_ns),
                calendar[0].before_open_ns,
            )
            .unwrap();
    }
    manager
}
pub fn time(row: &CalendarSession, minute: i64) -> Nanoseconds {
    ns(row.local_midnight_ns.get() + minute * MINUTE_NS)
}
pub fn command(row: &CalendarSession, symbol: &str, sequence: u64, time: Nanoseconds) -> EventKey {
    EventKey {
        time_ns: time,
        phase: EventPhase::Callback,
        security: sec(symbol),
        identity: EventIdentity {
            source_session: row.session.key.clone(),
            channel: qf_core::types::keys::ChannelKey::new("strategy_commands").unwrap(),
            sequence: Some(Sequence::new(sequence)),
            stable_input_sequence: Sequence::new(sequence),
        },
    }
}
pub fn submit(
    manager: &mut Manager,
    symbol: &str,
    value: IntentValue,
    sequence: u64,
    now: Nanoseconds,
    tif: TimeInForce,
) -> OrderResult {
    let row = rows()
        .into_iter()
        .find(|r| r.before_open_ns <= now && now <= r.after_close_ns)
        .unwrap();
    manager
        .validate_and_reserve(&OrderIntent {
            security: sec(symbol),
            side: Side::Buy,
            value,
            style: OrderStyle::Market,
            tif,
            submitted_at: command(&row, symbol, sequence, now),
        })
        .unwrap()
}
pub fn tick(
    row: &CalendarSession,
    symbol: &str,
    sequence: u64,
    now: Nanoseconds,
    price: &str,
) -> MarketEvent {
    MarketEvent::TradeTick(TradeTick {
        security: sec(symbol),
        session: row.session.key.clone(),
        time_ns: now,
        price: p(price),
        quantity: q(1000),
        identity: EventIdentity {
            source_session: row.session.key.clone(),
            channel: qf_core::types::keys::ChannelKey::new("synthetic_trades").unwrap(),
            sequence: Some(Sequence::new(sequence)),
            stable_input_sequence: Sequence::new(sequence),
        },
        units: qf_core::types::market::MarketUnits {
            price_currency: qf_core::types::market::Currency::CNY,
            quantity_unit: qf_core::types::market::QuantityUnit::Shares,
        },
    })
}
pub fn fill_outcome(
    manager: &Manager,
    event: &MarketEvent,
    order: &Order,
    quantity: i64,
    price: &str,
) -> MatchOutcome {
    let mut updated = order.clone();
    updated.filled_quantity = updated.filled_quantity.checked_add(q(quantity)).unwrap();
    updated.status = if updated.filled_quantity == updated.quantity {
        OrderStatus::Filled
    } else {
        OrderStatus::PartiallyFilled
    };
    MatchOutcome {
        fills: vec![Fill {
            trade_id: manager.trade_id(0).unwrap(),
            order_id: order.order_id.clone(),
            security: order.security.clone(),
            side: order.side,
            quantity: q(quantity),
            price: p(price),
            fee: d("0"),
            execution_time_ns: event.key().time_ns,
        }],
        orders: vec![updated],
    }
}
pub fn execute(
    manager: &mut Manager,
    id: &str,
    quantity: i64,
    event: &MarketEvent,
    price: &str,
) -> MatchOutcome {
    let order = manager.get_order(id).unwrap();
    let outcome = fill_outcome(manager, event, &order, quantity, price);
    let claim = manager.claim_match(event, &[order]).unwrap();
    let outcome = manager.commit_match(claim, outcome).unwrap();
    manager.mark(event).unwrap();
    outcome
}
pub fn empty(manager: &mut Manager, event: &MarketEvent) {
    let orders = manager
        .active_orders(&event.key().security, 10_000)
        .unwrap();
    let claim = manager.claim_match(event, &orders).unwrap();
    manager
        .commit_match(
            claim,
            MatchOutcome {
                fills: vec![],
                orders: vec![],
            },
        )
        .unwrap();
    manager.mark(event).unwrap();
}

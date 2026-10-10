#![allow(dead_code)]
use qf_core::accounting::{AccountPort, AccountView};
use qf_core::clock::calendar::MINUTE_NS;
use qf_core::clock::schedule::ScheduledCall;
use qf_core::clock::{
    CalendarSession, Checkpoint, EventSource, Registration, SessionCalendar, TradingPeriod,
    TradingSession, Visibility,
};
use qf_core::data::{DataRequest, DependencyCheck};
use qf_core::engine::*;
use qf_core::matching::{Fill, MatchOutcome, Matcher};
use qf_core::orders::*;
use qf_core::results::*;
use qf_core::rules::market::SessionTemplate;
use qf_core::run::{ExecutionModel, Frequency, RunOutcome};
use qf_core::strategy::{CommandSink, ReadView, StrategyHost};
use qf_core::types::keys::ChannelKey;
use qf_core::types::market::{Currency, MarketUnits, QuantityUnit};
use qf_core::types::*;
use qf_core::{ErrorCode, QfError, QfResult};
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, VecDeque};
use std::rc::Rc;

pub const DAY_NS: i64 = 1440 * MINUTE_NS;
pub fn ns(time: i64) -> Nanoseconds {
    Nanoseconds::new(time)
}
pub fn security(value: &str) -> SecurityKey {
    SecurityKey::new(value).unwrap()
}
pub fn session_key(value: &str) -> SessionKey {
    SessionKey::new(value).unwrap()
}
pub fn quantity(value: i64) -> Quantity {
    Quantity::new(value).unwrap()
}
pub fn failure(code: ErrorCode) -> QfError {
    QfError::new(code, "synthetic_time_fixture", "隔离时序夹具")
}
pub fn local(session: &CalendarSession, minute: i64) -> Nanoseconds {
    ns(session.local_midnight_ns.get() + minute * MINUTE_NS)
}
pub fn row(index: usize) -> CalendarSession {
    // Explicit synthetic exchange calendar: Friday month-end, a weekend gap,
    // then Monday/Tuesday. UTC mappings are fixture data, not a market claim.
    let (date, days, week_id, week_n, week_total, month_id, month_n, month_total) = match index {
        0 => ("2026-01-30", 0, "2026-W05", 5, 5, "2026-01", 22, 22),
        1 => ("2026-02-02", 3, "2026-W06", 1, 5, "2026-02", 1, 20),
        2 => ("2026-02-03", 4, "2026-W06", 2, 5, "2026-02", 2, 20),
        _ => panic!("fixture session"),
    };
    let midnight = days * DAY_NS;
    CalendarSession {
        session: TradingSession {
            key: session_key(&format!("S{}", index + 1)),
            exchange_timezone: "Asia/Shanghai".into(),
            open_ns: ns(midnight + 555 * MINUTE_NS),
            close_ns: ns(midnight + 900 * MINUTE_NS),
        },
        date: date.parse().unwrap(),
        local_midnight_ns: ns(midnight),
        before_open_ns: ns(midnight + 550 * MINUTE_NS),
        after_close_ns: ns(midnight + 901 * MINUTE_NS),
        market_windows: vec![],
        bar_windows: vec![],
        week: TradingPeriod {
            id: week_id.into(),
            trading_day: week_n,
            total_trading_days: week_total,
        },
        month: TradingPeriod {
            id: month_id.into(),
            trading_day: month_n,
            total_trading_days: month_total,
        },
    }
    .with_template(SessionTemplate::ChinaAuction)
    .unwrap()
}
pub fn calendar(count: usize) -> SessionCalendar {
    SessionCalendar::new(
        "synthetic_clock_only".into(),
        (0..count).map(row).collect(),
        Some(session_key("NEXT")),
    )
    .unwrap()
}
pub fn identity(session: &CalendarSession, channel: &str, sequence: u64) -> EventIdentity {
    EventIdentity {
        source_session: session.session.key.clone(),
        channel: ChannelKey::new(channel).unwrap(),
        sequence: Some(Sequence::new(sequence)),
        stable_input_sequence: Sequence::new(sequence),
    }
}
pub fn bar(
    session: &CalendarSession,
    value: &str,
    start: Nanoseconds,
    end: Nanoseconds,
    sequence: u64,
) -> MarketEvent {
    MarketEvent::Bar(Bar {
        security: security(value),
        session: session.session.key.clone(),
        identity: identity(session, "bar", sequence),
        interval_start_ns: start,
        interval_end_ns: end,
        open: Price::new("10".parse().unwrap()).unwrap(),
        high: Price::new("12".parse().unwrap()).unwrap(),
        low: Price::new("9".parse().unwrap()).unwrap(),
        close: Price::new("11".parse().unwrap()).unwrap(),
        quantity: Some(quantity(100)),
        units: MarketUnits {
            price_currency: Currency::CNY,
            quantity_unit: QuantityUnit::Shares,
        },
    })
}
pub fn daily(session: &CalendarSession, value: &str) -> MarketEvent {
    bar(
        session,
        value,
        local(session, 570),
        session.session.close_ns,
        1,
    )
}
pub fn tick(
    session: &CalendarSession,
    value: &str,
    channel: &str,
    time: Nanoseconds,
    sequence: u64,
) -> MarketEvent {
    MarketEvent::TradeTick(TradeTick {
        security: security(value),
        session: session.session.key.clone(),
        identity: identity(session, channel, sequence),
        time_ns: time,
        price: Price::new("10".parse().unwrap()).unwrap(),
        quantity: quantity(100),
        units: MarketUnits {
            price_currency: Currency::CNY,
            quantity_unit: QuantityUnit::Shares,
        },
    })
}
pub fn quote(
    session: &CalendarSession,
    value: &str,
    time: Nanoseconds,
    sequence: u64,
) -> MarketEvent {
    MarketEvent::QuoteTick(QuoteTick {
        security: security(value),
        session: session.session.key.clone(),
        identity: identity(session, "quote", sequence),
        time_ns: time,
        bid: Some(Price::new("9".parse().unwrap()).unwrap()),
        ask: Some(Price::new("10".parse().unwrap()).unwrap()),
        bid_quantity: Some(quantity(100)),
        ask_quantity: Some(quantity(100)),
        units: MarketUnits {
            price_currency: Currency::CNY,
            quantity_unit: QuantityUnit::Shares,
        },
    })
}
pub fn intent(value: &str, tif: TimeInForce) -> OrderIntent {
    OrderIntent {
        security: security(value),
        side: Side::Buy,
        value: IntentValue::Quantity(quantity(1)),
        style: OrderStyle::Market,
        tif,
        // Deliberately forged timestamp; the clock MUST replace it.
        submitted_at: tick(&row(0), value, "forged", ns(i64::MIN), 0).key(),
    }
}
pub fn config(frequency: Frequency) -> EngineConfig {
    EngineConfig {
        frequency,
        execution_model: if frequency == Frequency::Tick {
            ExecutionModel::TradeTickV1
        } else {
            ExecutionModel::BarNextIntervalV1
        },
        universe: vec![security("A"), security("B")],
        result_id: "synthetic_time_result".into(),
        limits: EngineLimits::default(),
    }
}
pub struct Source {
    pub events: VecDeque<MarketEvent>,
    pub chunk: usize,
    pub reads: Rc<Cell<usize>>,
    pub fail_after: Option<usize>,
}
impl Source {
    pub fn new(mut events: Vec<MarketEvent>, chunk: usize) -> Self {
        events.sort_by_key(MarketEvent::key);
        Self {
            events: events.into(),
            chunk,
            reads: Rc::new(Cell::new(0)),
            fail_after: None,
        }
    }
}
impl EventSource for Source {
    fn next_chunk(&mut self, max_events: usize) -> QfResult<Option<Vec<MarketEvent>>> {
        self.reads.set(self.reads.get() + 1);
        if self.fail_after.is_some_and(|n| self.reads.get() > n) {
            return Err(failure(ErrorCode::DataChanged));
        }
        if self.events.is_empty() {
            return Ok(None);
        }
        let size = self.chunk.min(max_events).min(self.events.len());
        Ok(Some(
            (0..size)
                .map(|_| self.events.pop_front().unwrap())
                .collect(),
        ))
    }
}
#[derive(Default)]
pub struct Control {
    pub cancelled: Rc<Cell<bool>>,
    pub checks: usize,
    pub cancel_at_check: Option<usize>,
    pub progress: Vec<Progress>,
    pub cancel_after_events: Option<u64>,
}
impl Checkpoint for Control {
    fn check(&mut self) -> QfResult<()> {
        self.checks += 1;
        if self.cancelled.get() || self.cancel_at_check.is_some_and(|n| self.checks >= n) {
            Err(failure(ErrorCode::Cancelled))
        } else {
            Ok(())
        }
    }
}
impl RunControl for Control {
    fn progress(&mut self, progress: Progress) -> QfResult<()> {
        self.progress.push(progress);
        if self
            .cancel_after_events
            .is_some_and(|n| progress.completed_events >= n)
        {
            self.cancelled.set(true);
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Default)]
pub struct Frame {
    pub keys: Vec<EventKey>,
    pub knowledge: Option<Nanoseconds>,
}
impl VisibleFrame for Frame {
    fn latest_knowledge_ns(&self) -> Option<Nanoseconds> {
        self.knowledge
    }
    fn latest_market_key(&self) -> Option<&EventKey> {
        self.keys.last()
    }
}
#[derive(Default)]
pub struct Data {
    pub future: Vec<EventKey>,
    pub published: Vec<EventKey>,
    pub leaked: Option<EventKey>,
    pub reads: usize,
    pub checks: usize,
    pub change_at: Option<usize>,
    pub closed: usize,
}
impl StrategyDataPort for Data {
    type Frame = Frame;
    fn read(&mut self, request: &DataRequest, visibility: &Visibility) -> QfResult<Frame> {
        self.reads += 1;
        if let Some(key) = &self.leaked {
            return Ok(Frame {
                keys: vec![key.clone()],
                knowledge: Some(key.time_ns),
            });
        }
        Ok(Frame {
            keys: self
                .future
                .iter()
                .filter(|key| key.time_ns <= request.end_ns && visibility.contains_market(key))
                .cloned()
                .collect(),
            knowledge: None,
        })
    }
    fn publish(&mut self, event: &MarketEvent) -> QfResult<()> {
        self.published.push(event.key());
        Ok(())
    }
    fn check(&mut self) -> QfResult<DependencyCheck> {
        self.checks += 1;
        Ok(if self.change_at.is_some_and(|n| self.checks >= n) {
            DependencyCheck::DataChanged
        } else {
            DependencyCheck::Unchanged
        })
    }
    fn close(&mut self) -> QfResult<()> {
        self.closed += 1;
        Ok(())
    }
}
/// Timing-only order/reservation fixture, explicitly NOT D06/D09 accounting.
#[derive(Default)]
pub struct Execution {
    pub orders: BTreeMap<String, Order>,
    pub fills: Vec<Fill>,
    pub normalized: Vec<OrderIntent>,
    pub trace: Rc<RefCell<Vec<String>>>,
}
impl Execution {
    fn result(id: &str) -> OrderResult {
        OrderResult {
            accepted: true,
            order_id: Some(id.into()),
            reason_code: None,
            message: "synthetic_clock_only".into(),
            unchanged: false,
            requested_quantity: Some(quantity(1)),
            effective_quantity: Some(quantity(1)),
        }
    }
}
impl AccountPort for Execution {
    fn validate_and_reserve(&mut self, _intent: &OrderIntent) -> QfResult<OrderResult> {
        Err(failure(ErrorCode::CapabilityUnavailable))
    }
    fn apply_fill(&mut self, fill: &Fill) -> QfResult<()> {
        self.trace
            .borrow_mut()
            .push(format!("fill:{}", fill.security.as_str()));
        self.fills.push(fill.clone());
        Ok(())
    }
    fn release(&mut self, _id: &str) -> QfResult<()> {
        Ok(())
    }
    fn settle(&mut self, session: &SessionKey) -> QfResult<()> {
        self.trace
            .borrow_mut()
            .push(format!("settle:{}", session.as_str()));
        Ok(())
    }
    fn check_session_end(&self, _session: &SessionKey) -> QfResult<()> {
        // Explicit clock-only fixture acknowledgement, not D09 readiness.
        // Real adapters forward the actual account's postcondition.
        Ok(())
    }
    fn value(&self, _now: Nanoseconds) -> QfResult<AccountView> {
        let reserved = ExactDecimal::from_integer(self.active_order_count() as i64);
        Ok(AccountView {
            cash: "1000".parse().unwrap(),
            available_cash: ExactDecimal::from_integer(1000).checked_sub(reserved)?,
            frozen_cash: reserved,
            receivables: ExactDecimal::ZERO,
            total_value: None,
            positions: BTreeMap::new(),
        })
    }
}
impl ExecutionPort for Execution {
    fn submit_order(
        &mut self,
        intent: &OrderIntent,
        activation: &OrderActivation,
    ) -> QfResult<OrderResult> {
        self.normalized.push(intent.clone());
        let id = format!("o{}", self.orders.len() + 1);
        self.orders.insert(
            id.clone(),
            Order {
                updated_at: None,
                order_id: id.clone(),
                security: intent.security.clone(),
                side: intent.side,
                quantity: quantity(1),
                filled_quantity: quantity(0),
                status: OrderStatus::Open,
                submitted_ns: activation.submitted_ns,
                limit_price: None,
                tif: intent.tif,
                effective_session: activation.effective_session.clone(),
                eligible_interval_start: Some(activation.eligible_interval_start),
                eligible_after_event: activation.eligible_after_event.clone(),
                reason_code: None,
                message: "synthetic_clock_only".into(),
            },
        );
        Ok(Self::result(&id))
    }
    fn cancel_order(&mut self, id: &str) -> QfResult<OrderResult> {
        let order = self
            .orders
            .get_mut(id)
            .ok_or_else(|| failure(ErrorCode::InvalidOrder))?;
        order.status = OrderStatus::Cancelled;
        Ok(Self::result(id))
    }
    fn order(&self, id: &str) -> QfResult<Order> {
        self.orders
            .get(id)
            .cloned()
            .ok_or_else(|| failure(ErrorCode::InvalidOrder))
    }
    fn active_orders(&self, security: &SecurityKey, _limit: usize) -> QfResult<Vec<Order>> {
        Ok(self
            .orders
            .values()
            .filter(|o| {
                o.security == *security
                    && matches!(
                        o.status,
                        OrderStatus::Open | OrderStatus::PartiallyFilled | OrderStatus::Accepted
                    )
            })
            .cloned()
            .collect())
    }
    fn active_order_count(&self) -> usize {
        self.orders
            .values()
            .filter(|o| {
                matches!(
                    o.status,
                    OrderStatus::Open | OrderStatus::PartiallyFilled | OrderStatus::Accepted
                )
            })
            .count()
    }
    fn transition(&mut self, order: &Order) -> QfResult<()> {
        self.orders.insert(order.order_id.clone(), order.clone());
        Ok(())
    }
    fn mark(&mut self, event: &MarketEvent) -> QfResult<()> {
        self.trace
            .borrow_mut()
            .push(format!("mark:{}", event.key().security.as_str()));
        Ok(())
    }
    fn expire_day(&mut self, session: &SessionKey) -> QfResult<Vec<Order>> {
        self.trace
            .borrow_mut()
            .push(format!("expire:{}", session.as_str()));
        let mut expired = vec![];
        for order in self.orders.values_mut() {
            if order.tif == TimeInForce::Day
                && order.effective_session == *session
                && matches!(
                    order.status,
                    OrderStatus::Open | OrderStatus::PartiallyFilled | OrderStatus::Accepted
                )
            {
                order.status = OrderStatus::Expired;
                expired.push(order.clone());
            }
        }
        Ok(expired)
    }
    fn session_start(&mut self, _session: &CalendarSession) -> QfResult<Vec<Order>> {
        Ok(vec![])
    }
}
#[derive(Default)]
pub struct Matching {
    pub suppress: bool,
    pub seen: Vec<(EventKey, Vec<String>)>,
}
impl Matcher for Matching {
    type Rules = &'static str;
    fn consume(
        &mut self,
        event: &MarketEvent,
        orders: &[Order],
        rules: &Self::Rules,
    ) -> QfResult<MatchOutcome> {
        assert_eq!(*rules, "synthetic_clock_only");
        self.seen.push((
            event.key(),
            orders.iter().map(|o| o.order_id.clone()).collect(),
        ));
        if self.suppress {
            return Ok(MatchOutcome {
                fills: vec![],
                orders: vec![],
            });
        }
        let price = match event {
            MarketEvent::Bar(b) => b.open,
            MarketEvent::TradeTick(t) => t.price,
            MarketEvent::QuoteTick(q) => q.ask.unwrap(),
        };
        let fills = orders
            .iter()
            .map(|o| Fill {
                trade_id: format!("t:{}:{}", self.seen.len(), o.order_id),
                order_id: o.order_id.clone(),
                security: o.security.clone(),
                side: o.side,
                quantity: quantity(o.quantity.get() - o.filled_quantity.get()),
                price,
                fee: ExactDecimal::ZERO,
                execution_time_ns: event.key().time_ns,
            })
            .collect();
        let orders = orders
            .iter()
            .cloned()
            .map(|mut order| {
                order.status = OrderStatus::Filled;
                order.filled_quantity = order.quantity;
                order
            })
            .collect();
        Ok(MatchOutcome { fills, orders })
    }
}
#[derive(Default)]
pub struct Rules;
impl RulesPort<&'static str> for Rules {
    fn for_event(
        &mut self,
        _event: &MarketEvent,
        _session: &CalendarSession,
    ) -> QfResult<&'static str> {
        Ok("synthetic_clock_only")
    }
}

pub type View<'a> = dyn ReadView<Frame = Frame> + 'a;
pub type Hook = Box<dyn FnMut(&mut View<'_>, &mut dyn CommandSink) -> QfResult<()>>;
pub type InitHook =
    Box<dyn FnMut(&mut View<'_>, &mut dyn CommandSink, &mut Registration) -> QfResult<()>>;
pub type BarHook =
    Box<dyn FnMut(&BarBoundary, &mut View<'_>, &mut dyn CommandSink) -> QfResult<()>>;
pub type TickHook =
    Box<dyn FnMut(&MarketEvent, &mut View<'_>, &mut dyn CommandSink) -> QfResult<()>>;
pub type NoticeHook =
    Box<dyn FnMut(&Notification, &mut View<'_>, &mut dyn CommandSink) -> QfResult<()>>;
pub struct Host {
    pub handlers: Handlers,
    pub initialize: Option<InitHook>,
    pub bars: Option<BarHook>,
    pub ticks: Option<TickHook>,
    pub notice: Option<NoticeHook>,
    pub after: Option<Hook>,
    pub scheduled_hook: Option<Hook>,
    pub calls: Vec<String>,
    pub bar_boundaries: Vec<BarBoundary>,
    pub tick_keys: Vec<EventKey>,
    pub notices: Vec<Notification>,
    pub scheduled_calls: Vec<ScheduledCall>,
    pub finish_count: usize,
    pub finish_error: Option<ErrorCode>,
    pub trace: Rc<RefCell<Vec<String>>>,
}
impl Default for Host {
    fn default() -> Self {
        Self {
            handlers: Handlers {
                bars: true,
                ticks: true,
                before_open: true,
                after_close: true,
                orders: true,
                trades: true,
            },
            initialize: None,
            bars: None,
            ticks: None,
            notice: None,
            after: None,
            scheduled_hook: None,
            calls: vec![],
            bar_boundaries: vec![],
            tick_keys: vec![],
            notices: vec![],
            scheduled_calls: vec![],
            finish_count: 0,
            finish_error: None,
            trace: Rc::new(RefCell::new(vec![])),
        }
    }
}
impl StrategyHost for Host {
    type Frame = Frame;
    fn initialize(
        &mut self,
        _view: &mut View<'_>,
        _commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.calls.push("initialize".into());
        Ok(())
    }
    fn callback(
        &mut self,
        event: &MarketEvent,
        view: &mut View<'_>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.calls.push("tick".into());
        self.tick_keys.push(event.key());
        if let Some(hook) = &mut self.ticks {
            hook(event, view, commands)?;
        }
        Ok(())
    }
    fn finish(&mut self) -> QfResult<()> {
        self.finish_count += 1;
        self.finish_error.map_or(Ok(()), |code| Err(failure(code)))
    }
}
impl EngineStrategy for Host {
    fn handlers(&self) -> Handlers {
        self.handlers
    }
    fn initialize_engine(
        &mut self,
        view: &mut View<'_>,
        commands: &mut dyn CommandSink,
        registration: &mut Registration,
    ) -> QfResult<()> {
        self.initialize(view, commands)?;
        if let Some(hook) = &mut self.initialize {
            hook(view, commands, registration)?;
        }
        Ok(())
    }
    fn before_open(
        &mut self,
        view: &mut View<'_>,
        _commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.calls.push("before_open".into());
        self.trace
            .borrow_mut()
            .push(format!("before:{}", view.now_ns()));
        Ok(())
    }
    fn after_close(&mut self, view: &mut View<'_>, commands: &mut dyn CommandSink) -> QfResult<()> {
        self.calls.push("after_close".into());
        self.trace
            .borrow_mut()
            .push(format!("after:{}", view.now_ns()));
        if let Some(hook) = &mut self.after {
            hook(view, commands)?;
        }
        Ok(())
    }
    fn handle_bars(
        &mut self,
        boundary: &BarBoundary,
        view: &mut View<'_>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.calls.push("bar".into());
        self.bar_boundaries.push(boundary.clone());
        self.trace.borrow_mut().push("handle_data".into());
        if let Some(hook) = &mut self.bars {
            hook(boundary, view, commands)?;
        }
        Ok(())
    }
    fn notification(
        &mut self,
        event: &Notification,
        view: &mut View<'_>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.calls.push("notification".into());
        self.notices.push(event.clone());
        if let Some(hook) = &mut self.notice {
            hook(event, view, commands)?;
        }
        Ok(())
    }
    fn scheduled(
        &mut self,
        call: &ScheduledCall,
        view: &mut View<'_>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.scheduled_calls.push(call.clone());
        if let Some(hook) = &mut self.scheduled_hook {
            hook(view, commands)?;
        }
        Ok(())
    }
}
pub struct Writer {
    pub records: Vec<ResultRecord>,
    pub first_sequences: Vec<u64>,
    pub credit_records: usize,
    pub credit_bytes: usize,
    pub finalized: usize,
    pub aborted: usize,
    pub fail_write: bool,
    pub fail_after_write: bool,
    pub final_cancel: bool,
}
impl Default for Writer {
    fn default() -> Self {
        Self {
            records: vec![],
            first_sequences: vec![],
            credit_records: 2,
            credit_bytes: qf_core::run::MAX_CONTROL_BYTES,
            finalized: 0,
            aborted: 0,
            fail_write: false,
            fail_after_write: false,
            final_cancel: false,
        }
    }
}
impl ResultSink for Writer {
    fn credit(&mut self) -> QfResult<SinkCredit> {
        Ok(SinkCredit {
            max_records: self.credit_records,
            max_bytes: self.credit_bytes,
        })
    }
    fn write(&mut self, batch: &ResultBatch) -> QfResult<()> {
        if self.fail_write {
            return Err(failure(ErrorCode::ResultBudgetExceeded));
        }
        assert_eq!(batch.first_sequence().get(), self.records.len() as u64 + 1);
        self.first_sequences.push(batch.first_sequence().get());
        self.records.extend(batch.records().iter().cloned());
        if self.fail_after_write {
            return Err(failure(ErrorCode::Cancelled));
        }
        Ok(())
    }
    fn finalize(&mut self, _outcome: &RunOutcome) -> QfResult<()> {
        self.finalized += 1;
        if self.final_cancel {
            Err(failure(ErrorCode::Cancelled))
        } else {
            Ok(())
        }
    }
    fn abort(&mut self, _error: &QfError) -> QfResult<()> {
        self.aborted += 1;
        Ok(())
    }
}
#[derive(Default)]
pub struct Harness {
    pub data: Data,
    pub execution: Execution,
    pub matcher: Matching,
    pub rules: Rules,
    pub host: Host,
    pub control: Control,
    pub writer: Writer,
}
impl Harness {
    pub fn run(
        &mut self,
        config: EngineConfig,
        calendar: SessionCalendar,
        sources: Vec<Source>,
    ) -> EngineReport {
        self.execution.trace = self.host.trace.clone();
        Engine::new(config, calendar, sources)
            .unwrap()
            .run(EnginePorts {
                data: &mut self.data,
                execution: &mut self.execution,
                matcher: &mut self.matcher,
                rules: &mut self.rules,
                strategy: &mut self.host,
                control: &mut self.control,
                results: &mut self.writer,
            })
    }
}
pub fn code(report: &EngineReport) -> Option<ErrorCode> {
    match &report.outcome {
        RunOutcome::Failed { error, .. } => Some(error.code),
        RunOutcome::Cancelled { .. } => Some(ErrorCode::Cancelled),
        _ => None,
    }
}
pub fn request(end: Nanoseconds, frequency: Frequency) -> DataRequest {
    DataRequest {
        securities: vec![security("A")],
        fields: vec!["price".into()],
        frequency,
        start_ns: None,
        end_ns: end,
        count_per_security: Some(100),
        adjustment: qf_core::data::Adjustment::None,
    }
}

//! D03 single-run driver. Concrete data/strategy/order/account/matching/result
//! implementations belong to D04-D13; this is not a deployed complete engine.
use crate::accounting::{AccountPort, AccountView};
use crate::clock::schedule::{ScheduledCall, SubscriptionKind};
use crate::clock::{
    CalendarSession, Checkpoint, EventSource, MergeLimits, Registration, ScheduleTime,
    SessionCalendar, SimClock, Visibility,
};
use crate::data::{DataRequest, DependencyCheck};
use crate::matching::{Fill, Matcher};
use crate::orders::{Order, OrderIntent, OrderResult, OrderStatus, TimeInForce};
use crate::results::{ResultBatch, ResultRecord, ResultSink};
use crate::run::{ExecutionModel, Frequency, RunConfig, RunOutcome};
use crate::strategy::{CommandSink, ReadView, StrategyHost};
use crate::types::keys::ChannelKey;
use crate::types::{
    EventIdentity, EventKey, EventPhase, MarketEvent, Nanoseconds, SecurityKey, Sequence,
    SessionKey,
};
use crate::{ErrorCode, QfError, QfResult};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, VecDeque};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderActivation {
    pub submitted_ns: Nanoseconds,
    pub effective_session: SessionKey,
    pub eligible_interval_start: Nanoseconds,
    /// Market cursor, not a Callback-phase key. Later same-ns ticks may match.
    pub eligible_after_event: Option<EventKey>,
}
/// D06 supplies order storage and atomic command processing with D09's account.
/// No matcher can see ineligible orders: the driver filters before consume().
pub trait ExecutionPort: AccountPort {
    fn submit_order(
        &mut self,
        intent: &OrderIntent,
        activation: &OrderActivation,
    ) -> QfResult<OrderResult>;
    fn cancel_order(&mut self, order_id: &str) -> QfResult<OrderResult>;
    fn order(&self, order_id: &str) -> QfResult<Order>;
    /// Return only this security, in D06's stable order priority, bounded by
    /// limit. Do not scan/clone the entire portfolio for every market event.
    fn active_orders(&self, security: &SecurityKey, limit: usize) -> QfResult<Vec<Order>>;
    fn active_order_count(&self) -> usize;
    fn transition(&mut self, order: &Order) -> QfResult<()>;
    /// D09 updates valuation inputs using this completed event, after fills and
    /// before any strategy can observe the boundary's account.
    fn mark(&mut self, event: &MarketEvent) -> QfResult<()>;
    fn expire_day(&mut self, session: &SessionKey) -> QfResult<Vec<Order>>;
    /// D06/D09 recheck GTC, apply due corporate actions/rules, release cancelled
    /// orders and return notifications. Called after AccountPort::settle.
    fn session_start(&mut self, session: &CalendarSession) -> QfResult<Vec<Order>>;
    /// D09 registers close-date entitlements/payments after the last market
    /// event, including halted securities with no event. D06 stages any order
    /// notifications atomically with the account. Default keeps D03 adapters.
    fn session_end(&mut self, _session: &CalendarSession) -> QfResult<Vec<Order>> {
        Ok(Vec::new())
    }
}
pub trait RulesPort<R> {
    /// D02 facts/catalogue are consumed by the concrete matcher, never replaced
    /// by clock defaults. An owned R may itself be an Arc/borrowed rule bundle.
    fn for_event(&mut self, event: &MarketEvent, session: &CalendarSession) -> QfResult<R>;
}
/// D05/D10 frames report their actual latest knowledge time/market key; checked
/// here as well as by the provider's per-row clipping. None denotes an empty
/// market frame (research-only frames still report latest_knowledge_ns).
pub trait VisibleFrame {
    fn latest_knowledge_ns(&self) -> Option<Nanoseconds>;
    fn latest_market_key(&self) -> Option<&EventKey>;
}
pub trait StrategyDataPort {
    type Frame: VisibleFrame;
    fn read(&mut self, request: &DataRequest, visibility: &Visibility) -> QfResult<Self::Frame>;
    /// Publish after account updates; prefetched chunks are not public data.
    fn publish(&mut self, event: &MarketEvent) -> QfResult<()>;
    fn check(&mut self) -> QfResult<DependencyCheck>;
    fn close(&mut self) -> QfResult<()>;
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BarBoundary {
    pub time_ns: Nanoseconds,
    pub securities: Vec<SecurityKey>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Notification {
    Order(Order),
    Trade(Fill),
}
#[derive(Debug, Clone, Copy, Default)]
pub struct Handlers {
    pub bars: bool,
    pub ticks: bool,
    pub before_open: bool,
    pub after_close: bool,
    pub orders: bool,
    pub trades: bool,
}
/// Extension of the D01 host for scheduler/lifecycle/aggregate-Bar dispatch.
/// D10 can override initialize_engine to expose registration inside the same
/// Python initialize call; the D01 initializer is still the default path.
pub trait EngineStrategy: StrategyHost {
    fn handlers(&self) -> Handlers;
    fn initialize_engine(
        &mut self,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
        _registration: &mut Registration,
    ) -> QfResult<()> {
        self.initialize(view, commands)
    }
    fn before_open(
        &mut self,
        _view: &mut dyn ReadView<Frame = Self::Frame>,
        _commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        Ok(())
    }
    fn after_close(
        &mut self,
        _view: &mut dyn ReadView<Frame = Self::Frame>,
        _commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        Ok(())
    }
    fn handle_bars(
        &mut self,
        _boundary: &BarBoundary,
        _view: &mut dyn ReadView<Frame = Self::Frame>,
        _commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        Err(error(
            ErrorCode::CapabilityUnavailable,
            "handle_data",
            "聚合Bar回调尚未接入",
        ))
    }
    fn scheduled(
        &mut self,
        _call: &ScheduledCall,
        _view: &mut dyn ReadView<Frame = Self::Frame>,
        _commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        Err(error(
            ErrorCode::CapabilityUnavailable,
            "scheduled",
            "定时回调尚未接入",
        ))
    }
    fn notification(
        &mut self,
        _event: &Notification,
        _view: &mut dyn ReadView<Frame = Self::Frame>,
        _commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Progress {
    pub completed_events: u64,
    pub completed_sessions: u64,
    pub now_ns: Nanoseconds,
}
pub trait RunControl: Checkpoint {
    /// An interruptible bounded send in D12; the engine keeps no progress list.
    fn progress(&mut self, progress: Progress) -> QfResult<()>;
}
#[derive(Debug, Clone, Copy)]
pub struct EngineLimits {
    pub merge: MergeLimits,
    pub max_events: u64,
    pub max_bar_events_per_boundary: usize,
    pub max_active_orders: usize,
    pub max_commands_per_boundary: usize,
    pub max_notifications_per_boundary: usize,
    pub max_pending_result_records: usize,
    pub max_pending_result_bytes: usize,
    pub progress_every_events: u64,
}
impl Default for EngineLimits {
    fn default() -> Self {
        Self {
            merge: MergeLimits::default(),
            max_events: 100_000_000,
            max_bar_events_per_boundary: 10_000,
            max_active_orders: 10_000,
            max_commands_per_boundary: 10_000,
            max_notifications_per_boundary: 10_000,
            max_pending_result_records: 10_000,
            max_pending_result_bytes: crate::run::MAX_CONTROL_BYTES,
            progress_every_events: 10_000,
        }
    }
}
impl EngineLimits {
    fn validate(&self) -> QfResult<()> {
        if self.max_events == 0
            || self.progress_every_events == 0
            || [
                self.max_bar_events_per_boundary,
                self.max_active_orders,
                self.max_commands_per_boundary,
                self.max_notifications_per_boundary,
                self.max_pending_result_records,
            ]
            .iter()
            .any(|v| *v == 0 || *v > 10_000)
            || self.max_pending_result_bytes == 0
            || self.max_pending_result_bytes > crate::data::MAX_ARROW_BATCH_BYTES
        {
            return Err(error(
                ErrorCode::ResourceLimit,
                "engine_config",
                "引擎事件、命令或结果预算无效",
            ));
        }
        Ok(())
    }
}
#[derive(Debug, Clone)]
pub struct EngineConfig {
    pub frequency: Frequency,
    pub execution_model: ExecutionModel,
    pub universe: Vec<SecurityKey>,
    pub result_id: String,
    pub limits: EngineLimits,
}
pub struct Engine<S> {
    config: EngineConfig,
    calendar: SessionCalendar,
    merge: crate::clock::StreamingMerge<S>,
}
pub struct EnginePorts<'a, D, E, M, R, H, C, W> {
    pub data: &'a mut D,
    pub execution: &'a mut E,
    pub matcher: &'a mut M,
    pub rules: &'a mut R,
    pub strategy: &'a mut H,
    pub control: &'a mut C,
    pub results: &'a mut W,
}
#[derive(Debug)]
pub struct EngineReport {
    pub outcome: RunOutcome,
    pub progress: Progress,
    pub merge: crate::clock::merge::MergeStats,
    /// At most finish/abort/close errors; never replaces an authoritative terminal.
    pub cleanup_errors: Vec<QfError>,
}
fn error(code: ErrorCode, operation: &str, message: &str) -> QfError {
    QfError::new(code, operation, message)
}

struct BoundaryBudget {
    commands: usize,
    notifications: usize,
    fault: Option<QfError>,
}
impl BoundaryBudget {
    fn new() -> Self {
        Self {
            commands: 0,
            notifications: 0,
            fault: None,
        }
    }
    fn fail(&mut self, failure: QfError) -> QfError {
        self.fault.get_or_insert_with(|| failure.clone());
        failure
    }
    fn check(&self) -> QfResult<()> {
        self.fault.clone().map_or(Ok(()), Err)
    }
}
struct Output {
    pending: VecDeque<ResultRecord>,
    bytes: usize,
    next_sequence: u64,
    write_attempted: bool,
}
/// Count encoded bytes without allocating an extra, possibly huge message.
fn encoded_len(value: &impl serde::Serialize, maximum: usize) -> QfResult<usize> {
    struct Counter {
        bytes: usize,
        maximum: usize,
    }
    impl std::io::Write for Counter {
        fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
            if buffer.len() > self.maximum.saturating_sub(self.bytes) {
                return Err(std::io::Error::other("result budget"));
            }
            self.bytes += buffer.len();
            Ok(buffer.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut counter = Counter { bytes: 0, maximum };
    serde_json::to_writer(&mut counter, value).map_err(|_| {
        error(
            ErrorCode::ResultBudgetExceeded,
            "result_sink",
            "结果记录超过编码预算",
        )
    })?;
    Ok(counter.bytes)
}
impl Output {
    fn new() -> Self {
        Self {
            pending: VecDeque::new(),
            bytes: 0,
            next_sequence: 1,
            write_attempted: false,
        }
    }
    fn push(&mut self, record: ResultRecord, limits: &EngineLimits) -> QfResult<()> {
        let bytes = encoded_len(
            &record,
            limits.max_pending_result_bytes.saturating_sub(self.bytes),
        )?;
        if self.pending.len() >= limits.max_pending_result_records
            || bytes > limits.max_pending_result_bytes.saturating_sub(self.bytes)
        {
            return Err(error(
                ErrorCode::ResultBudgetExceeded,
                "result_sink",
                "回调内待发结果超过预算",
            ));
        }
        self.bytes += bytes;
        self.pending.push_back(record);
        Ok(())
    }
    fn flush<W: ResultSink, C: RunControl>(
        &mut self,
        writer: &mut W,
        control: &mut C,
    ) -> QfResult<()> {
        while !self.pending.is_empty() {
            control.check()?;
            let credit = writer.credit()?;
            control.check()?;
            if credit.max_records == 0
                || credit.max_bytes == 0
                || credit.max_bytes > crate::run::MAX_CONTROL_BYTES
            {
                return Err(error(
                    ErrorCode::ResultBudgetExceeded,
                    "result_sink",
                    "结果接收信用无效",
                ));
            }
            // Include the exact shared batch framing; a credit equal to the
            // encoded single-record batch must be usable, even at tight limits.
            let mut bytes = encoded_len(
                &serde_json::json!({
                    "first_sequence": Sequence::new(self.next_sequence),
                    "records": [],
                }),
                credit.max_bytes,
            )?;
            let mut records = Vec::new();
            while records.len() < credit.max_records.min(crate::results::MAX_PAGE_ITEMS) {
                let Some(record) = self.pending.front() else {
                    break;
                };
                let size = encoded_len(record, crate::run::MAX_CONTROL_BYTES)?;
                let comma = usize::from(!records.is_empty());
                if size + comma > credit.max_bytes.saturating_sub(bytes) {
                    break;
                }
                bytes += size + comma;
                self.bytes -= size;
                records.push(self.pending.pop_front().expect("pending record"));
            }
            if records.is_empty() {
                return Err(error(
                    ErrorCode::ResultBudgetExceeded,
                    "result_sink",
                    "单条必要结果超出接收信用",
                ));
            }
            let count = records.len() as u64;
            let batch = ResultBatch::new(Sequence::new(self.next_sequence), records, &credit)?;
            control.check()?;
            // An interrupted acknowledgement may follow a persisted write.
            // Keep the terminal report conservative even on the first batch.
            self.write_attempted = true;
            writer.write(&batch)?;
            self.next_sequence = self
                .next_sequence
                .checked_add(count)
                .ok_or_else(|| error(ErrorCode::ResourceLimit, "result_sink", "结果序号溢出"))?;
            control.check()?;
        }
        Ok(())
    }
}
fn enqueue(
    notification: Notification,
    pending: &mut VecDeque<Notification>,
    output: &mut Output,
    budget: &mut BoundaryBudget,
    limits: &EngineLimits,
) -> QfResult<()> {
    if budget.notifications >= limits.max_notifications_per_boundary {
        return Err(budget.fail(error(
            ErrorCode::ResourceLimit,
            "notification",
            "边界内通知链超过预算",
        )));
    }
    budget.notifications += 1;
    let record = match &notification {
        Notification::Order(order) => ResultRecord::Order(order.clone()),
        Notification::Trade(fill) => ResultRecord::Trade(fill.clone()),
    };
    output.push(record, limits).map_err(|e| budget.fail(e))?;
    pending.push_back(notification);
    Ok(())
}

struct ScopedView<'a, D, E> {
    data: &'a mut D,
    execution: &'a RefCell<&'a mut E>,
    visibility: &'a Visibility,
    universe: &'a [SecurityKey],
}
impl<D: StrategyDataPort, E: ExecutionPort> ReadView for ScopedView<'_, D, E> {
    type Frame = D::Frame;
    fn now_ns(&self) -> Nanoseconds {
        self.visibility.now_ns
    }
    fn account(&self) -> QfResult<AccountView> {
        self.execution.borrow().value(self.visibility.now_ns)
    }
    fn read(&mut self, request: &DataRequest) -> QfResult<Self::Frame> {
        request.validate(self.visibility.now_ns)?;
        if request
            .securities
            .iter()
            .any(|s| !self.universe.contains(s))
        {
            return Err(error(
                ErrorCode::DataRestricted,
                "strategy_read",
                "查询标的不在本run授权集合内",
            ));
        }
        let frame = self.data.read(request, self.visibility)?;
        if frame
            .latest_knowledge_ns()
            .is_some_and(|time| time > self.visibility.now_ns)
            || frame
                .latest_market_key()
                .is_some_and(|key| !self.visibility.contains_market(key))
        {
            return Err(error(
                ErrorCode::LookaheadForbidden,
                "strategy_read",
                "数据帧越过已发布事件或知识时间边界",
            ));
        }
        if frame
            .latest_market_key()
            .is_some_and(|key| key.time_ns > request.end_ns)
        {
            return Err(error(
                ErrorCode::InvalidContract,
                "strategy_read",
                "数据帧越过请求行情终点",
            ));
        }
        Ok(frame)
    }
}
struct ScopedCommands<'a, E, C> {
    execution: &'a RefCell<&'a mut E>,
    control: &'a mut C,
    visibility: &'a Visibility,
    calendar: &'a SessionCalendar,
    session_index: usize,
    closing: bool,
    universe: &'a [SecurityKey],
    limits: &'a EngineLimits,
    budget: &'a mut BoundaryBudget,
    pending: &'a mut VecDeque<Notification>,
    output: &'a mut Output,
    command_sequence: &'a mut u64,
}
impl<E: ExecutionPort, C: RunControl> ScopedCommands<'_, E, C> {
    fn checkpoint(&mut self) -> QfResult<()> {
        self.control.check().map_err(|e| self.budget.fail(e))?;
        self.budget.check()?;
        if self.budget.commands >= self.limits.max_commands_per_boundary {
            return Err(self.budget.fail(error(
                ErrorCode::ResourceLimit,
                "strategy_commands",
                "边界内命令数超过预算",
            )));
        }
        self.budget.commands += 1;
        *self.command_sequence = self.command_sequence.checked_add(1).ok_or_else(|| {
            self.budget.fail(error(
                ErrorCode::ResourceLimit,
                "strategy_commands",
                "命令序号溢出",
            ))
        })?;
        Ok(())
    }
    fn notify_result(&mut self, result: &OrderResult) -> QfResult<()> {
        if self.execution.borrow().active_order_count() > self.limits.max_active_orders {
            return Err(self.budget.fail(error(
                ErrorCode::ResourceLimit,
                "active_orders",
                "活动订单超过预算",
            )));
        }
        if let Some(id) = result.order_id.as_ref().filter(|_| !result.unchanged) {
            let order = self
                .execution
                .borrow()
                .order(id)
                .map_err(|e| self.budget.fail(e))?;
            enqueue(
                Notification::Order(order),
                self.pending,
                self.output,
                self.budget,
                self.limits,
            )?;
        }
        Ok(())
    }
}
fn rejected(failure: QfError) -> OrderResult {
    OrderResult {
        accepted: false,
        order_id: None,
        reason_code: Some(failure.code),
        message: failure.message,
        unchanged: false,
        requested_quantity: None,
        effective_quantity: None,
    }
}
impl<E: ExecutionPort, C: RunControl> CommandSink for ScopedCommands<'_, E, C> {
    fn submit(&mut self, intent: &OrderIntent) -> QfResult<OrderResult> {
        self.checkpoint()?;
        if !self.universe.contains(&intent.security) {
            return Ok(rejected(error(
                ErrorCode::InvalidOrder,
                "order",
                "下单标的不在run授权集合内",
            )));
        }
        if let Err(failure) = intent.validate() {
            return Ok(rejected(failure));
        }
        let activation = OrderActivation {
            submitted_ns: self.visibility.now_ns,
            effective_session: self.calendar.effective_session(
                self.session_index,
                self.visibility.now_ns,
                self.closing,
            )?,
            eligible_interval_start: self.visibility.now_ns,
            eligible_after_event: self.visibility.market_through.clone(),
        };
        let mut normalized = intent.clone();
        normalized.submitted_at = EventKey {
            time_ns: activation.submitted_ns,
            phase: EventPhase::Callback,
            security: intent.security.clone(),
            identity: EventIdentity {
                source_session: self.calendar.sessions()[self.session_index]
                    .session
                    .key
                    .clone(),
                channel: ChannelKey::new("strategy_commands")?,
                sequence: Some(Sequence::new(*self.command_sequence)),
                stable_input_sequence: Sequence::new(*self.command_sequence),
            },
        };
        let result = self
            .execution
            .borrow_mut()
            .submit_order(&normalized, &activation)
            .map_err(|e| self.budget.fail(e))?;
        self.notify_result(&result)?;
        Ok(result)
    }
    fn cancel(&mut self, order_id: &str) -> QfResult<OrderResult> {
        self.checkpoint()?;
        crate::types::keys::label(order_id, 128)?;
        let result = self
            .execution
            .borrow_mut()
            .cancel_order(order_id)
            .map_err(|e| self.budget.fail(e))?;
        self.notify_result(&result)?;
        Ok(result)
    }
}

enum Call<'a> {
    Initialize,
    BeforeOpen,
    AfterClose,
    Bars(&'a BarBoundary),
    Tick(&'a MarketEvent),
    Scheduled(&'a ScheduledCall),
    Notification(&'a Notification),
}
struct Runtime<'a, D, E, M, R, H, C, W> {
    ports: EnginePorts<'a, D, E, M, R, H, C, W>,
    clock: SimClock,
    registration: Registration,
    pending: VecDeque<Notification>,
    output: Output,
    budget: BoundaryBudget,
    command_sequence: u64,
    progress: Progress,
    active_sessions: BTreeSet<SessionKey>,
}
impl<S: EventSource> Engine<S> {
    /// Normal integration path: project D01's validated run configuration and
    /// reject a calendar belonging to a different date scope/reference.
    /// D09 initializes the account from the same accepted config separately.
    pub fn from_run(
        config: &RunConfig,
        result_id: String,
        limits: EngineLimits,
        calendar: SessionCalendar,
        sources: Vec<S>,
    ) -> QfResult<Self> {
        if config
            .reference_calendar
            .as_ref()
            .is_some_and(|reference| reference != calendar.reference())
            || calendar.sessions().iter().any(|s| {
                s.date.as_str() < config.start.as_str() || s.date.as_str() > config.end.as_str()
            })
        {
            return Err(error(
                ErrorCode::InvalidRunConfig,
                "engine_calendar",
                "参考日历与归一化run日期范围不一致",
            ));
        }
        Self::new(
            EngineConfig {
                frequency: config.frequency,
                execution_model: config.execution_model,
                universe: config.universe.clone(),
                result_id,
                limits,
            },
            calendar,
            sources,
        )
    }
    pub fn new(config: EngineConfig, calendar: SessionCalendar, sources: Vec<S>) -> QfResult<Self> {
        config.limits.validate()?;
        crate::types::keys::label(&config.result_id, 128)?;
        Registration::new(&config.universe, config.frequency)?;
        let supported = matches!(
            (config.frequency, config.execution_model),
            (
                Frequency::Tick,
                ExecutionModel::TradeTickV1 | ExecutionModel::QuoteTickV1
            )
        ) || config.frequency != Frequency::Tick
            && config.execution_model == ExecutionModel::BarNextIntervalV1;
        if !supported {
            return Err(error(
                ErrorCode::InvalidRunConfig,
                "engine_config",
                "运行频率与撮合源模型不一致",
            ));
        }
        let merge = crate::clock::StreamingMerge::new(sources, config.limits.merge)?;
        Ok(Self {
            config,
            calendar,
            merge,
        })
    }
    /// Consumes the driver: a run cannot recursively re-enter or resume it.
    pub fn run<D, E, M, R, H, C, W>(
        mut self,
        ports: EnginePorts<'_, D, E, M, R, H, C, W>,
    ) -> EngineReport
    where
        D: StrategyDataPort,
        E: ExecutionPort,
        M: Matcher,
        R: RulesPort<M::Rules>,
        H: EngineStrategy<Frame = D::Frame>,
        C: RunControl,
        W: ResultSink,
    {
        let start = self.calendar.sessions()[0].before_open_ns;
        let mut runtime = Runtime {
            ports,
            clock: SimClock::new(start),
            registration: Registration::new(&self.config.universe, self.config.frequency)
                .expect("validated registration"),
            pending: VecDeque::new(),
            output: Output::new(),
            budget: BoundaryBudget::new(),
            command_sequence: 0,
            progress: Progress {
                completed_events: 0,
                completed_sessions: 0,
                now_ns: start,
            },
            active_sessions: BTreeSet::new(),
        };
        let mut result = runtime.drive(&mut self);
        // finish is called exactly once on all paths; a primary failure keeps its
        // diagnosis. D10 supplies safe Python exception/function/line mapping.
        let mut cleanup_errors = Vec::new();
        if let Err(failure) = runtime.ports.strategy.finish() {
            if result.is_ok() {
                result = Err(failure);
            } else {
                cleanup_errors.push(failure);
            }
        }
        if result.is_ok() {
            result = runtime
                .flush()
                .and_then(|_| runtime.check_data())
                .and_then(|_| runtime.ports.control.check());
        }
        let success = RunOutcome::Succeeded {
            result_id: self.config.result_id.clone(),
        };
        if result.is_ok() {
            result = runtime.ports.results.finalize(&success);
        }
        let partial = runtime.output.write_attempted || runtime.progress.completed_events > 0;
        let outcome = match result {
            Ok(()) => success,
            Err(failure) => {
                if let Err(abort_error) = runtime.ports.results.abort(&failure) {
                    cleanup_errors.push(abort_error);
                }
                if failure.code == ErrorCode::Cancelled {
                    RunOutcome::Cancelled { partial }
                } else {
                    RunOutcome::Failed {
                        error: failure,
                        partial,
                    }
                }
            }
        };
        if let Err(close_error) = runtime.ports.data.close() {
            cleanup_errors.push(close_error);
        }
        EngineReport {
            outcome,
            progress: runtime.progress,
            merge: self.merge.stats(),
            cleanup_errors,
        }
    }
}

impl<D, E, M, R, H, C, W> Runtime<'_, D, E, M, R, H, C, W>
where
    D: StrategyDataPort,
    E: ExecutionPort,
    M: Matcher,
    R: RulesPort<M::Rules>,
    H: EngineStrategy<Frame = D::Frame>,
    C: RunControl,
    W: ResultSink,
{
    fn check_data(&mut self) -> QfResult<()> {
        self.ports.control.check()?;
        if self.ports.data.check()? == DependencyCheck::DataChanged {
            return Err(error(
                ErrorCode::DataChanged,
                "engine_data",
                "运行内数据或可读性状态已变化",
            ));
        }
        Ok(())
    }
    fn flush(&mut self) -> QfResult<()> {
        self.output.flush(self.ports.results, self.ports.control)
    }
    fn call(
        &mut self,
        call: Call<'_>,
        config: &EngineConfig,
        calendar: &SessionCalendar,
        index: usize,
        closing: bool,
    ) -> QfResult<()> {
        self.ports.control.check()?;
        self.budget.check()?;
        let visibility = self.clock.visibility();
        // Method-scoped account borrows end before invoking the host. Views see
        // synchronous reservations/cancellations within this same callback.
        let execution = RefCell::new(&mut *self.ports.execution);
        let mut view = ScopedView {
            data: self.ports.data,
            execution: &execution,
            visibility,
            universe: &config.universe,
        };
        let mut commands = ScopedCommands {
            execution: &execution,
            control: self.ports.control,
            visibility,
            calendar,
            session_index: index,
            closing,
            universe: &config.universe,
            limits: &config.limits,
            budget: &mut self.budget,
            pending: &mut self.pending,
            output: &mut self.output,
            command_sequence: &mut self.command_sequence,
        };
        let result = match call {
            Call::Initialize => self.ports.strategy.initialize_engine(
                &mut view,
                &mut commands,
                &mut self.registration,
            ),
            Call::BeforeOpen => self.ports.strategy.before_open(&mut view, &mut commands),
            Call::AfterClose => self.ports.strategy.after_close(&mut view, &mut commands),
            Call::Bars(boundary) => {
                self.ports
                    .strategy
                    .handle_bars(boundary, &mut view, &mut commands)
            }
            Call::Tick(event) => self
                .ports
                .strategy
                .callback(event, &mut view, &mut commands),
            Call::Scheduled(scheduled) => {
                self.ports
                    .strategy
                    .scheduled(scheduled, &mut view, &mut commands)
            }
            Call::Notification(notification) => {
                self.ports
                    .strategy
                    .notification(notification, &mut view, &mut commands)
            }
        };
        // Resource/cancellation errors remain terminal even if Python caught them.
        self.budget.check()?;
        result?;
        self.ports.control.check()?;
        self.flush()
    }
    fn drain(
        &mut self,
        config: &EngineConfig,
        calendar: &SessionCalendar,
        index: usize,
        closing: bool,
    ) -> QfResult<()> {
        while let Some(notification) = self.pending.pop_front() {
            self.ports.control.check()?;
            let handlers = self.ports.strategy.handlers();
            if matches!(&notification, Notification::Order(_) if handlers.orders)
                || matches!(&notification, Notification::Trade(_) if handlers.trades)
            {
                self.call(
                    Call::Notification(&notification),
                    config,
                    calendar,
                    index,
                    closing,
                )?;
            }
        }
        self.flush()
    }
    fn timers(
        &mut self,
        calls: &[ScheduledCall],
        cursor: &mut usize,
        through: Nanoseconds,
        config: &EngineConfig,
        calendar: &SessionCalendar,
        session_state: (usize, bool),
    ) -> QfResult<()> {
        let (index, closing) = session_state;
        while let Some(call) = calls.get(*cursor).filter(|c| c.time_ns <= through) {
            self.clock.advance(call.time_ns)?;
            self.progress.now_ns = call.time_ns;
            self.call(Call::Scheduled(call), config, calendar, index, closing)?;
            self.drain(config, calendar, index, closing)?;
            *cursor += 1;
        }
        Ok(())
    }
    fn match_event(
        &mut self,
        event: &MarketEvent,
        config: &EngineConfig,
        session: &CalendarSession,
    ) -> QfResult<()> {
        if self.progress.completed_events >= config.limits.max_events {
            return Err(error(
                ErrorCode::ResourceLimit,
                "engine",
                "运行市场事件总数超过预算",
            ));
        }
        self.check_data()?;
        session.validate_event(event, config.frequency)?;
        let correct = matches!(
            (event, config.execution_model),
            (MarketEvent::Bar(_), ExecutionModel::BarNextIntervalV1)
                | (MarketEvent::TradeTick(_), ExecutionModel::TradeTickV1)
                | (MarketEvent::QuoteTick(_), ExecutionModel::QuoteTickV1)
        );
        let key = event.key();
        if !correct || !self.registration.authorized(&key.security) {
            return Err(error(
                ErrorCode::InvalidContract,
                "market_source",
                "回放事件不在run撮合源或授权集合内",
            ));
        }
        if self.ports.execution.active_order_count() > config.limits.max_active_orders {
            return Err(error(
                ErrorCode::ResourceLimit,
                "active_orders",
                "活动订单超过预算",
            ));
        }
        let active = self
            .ports
            .execution
            .active_orders(&key.security, config.limits.max_active_orders)?;
        if active.len() > config.limits.max_active_orders {
            return Err(error(
                ErrorCode::ResourceLimit,
                "active_orders",
                "活动订单超过预算",
            ));
        }
        let mut ids = BTreeSet::new();
        for order in &active {
            crate::types::keys::label(&order.order_id, 128)?;
            if !ids.insert(&order.order_id)
                || order.security != key.security
                || order.filled_quantity > order.quantity
                || !matches!(
                    order.status,
                    OrderStatus::Accepted | OrderStatus::Open | OrderStatus::PartiallyFilled
                )
            {
                return Err(error(
                    ErrorCode::InvalidContract,
                    "active_orders",
                    "活动订单状态无效或重复",
                ));
            }
        }
        let eligible: Vec<_> = active
            .into_iter()
            .filter(|order| {
                order.security == key.security
                    && self.active_sessions.contains(&order.effective_session)
                    && (order.tif != TimeInForce::Day
                        || order.effective_session == session.session.key)
                    && match event {
                        MarketEvent::Bar(bar) => {
                            order.submitted_ns <= bar.interval_start_ns
                                && order
                                    .eligible_interval_start
                                    .is_some_and(|start| start <= bar.interval_start_ns)
                        }
                        _ => {
                            order.submitted_ns <= key.time_ns
                                && order
                                    .eligible_after_event
                                    .as_ref()
                                    .is_none_or(|last| &key > last)
                        }
                    }
            })
            .collect();
        let rules = self.ports.rules.for_event(event, session)?;
        let outcome = self.ports.matcher.consume(event, &eligible, &rules)?;
        if outcome.fills.len().saturating_add(outcome.orders.len())
            > config
                .limits
                .max_notifications_per_boundary
                .saturating_sub(self.budget.notifications)
        {
            return Err(error(
                ErrorCode::ResourceLimit,
                "matcher",
                "撮合输出超过本边界通知预算",
            ));
        }
        let mut fill_quantities = BTreeMap::new();
        let mut trade_ids = BTreeSet::new();
        let eligible_by_id: BTreeMap<_, _> =
            eligible.iter().map(|o| (o.order_id.as_str(), o)).collect();
        for fill in &outcome.fills {
            crate::types::keys::label(&fill.trade_id, 128)?;
            let order = eligible_by_id.get(fill.order_id.as_str()).ok_or_else(|| {
                error(
                    ErrorCode::InvalidContract,
                    "matcher",
                    "撮合返回未生效订单成交",
                )
            })?;
            let quantity: i64 = fill_quantities.get(&fill.order_id).copied().unwrap_or(0);
            let quantity = quantity.checked_add(fill.quantity.get()).ok_or_else(|| {
                error(
                    ErrorCode::NumericRangeUnsupported,
                    "matcher",
                    "成交数量溢出",
                )
            })?;
            fill_quantities.insert(&fill.order_id, quantity);
            if !trade_ids.insert(&fill.trade_id)
                || fill.quantity.get() == 0
                || quantity > order.quantity.get() - order.filled_quantity.get()
                || fill.security != order.security
                || fill.side != order.side
                || fill.execution_time_ns != key.time_ns
            {
                return Err(error(
                    ErrorCode::InvalidContract,
                    "matcher",
                    "成交标的、方向、时间或数量不符合事件边界",
                ));
            }
        }
        let mut transitions = BTreeSet::new();
        for order in &outcome.orders {
            if !transitions.insert(order.order_id.as_str())
                || !eligible_by_id
                    .get(order.order_id.as_str())
                    .is_some_and(|previous| {
                        previous.order_id == order.order_id
                            && previous.security == order.security
                            && previous.side == order.side
                            && previous.quantity == order.quantity
                            && previous.filled_quantity.get().checked_add(
                                fill_quantities.get(&order.order_id).copied().unwrap_or(0),
                            ) == Some(order.filled_quantity.get())
                    })
                || order.filled_quantity > order.quantity
            {
                return Err(error(
                    ErrorCode::InvalidContract,
                    "matcher",
                    "订单状态更新不符合撮合输入",
                ));
            }
        }
        if fill_quantities
            .keys()
            .any(|id| !transitions.contains(id.as_str()))
        {
            return Err(error(
                ErrorCode::InvalidContract,
                "matcher",
                "成交必须带有对应订单数量更新",
            ));
        }
        for fill in outcome.fills {
            self.ports.control.check()?;
            let assessed = self.ports.execution.assess_fill(&fill)?;
            let mut expected = fill;
            expected.fee = assessed.fee;
            if assessed != expected || assessed.fee.is_negative() {
                return Err(error(
                    ErrorCode::InvalidContract,
                    "account_fill",
                    "账户费用核定不能改变成交身份、价格、数量或时间",
                ));
            }
            let fill = assessed;
            self.ports.execution.apply_fill(&fill)?;
            enqueue(
                Notification::Trade(fill),
                &mut self.pending,
                &mut self.output,
                &mut self.budget,
                &config.limits,
            )?;
        }
        for order in outcome.orders {
            self.ports.control.check()?;
            self.ports.execution.transition(&order)?;
            enqueue(
                Notification::Order(order),
                &mut self.pending,
                &mut self.output,
                &mut self.budget,
                &config.limits,
            )?;
        }
        self.ports.execution.mark(event)?;
        self.ports.data.publish(event)?;
        self.clock.publish(key)?;
        self.progress.completed_events = self
            .progress
            .completed_events
            .checked_add(1)
            .ok_or_else(|| error(ErrorCode::ResourceLimit, "engine", "事件计数溢出"))?;
        self.progress.now_ns = self.clock.visibility().now_ns;
        if self.progress.completed_events > config.limits.max_events {
            return Err(error(
                ErrorCode::ResourceLimit,
                "engine",
                "运行市场事件总数超过预算",
            ));
        }
        if self
            .progress
            .completed_events
            .is_multiple_of(config.limits.progress_every_events)
        {
            self.ports.control.progress(self.progress)?;
        }
        self.flush()
    }
    fn drive<S: EventSource>(&mut self, engine: &mut Engine<S>) -> QfResult<()> {
        let config = &engine.config;
        let calendar = &engine.calendar;
        self.check_data()?;
        self.call(Call::Initialize, config, calendar, 0, false)?;
        self.registration.seal();
        // Validate all schedule/calendar precision before processing markets,
        // holding only one session's bounded invocation list at a time.
        for session in calendar.sessions() {
            self.ports.control.check()?;
            self.registration.for_session(session)?;
        }
        self.drain(config, calendar, 0, false)?;
        self.ports.control.progress(self.progress)?;
        for (index, session) in calendar.sessions().iter().enumerate() {
            self.budget = BoundaryBudget::new();
            self.check_data()?;
            self.clock.advance(session.before_open_ns)?;
            self.active_sessions.insert(session.session.key.clone());
            self.ports.execution.settle(&session.session.key)?;
            let opening_orders = self.ports.execution.session_start(session)?;
            if opening_orders.len() > config.limits.max_notifications_per_boundary {
                return Err(error(
                    ErrorCode::ResourceLimit,
                    "session_start",
                    "会话状态通知超过预算",
                ));
            }
            for order in opening_orders {
                self.ports.control.check()?;
                enqueue(
                    Notification::Order(order),
                    &mut self.pending,
                    &mut self.output,
                    &mut self.budget,
                    &config.limits,
                )?;
            }
            self.drain(config, calendar, index, false)?;
            if self.ports.strategy.handlers().before_open {
                self.call(Call::BeforeOpen, config, calendar, index, false)?;
                self.drain(config, calendar, index, false)?;
            }
            let calls = self.registration.for_session(session)?;
            let (before, remaining): (Vec<_>, Vec<_>) = calls
                .into_iter()
                .partition(|c| c.time == ScheduleTime::BeforeOpen);
            let (intraday, after): (Vec<_>, Vec<_>) = remaining
                .into_iter()
                .partition(|c| c.time != ScheduleTime::AfterClose);
            let mut cursor = 0;
            self.timers(
                &before,
                &mut cursor,
                session.before_open_ns,
                config,
                calendar,
                (index, false),
            )?;
            let mut timer_cursor = 0;
            loop {
                self.ports.control.check()?;
                let next_time = engine
                    .merge
                    .peek(self.ports.control)?
                    .map(|e| e.key().time_ns);
                if next_time.is_some_and(|t| t < session.session.open_ns) {
                    return Err(error(
                        ErrorCode::InvalidContract,
                        "market_source",
                        "市场事件早于当前会话或未消费会话",
                    ));
                }
                let next_time = next_time.filter(|t| *t <= session.session.close_ns);
                if let Some(timer) = intraday
                    .get(timer_cursor)
                    .filter(|t| next_time.is_none_or(|market| t.time_ns < market))
                {
                    // There is no market event at this timer's timestamp. At
                    // close all earlier events are complete, so new DAY orders
                    // belong to the next session even with an empty source.
                    let closing = timer.time_ns >= session.session.close_ns;
                    self.budget = BoundaryBudget::new();
                    self.timers(
                        &intraday,
                        &mut timer_cursor,
                        timer.time_ns,
                        config,
                        calendar,
                        (index, closing),
                    )?;
                    continue;
                }
                let Some(time) = next_time else {
                    break;
                };
                self.budget = BoundaryBudget::new();
                if config.execution_model == ExecutionModel::BarNextIntervalV1 {
                    let mut securities = BTreeSet::new();
                    let mut count = 0;
                    while engine
                        .merge
                        .peek(self.ports.control)?
                        .is_some_and(|e| e.key().time_ns == time)
                    {
                        if count == config.limits.max_bar_events_per_boundary {
                            return Err(error(
                                ErrorCode::ResourceLimit,
                                "bar_barrier",
                                "同刻Bar数量超过屏障预算",
                            ));
                        }
                        let event = engine.merge.pop(self.ports.control)?.expect("peeked event");
                        self.match_event(&event, config, session)?;
                        let security = event.key().security;
                        if self
                            .registration
                            .subscribed(&security, SubscriptionKind::Bar)
                        {
                            securities.insert(security);
                        }
                        count += 1;
                    }
                    // ALL accounts and market publications at this Bar end are
                    // complete before notifications, timers, or handle_data.
                    self.drain(config, calendar, index, time >= session.session.close_ns)?;
                    self.timers(
                        &intraday,
                        &mut timer_cursor,
                        time,
                        config,
                        calendar,
                        (index, time >= session.session.close_ns),
                    )?;
                    if self.ports.strategy.handlers().bars && !securities.is_empty() {
                        let boundary = BarBoundary {
                            time_ns: time,
                            securities: securities.into_iter().collect(),
                        };
                        self.call(
                            Call::Bars(&boundary),
                            config,
                            calendar,
                            index,
                            time >= session.session.close_ns,
                        )?;
                        self.drain(config, calendar, index, time >= session.session.close_ns)?;
                    }
                } else {
                    let event = engine.merge.pop(self.ports.control)?.expect("peeked event");
                    self.match_event(&event, config, session)?;
                    let closing = time >= session.session.close_ns
                        && engine
                            .merge
                            .peek(self.ports.control)?
                            .is_none_or(|next| next.key().time_ns > session.session.close_ns);
                    self.drain(config, calendar, index, closing)?;
                    if self.ports.strategy.handlers().ticks
                        && self
                            .registration
                            .subscribed(&event.key().security, SubscriptionKind::Tick)
                    {
                        self.call(Call::Tick(&event), config, calendar, index, closing)?;
                        self.drain(config, calendar, index, closing)?;
                    }
                    if engine
                        .merge
                        .peek(self.ports.control)?
                        .is_none_or(|e| e.key().time_ns != time)
                    {
                        self.timers(
                            &intraday,
                            &mut timer_cursor,
                            time,
                            config,
                            calendar,
                            (index, closing),
                        )?;
                    }
                }
            }
            // Last market event is matched first, then DAY expires, then all
            // after_close callbacks. Expiry notifications also belong next DAY.
            self.budget = BoundaryBudget::new();
            self.clock.advance(session.session.close_ns)?;
            let closing_orders = self.ports.execution.session_end(session)?;
            if closing_orders.len() > config.limits.max_notifications_per_boundary {
                return Err(error(
                    ErrorCode::ResourceLimit,
                    "session_end",
                    "会话关闭通知超过预算",
                ));
            }
            for order in closing_orders {
                self.ports.control.check()?;
                enqueue(
                    Notification::Order(order),
                    &mut self.pending,
                    &mut self.output,
                    &mut self.budget,
                    &config.limits,
                )?;
            }
            let expired = self.ports.execution.expire_day(&session.session.key)?;
            if expired.len() > config.limits.max_notifications_per_boundary {
                return Err(error(
                    ErrorCode::ResourceLimit,
                    "day_expiry",
                    "DAY到期通知超过预算",
                ));
            }
            for order in expired {
                self.ports.control.check()?;
                enqueue(
                    Notification::Order(order),
                    &mut self.pending,
                    &mut self.output,
                    &mut self.budget,
                    &config.limits,
                )?;
            }
            self.drain(config, calendar, index, true)?;
            let mut after_cursor = 0;
            self.timers(
                &after,
                &mut after_cursor,
                session.after_close_ns,
                config,
                calendar,
                (index, true),
            )?;
            self.clock.advance(session.after_close_ns)?;
            if self.ports.strategy.handlers().after_close {
                self.call(Call::AfterClose, config, calendar, index, true)?;
                self.drain(config, calendar, index, true)?;
            }
            self.progress.completed_sessions += 1;
            self.progress.now_ns = self.clock.visibility().now_ns;
            self.ports.control.progress(self.progress)?;
        }
        if engine.merge.peek(self.ports.control)?.is_some() {
            return Err(error(
                ErrorCode::InvalidContract,
                "market_source",
                "回放事件超出run参考日历",
            ));
        }
        self.check_data()?;
        self.flush()
    }
}

//! One waiting Rust callback scope, synchronous requests, no borrowed PyClass.
use super::{source, spec::invalid, spool::SharedSpool};
use qf_core::clock::schedule::ScheduledCall;
use qf_core::clock::{Cadence, CallbackId, Registration, ScheduleTime, SubscriptionKind};
use qf_core::engine::{BarBoundary, EngineStrategy, Handlers, Notification};
use qf_core::orders::{IntentValue, OrderIntent, OrderStyle, Side, TimeInForce};
use qf_core::results::ResultRecord;
use qf_core::strategy::{CommandSink, ReadView, StrategyHost};
use qf_core::types::*;
use qf_core::{ErrorCode, QfError, QfResult};
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{
    Arc,
    mpsc::{Receiver, SyncSender},
};
use std::time::{Duration, Instant};

pub struct Response {
    pub body: Value,
    pub payload: Vec<u8>,
}
pub enum Request {
    Call {
        operation: String,
        body: Value,
        reply: SyncSender<QfResult<Response>>,
    },
    Done(QfResult<()>),
}
pub struct Callback {
    pub kind: String,
    pub payload: Value,
    pub requests: SyncSender<Request>,
}
pub struct Host {
    pub callbacks: SyncSender<Callback>,
    pub handlers: Handlers,
    pub cancelled: Arc<AtomicBool>,
    pub deadline: Instant,
    pub spool: SharedSpool,
    pub gateway: Option<source::Gateway>,
    pub first_session: SessionKey,
    pub callback_count: u64,
    pub logs_truncated: bool,
    record_points: u64,
    log_bytes: usize,
    fault: Option<QfError>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Submit {
    security: SecurityKey,
    side: Side,
    value: IntentValue,
    style: OrderStyle,
    tif: TimeInForce,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Schedule {
    callback: String,
    cadence: String,
    trading_day: i16,
    time: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Subscription {
    securities: Vec<SecurityKey>,
    events: String,
}
pub fn json_value(value: impl serde::Serialize) -> QfResult<Value> {
    serde_json::to_value(value).map_err(|_| invalid("回调输出编码失败"))
}
fn parse<T: serde::de::DeserializeOwned>(value: Value) -> QfResult<T> {
    serde_json::from_value(value).map_err(|_| invalid("回调命令契约无效"))
}
impl Host {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        callbacks: SyncSender<Callback>,
        handlers: Handlers,
        cancelled: Arc<AtomicBool>,
        deadline: Instant,
        spool: SharedSpool,
        gateway: Option<source::Gateway>,
        first_session: SessionKey,
    ) -> Self {
        Self {
            callbacks,
            handlers,
            cancelled,
            deadline,
            spool,
            gateway,
            first_session,
            callback_count: 0,
            logs_truncated: false,
            record_points: 0,
            log_bytes: 0,
            fault: None,
        }
    }
    fn check(&self) -> QfResult<()> {
        if self.cancelled.load(Ordering::Acquire) {
            return Err(QfError::new(
                ErrorCode::Cancelled,
                "strategy_callback",
                "运行已取消",
            ));
        }
        if Instant::now() >= self.deadline {
            return Err(QfError::new(
                ErrorCode::ResourceLimit,
                "strategy_callback",
                "运行超过墙钟预算",
            ));
        }
        self.fault.clone().map_or(Ok(()), Err)
    }
    fn respond(
        &mut self,
        op: &str,
        body: Value,
        view: &mut dyn ReadView<Frame = source::Frame>,
        commands: &mut dyn CommandSink,
        registration: &mut Option<&mut Registration>,
    ) -> QfResult<Response> {
        self.check()?;
        let output = match op {
            "fault" => {
                let fault: QfError = parse(body)?;
                if !matches!(
                    fault.code,
                    ErrorCode::ResourceLimit
                        | ErrorCode::ResultBudgetExceeded
                        | ErrorCode::DataChanged
                        | ErrorCode::Cancelled
                ) {
                    return Err(invalid("回调终止错误类型无效"));
                }
                self.fault = Some(fault.clone());
                return Err(fault);
            }
            "portfolio" => json_value(view.account()?)?,
            "current" => json_value(view.current_data(&parse::<Vec<SecurityKey>>(body)?)?)?,
            "order" => {
                let request: Submit = parse(body)?;
                json_value(commands.submit(&OrderIntent {
                    security: request.security.clone(),
                    side: request.side,
                    value: request.value,
                    style: request.style,
                    tif: request.tif,
                    submitted_at: EventKey {
                        time_ns: view.now_ns(),
                        phase: EventPhase::Callback,
                        security: request.security,
                        identity: EventIdentity {
                            source_session: self.first_session.clone(),
                            channel: crate::runtime::channel()?,
                            sequence: None,
                            stable_input_sequence: Sequence::new(0),
                        },
                    },
                })?)?
            }
            "cancel" => json_value(commands.cancel(&parse::<String>(body)?)?)?,
            "get_order" => json_value(commands.get_order(&parse::<String>(body)?)?)?,
            "open_orders" => json_value(commands.get_open_orders()?)?,
            "trades" => {
                #[derive(Deserialize)]
                #[serde(deny_unknown_fields)]
                struct Page {
                    after: Option<String>,
                    limit: usize,
                }
                let page: Page = parse(body)?;
                self.spool
                    .lock()
                    .map_err(|_| invalid("结果读取锁失败"))?
                    .page("trades", page.after.as_deref(), page.limit)?
            }
            "schedule" => {
                let request: Schedule = parse(body)?;
                let cadence = match request.cadence.as_str() {
                    "daily" => Cadence::Daily,
                    "weekly" => Cadence::Weekly(request.trading_day),
                    "monthly" => Cadence::Monthly(request.trading_day),
                    _ => return Err(invalid("调度频率无效")),
                };
                registration
                    .as_deref_mut()
                    .ok_or_else(|| invalid("调度只能在initialize注册"))?
                    .register(
                        CallbackId::new(request.callback)?,
                        cadence,
                        request.time.parse::<ScheduleTime>()?,
                    )?;
                Value::Null
            }
            "subscribe" => {
                let request: Subscription = parse(body)?;
                let events = match request.events.as_str() {
                    "bar" => SubscriptionKind::Bar,
                    "tick" => SubscriptionKind::Tick,
                    _ => return Err(invalid("订阅事件类型无效")),
                };
                registration
                    .as_deref_mut()
                    .ok_or_else(|| invalid("订阅只能在initialize注册"))?
                    .subscribe(&request.securities, events)?;
                Value::Null
            }
            "emit" => {
                let mut record: ResultRecord = parse(body)?;
                match &mut record {
                    ResultRecord::Record(row) => {
                        if row.time_ns != view.now_ns()
                            || row.values.is_empty()
                            || row.values.len() > 128
                            || self.record_points >= 100_000
                        {
                            return Err(QfError::new(
                                ErrorCode::ResultBudgetExceeded,
                                "record",
                                "指标点数、名称或时间超过预算",
                            ));
                        }
                        for (name, value) in &row.values {
                            super::label(name)?;
                            if let qf_core::results::RecordValue::Text(s) = value
                                && s.len() > 1024
                            {
                                return Err(invalid("指标文本超过预算"));
                            }
                        }
                        self.record_points += 1;
                    }
                    ResultRecord::Log(row) => {
                        if row.time_ns != view.now_ns() {
                            return Err(invalid("日志时点与回调不一致"));
                        }
                        // Both per-message and exhausted-run truncation remain
                        // visible in the run summary, even at an exact cutoff.
                        if self.log_bytes >= 1024 * 1024 {
                            self.logs_truncated = true;
                            return Ok(Response {
                                body: json!({"truncated":true}),
                                payload: Vec::new(),
                            });
                        }
                        let maximum = 4096.min(1024 * 1024 - self.log_bytes);
                        if row.message.len() > maximum {
                            let mut end = maximum;
                            while !row.message.is_char_boundary(end) {
                                end -= 1;
                            }
                            row.message.truncate(end);
                            row.truncated = true;
                        }
                        self.logs_truncated |= row.truncated;
                        self.log_bytes += row.message.len();
                    }
                    _ => return Err(invalid("策略不可写订单、成交或账户结果")),
                }
                commands.emit(record)?;
                Value::Null
            }
            "data" => {
                #[derive(Deserialize)]
                #[serde(deny_unknown_fields)]
                struct Query {
                    op: String,
                    body: Value,
                }
                let query: Query = parse(body)?;
                let gateway = self.gateway.as_ref().ok_or_else(|| {
                    QfError::new(
                        ErrorCode::CapabilityUnavailable,
                        "research_data",
                        "当前运行缺少已授权研究数据通道",
                    )
                })?;
                let (status, body, payload) = gateway
                    .lock()
                    .map_err(|_| invalid("数据通道锁失败"))?
                    .callback_request(&query.op, query.body)?;
                return Ok(Response {
                    body: json!({"status":status,"body":body}),
                    payload,
                });
            }
            _ => return Err(invalid("未知策略操作")),
        };
        Ok(Response {
            body: output,
            payload: Vec::new(),
        })
    }
    fn serve(
        &mut self,
        receiver: Receiver<Request>,
        view: &mut dyn ReadView<Frame = source::Frame>,
        commands: &mut dyn CommandSink,
        mut registration: Option<&mut Registration>,
    ) -> QfResult<()> {
        loop {
            self.check()?;
            match receiver.recv_timeout(Duration::from_millis(50)) {
                Ok(Request::Done(result)) => {
                    self.check()?;
                    return result;
                }
                Ok(Request::Call {
                    operation,
                    body,
                    reply,
                }) => {
                    let result = self.respond(&operation, body, view, commands, &mut registration);
                    if let Err(failure) = &result
                        && matches!(
                            failure.code,
                            ErrorCode::ResourceLimit
                                | ErrorCode::ResultBudgetExceeded
                                | ErrorCode::Cancelled
                                | ErrorCode::DataChanged
                        )
                    {
                        self.fault = Some(failure.clone());
                    }
                    let _ = reply.send(result);
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                Err(_) => {
                    return Err(QfError::new(
                        ErrorCode::Cancelled,
                        "strategy_callback",
                        "回调通道已关闭",
                    ));
                }
            }
        }
    }
    fn invoke(
        &mut self,
        kind: &str,
        data: Value,
        view: &mut dyn ReadView<Frame = source::Frame>,
        commands: &mut dyn CommandSink,
        registration: Option<&mut Registration>,
    ) -> QfResult<()> {
        self.check()?;
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        let visibility = view.visibility();
        self.callbacks.send(Callback { kind:kind.into(),payload:json!({"boundary":{"now_ns":visibility.now_ns,"market_through":visibility.market_through},"data":data}),requests:sender })
            .map_err(|_|QfError::new(ErrorCode::Cancelled,"strategy_callback","策略接收器已退出"))?;
        self.callback_count += 1;
        self.serve(receiver, view, commands, registration)
    }
}
impl StrategyHost for Host {
    type Frame = source::Frame;
    fn initialize(
        &mut self,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.invoke("initialize", Value::Null, view, commands, None)
    }
    fn callback(
        &mut self,
        event: &MarketEvent,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.invoke("handle_tick", json_value(event)?, view, commands, None)
    }
    fn finish(&mut self) -> QfResult<()> {
        Ok(())
    }
}
impl EngineStrategy for Host {
    fn handlers(&self) -> Handlers {
        self.handlers
    }
    fn initialize_engine(
        &mut self,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
        registration: &mut Registration,
    ) -> QfResult<()> {
        self.invoke(
            "initialize",
            Value::Null,
            view,
            commands,
            Some(registration),
        )
    }
    fn before_open(
        &mut self,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.invoke("before_trading_start", Value::Null, view, commands, None)
    }
    fn after_close(
        &mut self,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.invoke("after_trading_end", Value::Null, view, commands, None)
    }
    fn handle_bars(
        &mut self,
        boundary: &BarBoundary,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.invoke(
            "handle_data",
            json!({"time_ns":boundary.time_ns,"securities":boundary.securities}),
            view,
            commands,
            None,
        )
    }
    fn scheduled(
        &mut self,
        call: &ScheduledCall,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        self.invoke(
            "scheduled",
            json!({"callback":call.callback.as_str()}),
            view,
            commands,
            None,
        )
    }
    fn notification(
        &mut self,
        event: &Notification,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()> {
        match event {
            Notification::Order(order) => {
                self.invoke("on_order", json_value(order)?, view, commands, None)
            }
            Notification::Trade(trade) => {
                self.invoke("on_trade", json_value(trade)?, view, commands, None)
            }
        }
    }
}

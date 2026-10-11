//! D10 worker binding. Rust owns the clock, matcher, orders and account.
mod host;
mod source;
mod spec;
mod spool;
use crate::{convert, python_error};
use pyo3::prelude::*;
use pyo3::types::PyAny;
use qf_core::accounting::{Account, AccountLimits, AccountPort};
use qf_core::clock::{Checkpoint, SessionCalendar};
use qf_core::data::views::{ViewLease, bounded_json};
use qf_core::engine::{
    Engine, EngineLimits, EnginePorts, Handlers, Progress, RulesPort, RunControl,
};
use qf_core::matching::{
    FillBudget, MatchOutcome, Matcher,
    bar::{BarMatcher, BarRules},
    tick::TickMatcher,
};
use qf_core::orders::{Order, OrderLimits, OrderManager};
use qf_core::results::{ResultKind, ResultSink};
use qf_core::run::{ExecutionModel, RunOutcome};
use qf_core::types::*;
use qf_core::{ErrorCode, QfError, QfResult};
use serde_json::{Value, json};
use spec::{Facts, Spec, invalid};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
    mpsc::{RecvTimeoutError, SyncSender, sync_channel},
};
use std::time::{Duration, Instant};

pub fn label(s: &str) -> QfResult<()> {
    SecurityKey::new(s).map(|_| ())
}
fn channel() -> QfResult<qf_core::types::keys::ChannelKey> {
    qf_core::types::keys::ChannelKey::new("python_commands")
}
fn encode(value: &impl serde::Serialize) -> QfResult<String> {
    let text = serde_json::to_string(value).map_err(|_| invalid("策略消息编码失败"))?;
    if text.len() > qf_core::run::MAX_CONTROL_BYTES {
        return Err(QfError::new(
            ErrorCode::ResourceLimit,
            "strategy_callback",
            "策略消息超过长度预算",
        ));
    }
    Ok(text)
}
#[pyclass(name = "CancellationToken")]
#[derive(Default)]
pub struct CancellationToken {
    cancelled: Arc<AtomicBool>,
}
#[pymethods]
impl CancellationToken {
    #[new]
    fn new() -> Self {
        Self::default()
    }
    fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
    }
    #[getter]
    fn cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }
}
#[pyclass(name = "CallbackBridge")]
pub struct CallbackBridge {
    sender: SyncSender<host::Request>,
    lease: ViewLease,
    cancelled: Arc<AtomicBool>,
    owner: std::thread::ThreadId,
}
#[pymethods]
impl CallbackBridge {
    fn check(&self) -> PyResult<()> {
        convert(self.lease.check())
    }
    fn request(&self, py: Python<'_>, operation: &str, input: &str) -> PyResult<(String, Vec<u8>)> {
        convert(self.lease.check())?;
        if std::thread::current().id() != self.owner {
            return Err(python_error(invalid("策略请求不能跨回调线程使用")));
        }
        let body: Value = convert(bounded_json(input))?;
        let (reply, receiver) = sync_channel(1);
        let receiver = Mutex::new(receiver);
        convert(
            self.sender
                .send(host::Request::Call {
                    operation: operation.into(),
                    body,
                    reply,
                })
                .map_err(|_| {
                    QfError::new(
                        ErrorCode::Cancelled,
                        "strategy_callback",
                        "策略请求通道已关闭",
                    )
                }),
        )?;
        loop {
            match py.detach(|| {
                receiver
                    .lock()
                    .expect("receiver lock")
                    .recv_timeout(Duration::from_millis(50))
            }) {
                Ok(result) => {
                    let result = convert(result)?;
                    return Ok((convert(encode(&result.body))?, result.payload));
                }
                Err(RecvTimeoutError::Timeout) => {
                    py.check_signals()?;
                    if self.cancelled.load(Ordering::Acquire) {
                        return Err(python_error(QfError::new(
                            ErrorCode::Cancelled,
                            "strategy_callback",
                            "运行已取消",
                        )));
                    }
                }
                Err(_) => {
                    return Err(python_error(QfError::new(
                        ErrorCode::Cancelled,
                        "strategy_callback",
                        "策略请求已终止",
                    )));
                }
            }
        }
    }
}
#[pyclass(name = "RunResult")]
pub struct RunResult {
    spool: Option<spool::SharedSpool>,
    report: Value,
}
#[pymethods]
impl RunResult {
    fn summary_json(&self) -> PyResult<String> {
        let mut result = self.report.clone();
        if let Some(spool) = &self.spool {
            result["results"] = spool
                .lock()
                .map_err(|_| python_error(invalid("结果读取锁失败")))?
                .summary();
        }
        convert(encode(&result))
    }
    #[pyo3(signature=(kind,after=None,limit=1000))]
    fn page_json(
        &self,
        py: Python<'_>,
        kind: &str,
        after: Option<&str>,
        limit: usize,
    ) -> PyResult<String> {
        let kind: ResultKind = convert(bounded_json(&format!("\"{kind}\"")))?;
        let name = spool::public_kind(kind)
            .ok_or_else(|| python_error(invalid("摘要请使用summary_json")))?;
        let spool = self
            .spool
            .as_ref()
            .ok_or_else(|| python_error(invalid("运行结果已关闭")))?;
        convert(py.detach(|| {
            encode(
                &spool
                    .lock()
                    .map_err(|_| invalid("结果读取锁失败"))?
                    .page(name, after, limit)?,
            )
        }))
    }
    fn close(&mut self) {
        self.spool.take();
    }
}
struct Control {
    cancelled: Arc<AtomicBool>,
    deadline: Instant,
    progress: Progress,
}
impl Checkpoint for Control {
    fn check(&mut self) -> QfResult<()> {
        if self.cancelled.load(Ordering::Acquire) {
            return Err(QfError::new(ErrorCode::Cancelled, "worker", "运行已取消"));
        }
        if Instant::now() >= self.deadline {
            return Err(QfError::new(
                ErrorCode::ResourceLimit,
                "worker",
                "运行超过墙钟预算",
            ));
        }
        Ok(())
    }
}
impl RunControl for Control {
    fn progress(&mut self, progress: Progress) -> QfResult<()> {
        self.check()?;
        self.progress = progress;
        Ok(())
    }
}
struct Rules(Facts);
impl RulesPort<BarRules> for Rules {
    fn for_event(
        &mut self,
        event: &MarketEvent,
        session: &qf_core::clock::CalendarSession,
    ) -> QfResult<BarRules> {
        let row = self.0.row(&event.key().security, &session.session.key)?;
        BarRules::new(
            &self.0.book,
            row.instrument.clone(),
            row.day.clone(),
            self.0.usage.clone(),
        )
    }
}
enum Matching {
    Bar(BarMatcher),
    Tick(TickMatcher),
}
impl Matcher for Matching {
    type Rules = BarRules;
    fn model_description(&self) -> Option<qf_core::matching::ExecutionModelDescription> {
        match self {
            Self::Bar(m) => m.model_description(),
            Self::Tick(m) => m.model_description(),
        }
    }
    fn consume(
        &mut self,
        _event: &MarketEvent,
        _orders: &[Order],
        _rules: &Self::Rules,
    ) -> QfResult<MatchOutcome> {
        Err(invalid("撮合必须使用真实账户FillBudget"))
    }
    fn consume_with_budget(
        &mut self,
        event: &MarketEvent,
        orders: &[Order],
        rules: &Self::Rules,
        budget: &dyn FillBudget,
    ) -> QfResult<MatchOutcome> {
        match self {
            Self::Bar(matcher) => matcher.consume_with_budget(event, orders, rules, budget),
            Self::Tick(matcher) => matcher.consume_with_budget(event, orders, rules, budget),
        }
    }
}
struct CheckedWriter {
    writer: spool::Writer,
    gateway: Option<source::Gateway>,
    context: Option<qf_core::data::DependencyContext>,
}
impl ResultSink for CheckedWriter {
    fn credit(&mut self) -> QfResult<qf_core::results::SinkCredit> {
        self.writer.credit()
    }
    fn write(&mut self, batch: &qf_core::results::ResultBatch) -> QfResult<()> {
        self.writer.write(batch)
    }
    fn finalize(&mut self, outcome: &RunOutcome) -> QfResult<()> {
        use qf_core::data::DataGateway;
        if let (Some(gateway), Some(context)) = (&self.gateway, &self.context) {
            gateway
                .lock()
                .map_err(|_| invalid("数据通道锁失败"))?
                .check(context)?;
        }
        self.writer.finalize(outcome)
    }
    fn abort(&mut self, error: &QfError) -> QfResult<()> {
        self.writer.abort(error)
    }
}
fn execute(
    spec: Spec,
    handlers: Handlers,
    callbacks: SyncSender<host::Callback>,
    cancelled: Arc<AtomicBool>,
    spool: spool::SharedSpool,
) -> QfResult<Value> {
    let facts = Facts::new(&spec)?;
    let calendar = SessionCalendar::new(
        spec.calendar_reference.clone(),
        spec.calendar.clone(),
        spec.next_session.clone(),
    )?;
    let start = spec.calendar[0].before_open_ns;
    let account = Account::new(
        spec.config.initial_cash,
        spec.calendar.clone(),
        facts.usage.clone(),
        AccountLimits {
            max_active_orders: spec.max_active_orders,
            ..AccountLimits::default()
        },
    )?;
    let mut rules = Rules(Facts {
        book: facts.book.clone(),
        usage: facts.usage.clone(),
        rows: facts.rows.clone(),
    });
    let mut execution = OrderManager::new(
        account,
        facts,
        spec.config.universe.clone(),
        OrderLimits {
            max_active: spec.max_active_orders,
            ..OrderLimits::default()
        },
    )?;
    for (security, mark) in spec.initial_marks {
        execution.set_raw_mark(security, mark, start)?;
    }
    for action in spec.corporate_actions {
        execution.add_corporate_action(action, start)?;
    }
    let deadline = Instant::now() + Duration::from_secs(spec.wall_time_seconds);
    let budget = (spec.data_budget_bytes / 8).min(qf_core::data::MAX_ARROW_BATCH_BYTES);
    let (events, mut data) = source::connect(
        spec.source,
        qf_core::data::RunScope {
            run_id: spec.run_id.clone(),
            universe: spec.config.universe.clone(),
        },
        cancelled.clone(),
        budget,
        deadline,
    )?;
    let gateway = data.gateway.clone();
    let context = data.context.clone();
    let mut host = host::Host::new(
        callbacks,
        handlers,
        cancelled.clone(),
        deadline,
        spool.clone(),
        gateway.clone(),
        spec.calendar[0].session.key.clone(),
    );
    let mut matcher = match spec.config.execution_model {
        ExecutionModel::BarNextIntervalV1 => Matching::Bar(BarMatcher::from_run(&spec.config)?),
        _ => Matching::Tick(TickMatcher::from_run(&spec.config)?),
    };
    let limits = EngineLimits {
        max_active_orders: spec.max_active_orders,
        max_commands_per_boundary: spec.max_commands_per_boundary,
        merge: qf_core::clock::MergeLimits {
            chunk_events: spec.chunk_events,
            max_buffered_bytes: budget,
            ..qf_core::clock::MergeLimits::default()
        },
        ..EngineLimits::default()
    };
    let engine = Engine::from_run(&spec.config, spec.run_id, limits, calendar, vec![events])?;
    let mut control = Control {
        cancelled,
        deadline,
        progress: Progress {
            completed_events: 0,
            completed_sessions: 0,
            now_ns: start,
        },
    };
    let mut writer = CheckedWriter {
        writer: spool::Writer(spool.clone()),
        gateway,
        context,
    };
    let report = engine.run(EnginePorts {
        data: &mut data,
        execution: &mut execution,
        matcher: &mut matcher,
        rules: &mut rules,
        strategy: &mut host,
        control: &mut control,
        results: &mut writer,
    });
    spool.lock().map_err(|_| invalid("结果写入锁失败"))?.outcome = Some(report.outcome.clone());
    let portfolio = execution.value(report.progress.now_ns).ok();
    Ok(
        json!({"outcome":report.outcome,"progress":{"completed_events":report.progress.completed_events,"completed_sessions":report.progress.completed_sessions,"now_ns":report.progress.now_ns},
        "merge":{"peak_buffered_events":report.merge.peak_buffered_events,"peak_buffered_bytes":report.merge.peak_buffered_bytes,"source_reads":report.merge.source_reads},
        "callback_count":host.callback_count,"logs_truncated":host.logs_truncated,"portfolio":portfolio,"cleanup_errors":report.cleanup_errors,"production_executed":false}),
    )
}
fn exception(py: Python<'_>, error: &PyErr) -> QfError {
    let value = error.value(py);
    let api_code = value
        .getattr("code")
        .and_then(|v| v.extract::<String>())
        .ok()
        .and_then(|code| serde_json::from_value::<ErrorCode>(Value::String(code)).ok());
    let code = if error.is_instance_of::<pyo3::exceptions::PyKeyboardInterrupt>(py) {
        ErrorCode::Cancelled
    } else {
        api_code.unwrap_or(ErrorCode::StrategyError)
    };
    let mut result = QfError::new(
        code,
        "strategy_callback",
        if code == ErrorCode::Cancelled {
            "策略执行已中断"
        } else {
            "Python策略回调异常"
        },
    );
    if api_code.is_some()
        && let Ok(message) = value.getattr("message").and_then(|v| v.extract::<String>())
        && message.len() <= 4096
    {
        result.message = message;
    }
    let mut frames = Vec::new();
    if let Ok(mut traceback) = value.getattr("__traceback__") {
        for _ in 0..32 {
            if traceback.is_none() {
                break;
            }
            let info = (|| -> PyResult<(String, String, u64)> {
                let code = traceback.getattr("tb_frame")?.getattr("f_code")?;
                Ok((
                    code.getattr("co_filename")?.extract()?,
                    code.getattr("co_name")?.extract()?,
                    traceback.getattr("tb_lineno")?.extract()?,
                ))
            })();
            if let Ok((path, function, line)) = info {
                let file = std::path::Path::new(&path)
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("strategy.py");
                frames.push(format!("{file}:{line}:{function}"));
                // The user call site remains actionable when an API raises
                // through SDK wrappers. Retain the full bounded stack too.
                if !path.replace('\\', "/").contains("/quantfoundry/") {
                    result
                        .scope
                        .insert("file".into(), file.chars().take(128).collect());
                    result
                        .scope
                        .insert("function".into(), function.chars().take(128).collect());
                    result.scope.insert("line".into(), line.to_string());
                }
            }
            match traceback.getattr("tb_next") {
                Ok(next) => traceback = next,
                Err(_) => break,
            }
        }
    }
    if error.is_instance_of::<pyo3::exceptions::PySyntaxError>(py)
        && let Ok(path) = value
            .getattr("filename")
            .and_then(|v| v.extract::<String>())
        && let Ok(line) = value.getattr("lineno").and_then(|v| v.extract::<u64>())
    {
        let file = std::path::Path::new(&path)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("strategy.py");
        result
            .scope
            .insert("file".into(), file.chars().take(128).collect());
        result.scope.insert("function".into(), "<module>".into());
        result.scope.insert("line".into(), line.to_string());
        frames.push(format!("{file}:{line}:<module>"));
    }
    result.scope.insert(
        "traceback".into(),
        frames.join("\n").chars().take(4096).collect(),
    );
    result
}
// Every exit, including Python allocation/encoding failure, joins the Rust
// owner and releases its Arrow source/gateway. No abandoned worker threads.
struct EngineThread {
    handle: Option<std::thread::JoinHandle<QfResult<Value>>>,
    cancelled: Arc<AtomicBool>,
}
impl EngineThread {
    fn join(&mut self, py: Python<'_>) -> PyResult<QfResult<Value>> {
        let handle = self
            .handle
            .take()
            .ok_or_else(|| python_error(invalid("Rust工作线程已经结束")))?;
        py.detach(move || handle.join())
            .map_err(|_| python_error(invalid("Rust工作线程异常退出")))
    }
}
impl Drop for EngineThread {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            self.cancelled.store(true, Ordering::Release);
            let _ = handle.join();
        }
    }
}
#[pyfunction]
pub fn run_strategy(
    py: Python<'_>,
    input: &str,
    handlers: &str,
    dispatch: Py<PyAny>,
    cancellation: PyRef<'_, CancellationToken>,
) -> PyResult<RunResult> {
    let spec: Spec = convert(bounded_json(input))?;
    convert(Facts::new(&spec))?;
    let flags: Value = convert(bounded_json(handlers))?;
    let flag = |name| flags[name].as_bool().unwrap_or(false);
    let handlers = Handlers {
        bars: flag("bars"),
        ticks: flag("ticks"),
        before_open: flag("before_open"),
        after_close: flag("after_close"),
        orders: flag("orders"),
        trades: flag("trades"),
    };
    let spool = Arc::new(Mutex::new(convert(spool::Spool::new(
        &spec.run_id,
        spec.result_budget_bytes,
    ))?));
    let (sender, receiver) = sync_channel(1);
    let receiver = Mutex::new(receiver);
    let cancelled = cancellation.cancelled.clone();
    let result_spool = spool.clone();
    let run_cancelled = cancelled.clone();
    let handle =
        std::thread::spawn(move || execute(spec, handlers, sender, run_cancelled, result_spool));
    let mut thread = EngineThread {
        handle: Some(handle),
        cancelled: cancelled.clone(),
    };
    loop {
        match py.detach(|| {
            receiver
                .lock()
                .expect("receiver lock")
                .recv_timeout(Duration::from_millis(50))
        }) {
            Ok(callback) => {
                let bridge = Py::new(
                    py,
                    CallbackBridge {
                        sender: callback.requests.clone(),
                        lease: ViewLease::default(),
                        cancelled: cancelled.clone(),
                        owner: std::thread::current().id(),
                    },
                )?;
                let payload = convert(encode(&callback.payload))?;
                let result = dispatch
                    .call1(py, (callback.kind, payload, bridge.clone_ref(py)))
                    .map(|_| ())
                    .map_err(|e| exception(py, &e));
                bridge.borrow(py).lease.expire();
                let _ = callback.requests.send(host::Request::Done(result));
            }
            Err(RecvTimeoutError::Timeout) => {
                if py.check_signals().is_err() {
                    cancelled.store(true, Ordering::Release);
                }
            }
            Err(_) => break,
        }
    }
    let result = thread.join(py)?;
    let report = match result {
        Ok(report) => report,
        Err(error) => {
            let outcome = if error.code == ErrorCode::Cancelled {
                RunOutcome::Cancelled { partial: false }
            } else {
                RunOutcome::Failed {
                    error,
                    partial: false,
                }
            };
            spool
                .lock()
                .map_err(|_| python_error(invalid("结果锁失败")))?
                .outcome = Some(outcome.clone());
            json!({"outcome":outcome,"production_executed":false})
        }
    };
    Ok(RunResult {
        spool: Some(spool),
        report,
    })
}
#[pyfunction]
pub fn strategy_error_json(py: Python<'_>, error: Bound<'_, PyAny>) -> PyResult<String> {
    convert(encode(&exception(py, &PyErr::from_value(error))))
}
pub fn register(m: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    m.add_class::<CancellationToken>()?;
    m.add_class::<CallbackBridge>()?;
    m.add_class::<RunResult>()?;
    m.add_function(wrap_pyfunction!(run_strategy, m)?)?;
    m.add_function(wrap_pyfunction!(strategy_error_json, m)?)?;
    Ok(())
}

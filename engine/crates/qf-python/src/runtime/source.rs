use super::spec::{Source, invalid};
use qf_core::clock::{EventSource, Visibility};
use qf_core::data::arrow::{ArrowEventSource, BatchSource};
use qf_core::data::ipc::IpcGateway;
use qf_core::data::{
    DataBatch, DataGateway, DataRequest, DependencyCheck, DependencyContext, RunScope,
};
use qf_core::engine::{StrategyDataPort, VisibleFrame};
use qf_core::types::{EventKey, MarketEvent, Nanoseconds, SecurityKey};
use qf_core::{ErrorCode, QfError, QfResult};
use serde_json::json;
use socket2::{Domain, SockAddr, Socket, Type};
use std::collections::{BTreeMap, VecDeque};
use std::os::unix::net::UnixStream;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

pub type Gateway = Arc<Mutex<IpcGateway>>;
fn connect_socket(path: &str, cancelled: &AtomicBool, deadline: Instant) -> QfResult<UnixStream> {
    let unavailable = || {
        QfError::new(
            ErrorCode::CapabilityUnavailable,
            "data_ipc",
            "已授权数据端点不可用",
        )
    };
    let address = SockAddr::unix(path).map_err(|_| unavailable())?;
    loop {
        if cancelled.load(Ordering::Acquire) {
            return Err(QfError::new(
                ErrorCode::Cancelled,
                "data_ipc",
                "数据建连已取消",
            ));
        }
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or_else(|| {
                QfError::new(ErrorCode::ResourceLimit, "data_ipc", "数据建连超过墙钟预算")
            })?;
        let socket = Socket::new(Domain::UNIX, Type::STREAM, None).map_err(|_| unavailable())?;
        socket.set_nonblocking(true).map_err(|_| unavailable())?;
        match socket.connect(&address) {
            Ok(()) => {
                socket.set_nonblocking(false).map_err(|_| unavailable())?;
                return Ok(UnixStream::from(std::os::fd::OwnedFd::from(socket)));
            }
            // Linux AF_UNIX backlog exhaustion returns EAGAIN. Each retry
            // owns and drops its socket; no abandoned connecting thread.
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                ) =>
            {
                drop(socket);
                std::thread::sleep(remaining.min(Duration::from_millis(10)));
            }
            Err(_) => return Err(unavailable()),
        }
    }
}
pub struct SharedBatches {
    gateway: Gateway,
    context: DependencyContext,
    binding: String,
    request: DataRequest,
    id: Option<String>,
    credit: Option<usize>,
    eof: bool,
    budget: usize,
}
impl BatchSource for SharedBatches {
    fn next_batch(&mut self, max_rows: usize) -> QfResult<Option<DataBatch>> {
        if self.eof {
            return Ok(None);
        }
        if max_rows == 0 || max_rows > 10_000 || self.credit.is_some_and(|n| max_rows < n) {
            return Err(invalid("事件源信用无效"));
        }
        let mut gateway = self.gateway.lock().map_err(|_| invalid("数据通道锁失败"))?;
        if self.id.is_none() {
            let (_, limits, _) = gateway.callback_request("check", json!({}))?;
            let rows = limits["limits"]["batch_rows"]
                .as_u64()
                .filter(|n| *n > 0 && *n <= 10_000)
                .ok_or_else(|| invalid("事件源行数预算无效"))? as usize;
            let credit = max_rows.min(rows);
            let (status, body, _) = gateway.callback_request(
                "open_stream",
                json!({"binding":self.binding,"request":self.request,"max_rows":credit}),
            )?;
            if status != "ok" {
                return Err(invalid("无法打开行情流"));
            }
            let id = body["stream_id"]
                .as_str()
                .ok_or_else(|| invalid("行情流标识无效"))?;
            super::label(id)?;
            self.id = Some(id.into());
            self.credit = Some(credit);
        }
        gateway.check(&self.context)?;
        let (status, metadata, payload) =
            gateway.callback_request("next", json!({"stream_id":self.id}))?;
        gateway.check(&self.context)?;
        match status.as_str() {
            "eof" if payload.is_empty() && metadata.as_object().is_some_and(|o| o.is_empty()) => {
                self.id = None;
                self.eof = true;
                Ok(None)
            }
            "batch" => Ok(Some(DataBatch::new(
                serde_json::from_value(metadata).map_err(|_| invalid("行情批次元信息无效"))?,
                payload,
                self.budget,
            )?)),
            _ => Err(invalid("行情批次状态无效")),
        }
    }
    fn close(&mut self) -> QfResult<()> {
        self.eof = true;
        if let Some(id) = self.id.take() {
            self.gateway
                .lock()
                .map_err(|_| invalid("数据通道锁失败"))?
                .callback_request("close_stream", json!({"stream_id":id}))?;
        }
        Ok(())
    }
}
impl Drop for SharedBatches {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
pub enum Events {
    Fixture(VecDeque<MarketEvent>),
    Ipc(Box<ArrowEventSource<SharedBatches>>),
}
impl EventSource for Events {
    fn next_chunk(&mut self, max_events: usize) -> QfResult<Option<Vec<MarketEvent>>> {
        match self {
            Self::Fixture(events) => {
                if events.is_empty() {
                    Ok(None)
                } else {
                    Ok(Some(events.drain(..max_events.min(events.len())).collect()))
                }
            }
            Self::Ipc(source) => source.next_chunk(max_events),
        }
    }
}
pub fn connect(
    source: Source,
    scope: RunScope,
    cancel: Arc<AtomicBool>,
    budget: usize,
    deadline: Instant,
) -> QfResult<(Events, Data)> {
    let mut data = Data {
        gateway: None,
        context: None,
        current: BTreeMap::new(),
        benchmark_origin: None,
        benchmark_security: None,
    };
    let events = match source {
        Source::Fixture { events } => {
            if events.len() > 10_000 {
                return Err(invalid("内存夹具超限"));
            }
            Events::Fixture(events.into())
        }
        Source::Ipc {
            socket_path,
            binding,
            request,
        } => {
            // Only trusted worker wiring accepts an endpoint. Public strategy
            // APIs never accept a path or obtain this spec/socket.
            if socket_path.len() > 4096 || request.securities != scope.universe {
                return Err(invalid("事件源范围与run不一致"));
            }
            let socket = connect_socket(&socket_path, &cancel, deadline)?;
            let mut gateway = IpcGateway::new(
                socket,
                scope.clone(),
                budget,
                Duration::from_secs(30),
                cancel,
            )?
            .with_run_deadline(deadline);
            let context = gateway.open(&scope)?;
            let gateway = Arc::new(Mutex::new(gateway));
            let batches = SharedBatches {
                gateway: gateway.clone(),
                context: context.clone(),
                binding,
                request,
                id: None,
                credit: None,
                eof: false,
                budget,
            };
            data.gateway = Some(gateway);
            data.context = Some(context);
            Events::Ipc(Box::new(ArrowEventSource::new(batches, budget)?))
        }
    };
    Ok((events, data))
}
pub struct Frame;
impl VisibleFrame for Frame {
    fn latest_knowledge_ns(&self) -> Option<Nanoseconds> {
        None
    }
    fn latest_market_key(&self) -> Option<&EventKey> {
        None
    }
}
pub struct Data {
    pub gateway: Option<Gateway>,
    pub context: Option<DependencyContext>,
    current: BTreeMap<SecurityKey, MarketEvent>,
    pub benchmark_origin: Option<qf_core::analysis::BenchmarkPoint>,
    pub benchmark_security: Option<String>,
}
impl StrategyDataPort for Data {
    type Frame = Frame;
    fn read(&mut self, _request: &DataRequest, _visibility: &Visibility) -> QfResult<Frame> {
        Err(QfError::new(
            ErrorCode::CapabilityUnavailable,
            "research_data",
            "研究请求须经D05回调视图与已授权D04数据通道",
        ))
    }
    fn publish(&mut self, event: &MarketEvent) -> QfResult<()> {
        self.current.insert(event.key().security, event.clone());
        Ok(())
    }
    fn benchmark(
        &self,
        security: &SecurityKey,
        at: Nanoseconds,
    ) -> QfResult<Option<qf_core::analysis::BenchmarkPoint>> {
        if self.benchmark_security.as_deref() != Some(security.as_str()) {
            return Ok(None);
        }
        if let Some(origin) = &self.benchmark_origin
            && origin.time_ns == at
        {
            return Ok(Some(origin.clone()));
        }
        let value = self.current.get(security).and_then(|event| match event {
            MarketEvent::Bar(bar) if bar.interval_end_ns == at => Some(bar.close.get()),
            MarketEvent::TradeTick(tick) if tick.time_ns == at => Some(tick.price.get()),
            _ => None,
        });
        Ok(value.map(|value| qf_core::analysis::BenchmarkPoint {
            time_ns: at,
            value: Some(value),
            unavailable_reason: None,
        }))
    }
    fn current_data(&self, securities: &[SecurityKey]) -> QfResult<Vec<MarketEvent>> {
        Ok(securities
            .iter()
            .filter_map(|s| self.current.get(s).cloned())
            .collect())
    }
    fn check(&mut self) -> QfResult<DependencyCheck> {
        // The D04 source checks around every bounded read; explicit queries
        // check before/after reads, and final_check runs before success. Never
        // request parent IPC once per tick solely to enter a Python callback.
        Ok(DependencyCheck::Unchanged)
    }
    fn close(&mut self) -> QfResult<()> {
        self.current.clear();
        if let (Some(gateway), Some(context)) = (&self.gateway, self.context.take()) {
            gateway
                .lock()
                .map_err(|_| invalid("数据通道锁失败"))?
                .close(context)?;
        }
        Ok(())
    }
}

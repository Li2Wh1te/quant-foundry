//! qf.ipc.v1 counterpart of quantfoundry._transport. One credit/one payload.
use super::arrow::BatchSource;
use super::{
    BatchMetadata, DataBatch, DataGateway, DataRequest, DependencyCheck, DependencyContext,
    MAX_ARROW_BATCH_BYTES, RunScope,
};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Operation {
    OpenStream,
    Next,
    Check,
    CloseStream,
    Cancel,
    Close,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Status {
    Request,
    Ok,
    Batch,
    Eof,
    Error,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Header {
    protocol: String,
    run_id: String,
    request_id: u64,
    op: Operation,
    status: Status,
    body: Value,
    payload_bytes: usize,
}
fn invalid() -> QfError {
    QfError::new(
        ErrorCode::InvalidContract,
        "data_ipc",
        "私有数据通道契约无效",
    )
}
fn limit() -> QfError {
    QfError::new(
        ErrorCode::ResourceLimit,
        "data_ipc",
        "数据通道超过长度或等待预算",
    )
}
fn disconnected() -> QfError {
    QfError::new(ErrorCode::Cancelled, "data_ipc", "数据通道已取消或断开")
}

pub struct IpcGateway {
    socket: UnixStream,
    scope: RunScope,
    sequence: u64,
    budget: usize,
    timeout: Duration,
    cancelled: Arc<AtomicBool>,
    failure: Option<QfError>,
    closed: bool,
    binding: Option<String>,
    row_budget: usize,
}
impl IpcGateway {
    /// Connected socket ONLY. D12 binds run, ownership, container and endpoint.
    pub fn new(
        socket: UnixStream,
        scope: RunScope,
        budget: usize,
        timeout: Duration,
        cancelled: Arc<AtomicBool>,
    ) -> QfResult<Self> {
        crate::types::keys::label(&scope.run_id, 128)?;
        if budget == 0
            || budget > MAX_ARROW_BATCH_BYTES
            || timeout.is_zero()
            || timeout > Duration::from_secs(3600)
            || scope.universe.len() > 10000
        {
            return Err(limit());
        }
        socket
            .set_read_timeout(Some(Duration::from_millis(50)))
            .map_err(|_| disconnected())?;
        socket
            .set_write_timeout(Some(Duration::from_millis(50)))
            .map_err(|_| disconnected())?;
        Ok(Self {
            socket,
            scope,
            sequence: 0,
            budget,
            timeout,
            cancelled,
            failure: None,
            closed: false,
            binding: None,
            row_budget: 1024,
        })
    }
    pub fn with_binding(mut self, binding: &str) -> QfResult<Self> {
        crate::types::keys::label(binding, 128)?;
        self.binding = Some(binding.into());
        Ok(self)
    }
    fn checkpoint(&self, deadline: Instant) -> QfResult<()> {
        if self.closed || self.cancelled.load(Ordering::Relaxed) {
            return Err(disconnected());
        }
        if Instant::now() >= deadline {
            return Err(limit());
        }
        Ok(())
    }
    fn send_all(&mut self, bytes: &[u8], deadline: Instant) -> QfResult<()> {
        let mut position = 0;
        while position < bytes.len() {
            self.checkpoint(deadline)?;
            let end = (position + 65536).min(bytes.len());
            match self.socket.write(&bytes[position..end]) {
                Ok(0) => return Err(disconnected()),
                Ok(n) => position += n,
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock
                            | std::io::ErrorKind::TimedOut
                            | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => return Err(disconnected()),
            }
        }
        Ok(())
    }
    fn receive_exact(&mut self, bytes: &mut [u8], deadline: Instant) -> QfResult<()> {
        let mut position = 0;
        while position < bytes.len() {
            self.checkpoint(deadline)?;
            let end = (position + 65536).min(bytes.len());
            match self.socket.read(&mut bytes[position..end]) {
                Ok(0) => return Err(disconnected()),
                Ok(n) => position += n,
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock
                            | std::io::ErrorKind::TimedOut
                            | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => return Err(disconnected()),
            }
        }
        Ok(())
    }
    fn exchange(&mut self, op: Operation, body: Value) -> QfResult<(Header, Vec<u8>)> {
        let deadline = Instant::now() + self.timeout;
        self.checkpoint(deadline)?;
        self.sequence = self
            .sequence
            .checked_add(1)
            .filter(|n| *n < 1_u64 << 53)
            .ok_or_else(limit)?;
        let request = Header {
            protocol: "qf.ipc.v1".into(),
            run_id: self.scope.run_id.clone(),
            request_id: self.sequence,
            op,
            status: Status::Request,
            body,
            payload_bytes: 0,
        };
        let encoded = serde_json::to_vec(&request).map_err(|_| invalid())?;
        if encoded.len() > crate::run::MAX_CONTROL_BYTES {
            return Err(limit());
        }
        let mut prefix = [0_u8; 16];
        prefix[..4].copy_from_slice(b"QFD4");
        prefix[4..8].copy_from_slice(&(encoded.len() as u32).to_be_bytes());
        self.send_all(&prefix, deadline)?;
        self.send_all(&encoded, deadline)?;
        self.receive_exact(&mut prefix, deadline)?;
        if &prefix[..4] != b"QFD4" {
            return Err(invalid());
        }
        let head = u32::from_be_bytes(prefix[4..8].try_into().map_err(|_| invalid())?) as usize;
        let payload = u64::from_be_bytes(prefix[8..].try_into().map_err(|_| invalid())?);
        if head == 0 || head > crate::run::MAX_CONTROL_BYTES || payload > self.budget as u64 {
            return Err(limit());
        }
        let mut header_bytes = vec![0; head];
        self.receive_exact(&mut header_bytes, deadline)?;
        let response: Header = serde_json::from_slice(&header_bytes).map_err(|_| invalid())?;
        if response.protocol != "qf.ipc.v1"
            || response.run_id != self.scope.run_id
            || response.request_id != self.sequence
            || response.op != op
            || response.payload_bytes as u64 != payload
            || response.status == Status::Request
            || !response.body.is_object()
            || (payload != 0 && response.status != Status::Batch)
        {
            return Err(invalid());
        }
        let mut bytes = vec![0; payload as usize];
        self.receive_exact(&mut bytes, deadline)?;
        if response.status == Status::Error {
            let error: QfError = serde_json::from_value(response.body).map_err(|_| invalid())?;
            return Err(error);
        }
        Ok((response, bytes))
    }
    fn call(&mut self, op: Operation, body: Value) -> QfResult<(Header, Vec<u8>)> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        let result = self.exchange(op, body);
        if let Err(error) = &result {
            self.failure = Some(error.clone());
            self.disconnect();
        }
        result
    }
    fn disconnect(&mut self) {
        self.closed = true;
        let _ = self.socket.shutdown(std::net::Shutdown::Both);
    }
    pub fn stream<'a>(
        &'a mut self,
        binding: &str,
        request: &DataRequest,
    ) -> QfResult<RemoteStream<'a>> {
        crate::types::keys::label(binding, 128)?;
        request.validate(request.end_ns)?;
        if request
            .securities
            .iter()
            .any(|s| !self.scope.universe.contains(s))
        {
            return Err(invalid());
        }
        Ok(RemoteStream {
            gateway: self,
            binding: binding.into(),
            request: request.clone(),
            id: None,
            credit: None,
            eof: false,
        })
    }
    pub fn cancel(&mut self) -> QfResult<()> {
        let result = self.call(Operation::Cancel, json!({})).map(|_| ());
        self.disconnect();
        result
    }
}
impl Drop for IpcGateway {
    fn drop(&mut self) {
        self.disconnect();
    }
}

impl DataGateway for IpcGateway {
    fn open(&mut self, scope: &RunScope) -> QfResult<DependencyContext> {
        if scope != &self.scope {
            return Err(invalid());
        }
        let (response, _) = self.call(Operation::Check, json!({}))?;
        if response.status != Status::Ok {
            return Err(invalid());
        }
        self.row_budget = response
            .body
            .get("limits")
            .and_then(|l| l.get("batch_rows"))
            .and_then(Value::as_u64)
            .filter(|n| *n > 0 && *n <= 10000)
            .ok_or_else(invalid)? as usize;
        serde_json::from_value(response.body.get("context").cloned().ok_or_else(invalid)?)
            .map_err(|_| invalid())
    }
    /// D01 single-batch method refuses a multi-batch request rather than hiding
    /// its remainder. Engine/research streams use `stream`/BatchSource.
    fn read(
        &mut self,
        request: &DataRequest,
        context: &mut DependencyContext,
    ) -> QfResult<DataBatch> {
        self.check(context)?;
        let binding = self.binding.clone().ok_or_else(|| {
            QfError::new(
                ErrorCode::CapabilityUnavailable,
                "data_read",
                "必须明确选择已授权的数据绑定",
            )
        })?;
        let batch = {
            let mut source = self.stream(&binding, request)?;
            let first = source.next_batch(1024)?.ok_or_else(invalid)?;
            if source.next_batch(1024)?.is_some() {
                return Err(limit());
            }
            first
        };
        *context = self.open(&self.scope.clone())?;
        Ok(batch)
    }
    fn check(&mut self, context: &DependencyContext) -> QfResult<DependencyCheck> {
        let (response, _) = self.call(Operation::Check, json!({}))?;
        let actual: DependencyContext =
            serde_json::from_value(response.body.get("context").cloned().ok_or_else(invalid)?)
                .map_err(|_| invalid())?;
        if response.status != Status::Ok
            || actual.scope_id != context.scope_id
            || response.body.get("check").and_then(Value::as_str) != Some("unchanged")
        {
            return Err(invalid());
        }
        Ok(DependencyCheck::Unchanged)
    }
    fn close(&mut self, _context: DependencyContext) -> QfResult<()> {
        if self.closed {
            return Ok(());
        }
        let result = self.call(Operation::Close, json!({})).map(|_| ());
        self.disconnect();
        result
    }
}
pub struct RemoteStream<'a> {
    gateway: &'a mut IpcGateway,
    binding: String,
    request: DataRequest,
    id: Option<String>,
    credit: Option<usize>,
    eof: bool,
}
impl BatchSource for RemoteStream<'_> {
    fn next_batch(&mut self, max_rows: usize) -> QfResult<Option<DataBatch>> {
        if self.eof {
            return Ok(None);
        }
        if max_rows == 0 || max_rows > 10000 || self.credit.is_some_and(|c| max_rows < c) {
            return Err(limit());
        }
        if self.id.is_none() {
            let (response, _) = self.gateway.call(
                Operation::OpenStream,
                json!({"binding":self.binding,
                "request":self.request,"max_rows":max_rows.min(self.gateway.row_budget)}),
            )?;
            if response.status != Status::Ok {
                return Err(invalid());
            }
            let id = response
                .body
                .get("stream_id")
                .and_then(Value::as_str)
                .ok_or_else(invalid)?;
            crate::types::keys::label(id, 128)?;
            self.id = Some(id.into());
            self.credit = Some(max_rows.min(self.gateway.row_budget));
        }
        let (response, payload) = self
            .gateway
            .call(Operation::Next, json!({"stream_id":self.id}))?;
        match response.status {
            Status::Eof
                if payload.is_empty()
                    && response.body.as_object().is_some_and(|v| v.is_empty()) =>
            {
                self.eof = true;
                self.id = None;
                Ok(None)
            }
            Status::Batch => {
                let metadata: BatchMetadata =
                    serde_json::from_value(response.body).map_err(|_| invalid())?;
                if metadata.rows > max_rows as u64 {
                    return Err(limit());
                }
                Ok(Some(DataBatch::new(
                    metadata,
                    payload,
                    self.gateway.budget,
                )?))
            }
            _ => Err(invalid()),
        }
    }
    fn close(&mut self) -> QfResult<()> {
        self.eof = true;
        if let Some(id) = self.id.take() {
            self.gateway
                .call(Operation::CloseStream, json!({"stream_id":id}))?;
        }
        Ok(())
    }
}
impl Drop for RemoteStream<'_> {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

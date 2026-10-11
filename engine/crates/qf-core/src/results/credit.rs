//! One-slot credit handshake for a trusted IPC writer; no unbounded queue.
use super::SinkCredit;
use crate::{ErrorCode, QfError, QfResult};
use std::sync::{
    Condvar, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};
#[derive(Default)]
pub struct CreditGate {
    state: Mutex<State>,
    ready: Condvar,
}
#[derive(Default)]
struct State {
    credit: Option<SinkCredit>,
    closed: bool,
}
fn failure(code: ErrorCode) -> QfError {
    QfError::new(code, "result_credit", "结果通道已关闭、取消或信用无效")
}
impl CreditGate {
    pub fn grant(&self, credit: SinkCredit) -> QfResult<()> {
        if credit.max_records == 0
            || credit.max_records > super::MAX_PAGE_ITEMS
            || credit.max_bytes == 0
            || credit.max_bytes > crate::run::MAX_CONTROL_BYTES
        {
            return Err(failure(ErrorCode::InvalidContract));
        }
        let mut state = self
            .state
            .lock()
            .map_err(|_| failure(ErrorCode::InvalidContract))?;
        if state.closed || state.credit.is_some() {
            return Err(failure(ErrorCode::InvalidContract));
        }
        state.credit = Some(credit);
        self.ready.notify_all();
        Ok(())
    }
    pub fn wait(&self, cancelled: &AtomicBool, deadline: Instant) -> QfResult<SinkCredit> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| failure(ErrorCode::InvalidContract))?;
        loop {
            if state.closed || cancelled.load(Ordering::Acquire) {
                return Err(failure(ErrorCode::Cancelled));
            }
            if Instant::now() >= deadline {
                return Err(failure(ErrorCode::ResourceLimit));
            }
            if let Some(credit) = state.credit.take() {
                return Ok(credit);
            }
            let delay = deadline
                .saturating_duration_since(Instant::now())
                .min(Duration::from_millis(20));
            state = self
                .ready
                .wait_timeout(state, delay)
                .map_err(|_| failure(ErrorCode::InvalidContract))?
                .0;
        }
    }
    pub fn close(&self) {
        if let Ok(mut s) = self.state.lock() {
            s.closed = true;
            s.credit = None;
        }
        self.ready.notify_all();
    }
}

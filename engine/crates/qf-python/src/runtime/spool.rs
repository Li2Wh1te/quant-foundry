//! D10 callback pages reuse the D11 format; no second all-history store.
pub use qf_core::results::file::public_kind;
use qf_core::results::file::{FileLimits, FileResultSink};
use qf_core::results::{ResultBatch, ResultSink, SinkCredit};
use qf_core::run::RunOutcome;
use qf_core::{ErrorCode, QfError, QfResult};
use serde_json::Value;
use std::sync::{Arc, Mutex, atomic::AtomicBool};
fn failure() -> QfError {
    QfError::new(
        ErrorCode::ResultBudgetExceeded,
        "result_spool",
        "结果写入或读取失败",
    )
}
pub struct Spool {
    inner: FileResultSink,
}
impl Spool {
    pub fn new(run_id: &str, maximum: u64, cancelled: Arc<AtomicBool>) -> QfResult<Self> {
        Ok(Self {
            inner: FileResultSink::create(
                &std::env::temp_dir(),
                run_id,
                FileLimits {
                    total_bytes: maximum,
                    ..FileLimits::default()
                },
                cancelled,
            )?,
        })
    }
    pub fn page(&mut self, name: &str, after: Option<&str>, limit: usize) -> QfResult<Value> {
        self.inner.page(name, after, limit)
    }
    pub fn note_dropped_log(&mut self) {
        self.inner.note_dropped_log();
    }
    pub fn summary(&self) -> Value {
        self.inner.overview()
    }
    pub fn fail(&mut self, error: &QfError) -> QfResult<()> {
        self.inner.abort(error)
    }
}
pub type SharedSpool = Arc<Mutex<Spool>>;
pub struct Writer(pub SharedSpool);
impl Writer {
    pub fn finalize_guarded<G>(
        &mut self,
        outcome: &RunOutcome,
        acquire: impl FnOnce() -> QfResult<G>,
    ) -> QfResult<()> {
        self.0
            .lock()
            .map_err(|_| failure())?
            .inner
            .finalize_guarded(outcome, acquire)
    }
}
impl ResultSink for Writer {
    fn credit(&mut self) -> QfResult<SinkCredit> {
        self.0.lock().map_err(|_| failure())?.inner.credit()
    }
    fn write(&mut self, batch: &ResultBatch) -> QfResult<()> {
        self.0.lock().map_err(|_| failure())?.inner.write(batch)
    }
    fn finalize(&mut self, outcome: &RunOutcome) -> QfResult<()> {
        self.0
            .lock()
            .map_err(|_| failure())?
            .inner
            .finalize(outcome)
    }
    fn abort(&mut self, error: &QfError) -> QfResult<()> {
        self.0.lock().map_err(|_| failure())?.inner.abort(error)
    }
}

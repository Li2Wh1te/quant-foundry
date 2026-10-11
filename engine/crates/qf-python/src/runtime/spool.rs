//! Bounded business results on private temporary files, never market history.
use qf_core::results::{ResultBatch, ResultKind, ResultRecord, ResultSink, SinkCredit};
use qf_core::run::{MAX_CONTROL_BYTES, RunOutcome};
use qf_core::{ErrorCode, QfError, QfResult};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU64, Ordering},
};

static NONCE: AtomicU64 = AtomicU64::new(1);
fn failure() -> QfError {
    QfError::new(
        ErrorCode::ResultBudgetExceeded,
        "result_spool",
        "结果写入、读取或磁盘预算失败",
    )
}
fn kind(record: &ResultRecord) -> &'static str {
    match record {
        ResultRecord::Order(_) => "orders",
        ResultRecord::Trade(_) => "trades",
        ResultRecord::Record(_) => "records",
        ResultRecord::Log(_) => "logs",
        ResultRecord::Equity(_) => "equity",
        ResultRecord::Position(_) => "positions",
        ResultRecord::CorporateAction(_) | ResultRecord::ExecutionModel(_) => "metadata",
    }
}
struct Partition {
    data: File,
    index: File,
    count: u64,
}
pub struct Spool {
    directory: PathBuf,
    token: String,
    partitions: BTreeMap<String, Partition>,
    bytes: u64,
    maximum: u64,
    next_sequence: u64,
    pub outcome: Option<RunOutcome>,
    pub max_page_read: usize,
}
impl Spool {
    pub fn new(run_id: &str, maximum: u64) -> QfResult<Self> {
        if maximum == 0 || maximum > 8 * 1024 * 1024 * 1024 {
            return Err(failure());
        }
        let nonce = NONCE.fetch_add(1, Ordering::Relaxed);
        let token = format!("{}-{nonce}-{run_id}", std::process::id());
        let directory = std::env::temp_dir().join(format!("qf-d10-{}-{nonce}", std::process::id()));
        std::fs::create_dir(&directory).map_err(|_| failure())?;
        Ok(Self {
            directory,
            token,
            partitions: BTreeMap::new(),
            bytes: 0,
            maximum,
            next_sequence: 1,
            outcome: None,
            max_page_read: 0,
        })
    }
    fn partition(&mut self, name: &str) -> QfResult<&mut Partition> {
        if !self.partitions.contains_key(name) {
            let open = |suffix| {
                OpenOptions::new()
                    .create_new(true)
                    .read(true)
                    .write(true)
                    .open(self.directory.join(format!("{name}.{suffix}")))
                    .map_err(|_| failure())
            };
            self.partitions.insert(
                name.into(),
                Partition {
                    data: open("data")?,
                    index: open("index")?,
                    count: 0,
                },
            );
        }
        self.partitions.get_mut(name).ok_or_else(failure)
    }
    pub fn page(&mut self, name: &str, after: Option<&str>, limit: usize) -> QfResult<Value> {
        if limit == 0 || limit > qf_core::results::MAX_PAGE_ITEMS {
            return Err(failure());
        }
        let start = if let Some(cursor) = after {
            if cursor.len() > qf_core::results::MAX_CURSOR_BYTES {
                return Err(failure());
            }
            let (token, suffix) = cursor.rsplit_once(':').ok_or_else(failure)?;
            let n: u64 = suffix.parse().map_err(|_| failure())?;
            if token != self.token || suffix != n.to_string() {
                return Err(failure());
            }
            n
        } else {
            0
        };
        let count = self.partitions.get(name).map_or(0, |p| p.count);
        if start > count {
            return Err(failure());
        }
        let end = start.saturating_add(limit as u64).min(count);
        let mut items = Vec::with_capacity((end - start) as usize);
        let mut encoded_bytes = 0;
        if end > start {
            let partition = self.partitions.get_mut(name).ok_or_else(failure)?;
            partition
                .index
                .seek(SeekFrom::Start(start.checked_mul(8).ok_or_else(failure)?))
                .map_err(|_| failure())?;
            for _ in start..end {
                let mut word = [0_u8; 8];
                partition
                    .index
                    .read_exact(&mut word)
                    .map_err(|_| failure())?;
                partition
                    .data
                    .seek(SeekFrom::Start(u64::from_le_bytes(word)))
                    .map_err(|_| failure())?;
                let mut length = [0_u8; 4];
                partition
                    .data
                    .read_exact(&mut length)
                    .map_err(|_| failure())?;
                let size = u32::from_le_bytes(length) as usize;
                if size > MAX_CONTROL_BYTES || encoded_bytes + size > MAX_CONTROL_BYTES - 8192 {
                    if items.is_empty() {
                        return Err(failure());
                    }
                    break;
                }
                let mut buffer = vec![0; size];
                partition
                    .data
                    .read_exact(&mut buffer)
                    .map_err(|_| failure())?;
                items.push(serde_json::from_slice::<Value>(&buffer).map_err(|_| failure())?);
                encoded_bytes += size;
            }
        }
        self.max_page_read = self.max_page_read.max(items.len());
        let next = start + items.len() as u64;
        Ok(
            json!({ "items": items, "next_cursor": (next < count).then(|| format!("{}:{next}", self.token)) }),
        )
    }
    pub fn summary(&self) -> Value {
        json!({"outcome":self.outcome,"counts":self.partitions.iter().map(|(k,p)|(k,p.count)).collect::<BTreeMap<_,_>>(),
            "result_bytes":self.bytes,"max_page_read":self.max_page_read})
    }
    fn write(&mut self, batch: &ResultBatch) -> QfResult<()> {
        if batch.first_sequence().get() != self.next_sequence || self.outcome.is_some() {
            return Err(failure());
        }
        for record in batch.records() {
            let name = kind(record);
            let envelope = serde_json::to_value(record).map_err(|_| failure())?;
            let buffer = serde_json::to_vec(&envelope["record"]).map_err(|_| failure())?;
            let size = buffer.len() as u64 + 12;
            if buffer.len() > MAX_CONTROL_BYTES
                || self
                    .bytes
                    .checked_add(size)
                    .is_none_or(|n| n > self.maximum)
            {
                return Err(failure());
            }
            let partition = self.partition(name)?;
            let offset = partition
                .data
                .seek(SeekFrom::End(0))
                .map_err(|_| failure())?;
            partition
                .data
                .write_all(&(buffer.len() as u32).to_le_bytes())
                .map_err(|_| failure())?;
            partition.data.write_all(&buffer).map_err(|_| failure())?;
            partition
                .index
                .seek(SeekFrom::End(0))
                .map_err(|_| failure())?;
            partition
                .index
                .write_all(&offset.to_le_bytes())
                .map_err(|_| failure())?;
            partition.count += 1;
            self.bytes += size;
            self.next_sequence += 1;
        }
        Ok(())
    }
}
impl Drop for Spool {
    fn drop(&mut self) {
        self.partitions.clear();
        for name in [
            "orders",
            "trades",
            "records",
            "logs",
            "equity",
            "positions",
            "metadata",
        ] {
            for suffix in ["data", "index"] {
                let _ = std::fs::remove_file(self.directory.join(format!("{name}.{suffix}")));
            }
        }
        let _ = std::fs::remove_dir(&self.directory);
    }
}
pub type SharedSpool = Arc<Mutex<Spool>>;
pub struct Writer(pub SharedSpool);
impl ResultSink for Writer {
    fn credit(&mut self) -> QfResult<SinkCredit> {
        Ok(SinkCredit {
            max_records: 1024,
            max_bytes: MAX_CONTROL_BYTES,
        })
    }
    fn write(&mut self, batch: &ResultBatch) -> QfResult<()> {
        self.0.lock().map_err(|_| failure())?.write(batch)
    }
    fn finalize(&mut self, outcome: &RunOutcome) -> QfResult<()> {
        self.0.lock().map_err(|_| failure())?.outcome = Some(outcome.clone());
        Ok(())
    }
    fn abort(&mut self, error: &QfError) -> QfResult<()> {
        self.0.lock().map_err(|_| failure())?.outcome =
            Some(if error.code == ErrorCode::Cancelled {
                RunOutcome::Cancelled { partial: true }
            } else {
                RunOutcome::Failed {
                    error: error.clone(),
                    partial: true,
                }
            });
        Ok(())
    }
}
pub fn public_kind(kind: ResultKind) -> Option<&'static str> {
    Some(match kind {
        ResultKind::Summary => return None,
        ResultKind::Orders => "orders",
        ResultKind::Trades => "trades",
        ResultKind::Positions => "positions",
        ResultKind::Equity => "equity",
        ResultKind::Records => "records",
        ResultKind::Logs => "logs",
    })
}

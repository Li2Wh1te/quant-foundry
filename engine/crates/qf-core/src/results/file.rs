//! Shared D11 file format for trusted D12/D13 writers. No strategy-selected path.
//! Directory-relative descriptors reject symlinks and remain anchored on rename.
use super::{ResultBatch, ResultKind, ResultRecord, ResultSink, SinkCredit, encode_bounded};
use crate::analysis::PerformanceSummary;
use crate::run::{MAX_CONTROL_BYTES, RunOutcome};
use crate::types::Sequence;
use crate::{ErrorCode, QfError, QfResult};
use rustix::fs::{self, AtFlags, Mode, OFlags};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Write;
use std::os::fd::OwnedFd;
use std::os::unix::fs::FileExt;
use std::path::{Component, Path};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
static NONCE: AtomicU64 = AtomicU64::new(1);
pub const FORMAT: &str = "qf.results.v1";
fn failure() -> QfError {
    QfError::new(
        ErrorCode::ResultBudgetExceeded,
        "result_sink",
        "结果容量、文件完整性或提交失败",
    )
}
fn invalid() -> QfError {
    QfError::new(
        ErrorCode::InvalidContract,
        "result_sink",
        "结果协议、路径或游标无效",
    )
}
#[derive(Debug, Clone)]
pub struct FileLimits {
    pub total_bytes: u64,
    pub shard_bytes: usize,
    pub max_shards: usize,
    pub max_page_bytes: usize,
    pub log_bytes: u64,
    pub log_records: u64,
    pub record_points: u64,
    pub require_summary: bool,
}
impl Default for FileLimits {
    fn default() -> Self {
        Self {
            total_bytes: 256 * 1024 * 1024,
            shard_bytes: 4 * 1024 * 1024,
            max_shards: 1024,
            max_page_bytes: MAX_CONTROL_BYTES - 8192,
            log_bytes: 1024 * 1024,
            log_records: 10_000,
            record_points: 100_000,
            require_summary: true,
        }
    }
}
impl FileLimits {
    fn validate(&self) -> QfResult<()> {
        if self.total_bytes == 0
            || self.total_bytes > 8 * 1024 * 1024 * 1024
            || self.shard_bytes < 256
            || self.shard_bytes > 64 * 1024 * 1024
            || self.max_shards == 0
            || self.max_shards > 4096
            || self.max_page_bytes < 128
            || self.max_page_bytes > MAX_CONTROL_BYTES - 8192
            || self.log_bytes > 16 * 1024 * 1024
            || self.log_records > 100_000
            || self.record_points > 1_000_000
        {
            return Err(failure());
        }
        Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShardDescriptor {
    pub kind: String,
    pub file: String,
    pub index_file: String,
    pub start_item: Sequence,
    pub count: Sequence,
    pub first_sequence: Sequence,
    pub last_sequence: Sequence,
    pub data_bytes: Sequence,
    pub index_bytes: Sequence,
    pub sha256: String,
    pub index_sha256: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputNotices {
    pub logs_dropped: Sequence,
    pub log_messages_truncated: Sequence,
    pub logs_truncated: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResultManifest {
    pub schema_id: String,
    pub run_id: String,
    pub outcome: RunOutcome,
    pub partial: bool,
    pub counts: BTreeMap<String, Sequence>,
    pub shards: Vec<ShardDescriptor>,
    pub result_bytes: Sequence,
    pub notices: OutputNotices,
    pub performance: Option<PerformanceSummary>,
}
impl Default for OutputNotices {
    fn default() -> Self {
        Self {
            logs_dropped: Sequence::new(0),
            log_messages_truncated: Sequence::new(0),
            logs_truncated: false,
        }
    }
}
struct Active {
    descriptor: ShardDescriptor,
    data: File,
    index: File,
    data_hash: Sha256,
    index_hash: Sha256,
}
fn directory(path: &Path) -> QfResult<OwnedFd> {
    if !path.is_absolute() {
        return Err(invalid());
    }
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let mut fd = fs::open("/", flags, Mode::empty()).map_err(|_| invalid())?;
    for part in path.components() {
        match part {
            Component::RootDir => (),
            Component::Normal(name) => {
                fd = fs::openat(&fd, name, flags, Mode::empty()).map_err(|_| invalid())?;
            }
            _ => return Err(invalid()),
        }
    }
    Ok(fd)
}
fn file(fd: &OwnedFd, name: &str, create: bool) -> QfResult<File> {
    if name.contains('/') || name.contains('\\') || name.is_empty() || name == "." || name == ".." {
        return Err(invalid());
    }
    let flags = OFlags::CLOEXEC
        | OFlags::NOFOLLOW
        | if create {
            OFlags::RDWR | OFlags::CREATE | OFlags::EXCL
        } else {
            OFlags::RDONLY
        };
    let fd = fs::openat(fd, name, flags, Mode::from_bits_truncate(0o600)).map_err(|_| failure())?;
    let stat = fs::fstat(&fd).map_err(|_| failure())?;
    if fs::FileType::from_raw_mode(stat.st_mode) != fs::FileType::RegularFile
        || stat.st_nlink != 1
        || stat.st_mode & 0o077 != 0
    {
        return Err(invalid());
    }
    Ok(File::from(fd))
}
fn identity(root: &OwnedFd, name: &str, file: &File) -> QfResult<()> {
    let actual = fs::fstat(file).map_err(|_| failure())?;
    let named = fs::statat(root, name, AtFlags::SYMLINK_NOFOLLOW).map_err(|_| failure())?;
    if actual.st_dev != named.st_dev
        || actual.st_ino != named.st_ino
        || actual.st_nlink != 1
        || named.st_mode & 0o077 != 0
        || fs::FileType::from_raw_mode(named.st_mode) != fs::FileType::RegularFile
    {
        return Err(invalid());
    }
    Ok(())
}
fn digest(file: &File, length: u64, cancel: &AtomicBool) -> QfResult<String> {
    if file.metadata().map_err(|_| failure())?.len() != length {
        return Err(failure());
    }
    let mut hash = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    let mut offset = 0;
    while offset < length {
        if cancel.load(Ordering::Acquire) {
            return Err(QfError::new(
                ErrorCode::Cancelled,
                "result_sink",
                "结果提交已取消",
            ));
        }
        let n = (length - offset).min(buffer.len() as u64) as usize;
        file.read_exact_at(&mut buffer[..n], offset)
            .map_err(|_| failure())?;
        hash.update(&buffer[..n]);
        offset += n as u64;
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn partition(record: &ResultRecord) -> &'static str {
    match record {
        ResultRecord::Summary(_) => "summary",
        ResultRecord::Equity(_) => "equity",
        ResultRecord::Order(_) => "orders",
        ResultRecord::Trade(_) => "trades",
        ResultRecord::Position(_) => "positions",
        ResultRecord::Record(_) => "records",
        ResultRecord::Log(_) => "logs",
        ResultRecord::ExecutionModel(_) | ResultRecord::CorporateAction(_) => "metadata",
    }
}
pub fn public_kind(kind: ResultKind) -> Option<&'static str> {
    Some(match kind {
        ResultKind::Summary => return None,
        ResultKind::Equity => "equity",
        ResultKind::Orders => "orders",
        ResultKind::Trades => "trades",
        ResultKind::Positions => "positions",
        ResultKind::Records => "records",
        ResultKind::Logs => "logs",
    })
}
pub struct FileResultSink {
    parent: OwnedFd,
    root: OwnedFd,
    name: String,
    run_id: String,
    token: String,
    limits: FileLimits,
    active: BTreeMap<String, Active>,
    shards: Vec<ShardDescriptor>,
    counts: BTreeMap<String, u64>,
    created: BTreeSet<String>,
    names: BTreeSet<String>,
    next_sequence: u64,
    bytes: u64,
    record_points: u64,
    log_bytes: u64,
    notices: OutputNotices,
    performance: Option<PerformanceSummary>,
    outcome: Option<RunOutcome>,
    fault: Option<QfError>,
    credit_issued: bool,
    cancelled: Arc<AtomicBool>,
    retain: bool,
    pub max_page_read: usize,
}
impl FileResultSink {
    /// parent is trusted configuration; name is server-generated and exclusive.
    pub fn create(
        parent: &Path,
        run_id: &str,
        limits: FileLimits,
        cancelled: Arc<AtomicBool>,
    ) -> QfResult<Self> {
        limits.validate()?;
        crate::types::keys::label(run_id, 128)?;
        let parent = directory(parent)?;
        let nonce = NONCE.fetch_add(1, Ordering::Relaxed);
        let name = format!("qf-results-{}-{nonce}", std::process::id());
        fs::mkdirat(&parent, name.as_str(), Mode::from_bits_truncate(0o700))
            .map_err(|_| failure())?;
        let root = fs::openat(
            &parent,
            name.as_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|_| failure())?;
        Ok(Self {
            parent,
            root,
            name,
            run_id: run_id.into(),
            token: format!("{}-{nonce}-{run_id}", std::process::id()),
            limits,
            active: BTreeMap::new(),
            shards: Vec::new(),
            counts: BTreeMap::new(),
            created: BTreeSet::new(),
            names: BTreeSet::new(),
            next_sequence: 1,
            bytes: 0,
            record_points: 0,
            log_bytes: 0,
            notices: OutputNotices::default(),
            performance: None,
            outcome: None,
            fault: None,
            credit_issued: false,
            cancelled,
            retain: false,
            max_page_read: 0,
        })
    }
    pub fn directory_name(&self) -> &str {
        &self.name
    }
    pub fn outcome(&self) -> Option<&RunOutcome> {
        self.outcome.as_ref()
    }
    fn check_location(&self) -> QfResult<()> {
        let actual = fs::fstat(&self.root).map_err(|_| invalid())?;
        let named = fs::statat(&self.parent, self.name.as_str(), AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|_| invalid())?;
        if actual.st_dev != named.st_dev
            || actual.st_ino != named.st_ino
            || fs::FileType::from_raw_mode(named.st_mode) != fs::FileType::Directory
            || named.st_mode & 0o077 != 0
        {
            return Err(invalid());
        }
        Ok(())
    }
    fn check(&self) -> QfResult<()> {
        self.check_location()?;
        if let Some(e) = &self.fault {
            return Err(e.clone());
        }
        if self.cancelled.load(Ordering::Acquire) {
            return Err(QfError::new(
                ErrorCode::Cancelled,
                "result_sink",
                "运行已取消",
            ));
        }
        if self.outcome.is_some() {
            return Err(invalid());
        }
        Ok(())
    }
    fn seal(&mut self, kind: &str) -> QfResult<()> {
        let Some(mut active) = self.active.remove(kind) else {
            return Ok(());
        };
        identity(&self.root, &active.descriptor.file, &active.data)?;
        identity(&self.root, &active.descriptor.index_file, &active.index)?;
        active.data.sync_all().map_err(|_| failure())?;
        active.index.sync_all().map_err(|_| failure())?;
        active.descriptor.sha256 = format!("{:x}", active.data_hash.finalize());
        active.descriptor.index_sha256 = format!("{:x}", active.index_hash.finalize());
        if digest(
            &active.data,
            active.descriptor.data_bytes.get(),
            &self.cancelled,
        )? != active.descriptor.sha256
            || digest(
                &active.index,
                active.descriptor.index_bytes.get(),
                &self.cancelled,
            )? != active.descriptor.index_sha256
        {
            return Err(failure());
        }
        self.shards.push(active.descriptor);
        Ok(())
    }
    fn ensure_active(&mut self, kind: &str, size: usize) -> QfResult<()> {
        if size + 20 > self.limits.shard_bytes {
            return Err(failure());
        }
        if self.active.get(kind).is_some_and(|a| {
            a.descriptor.data_bytes.get() + a.descriptor.index_bytes.get() + size as u64 + 20
                > self.limits.shard_bytes as u64
        }) {
            self.seal(kind)?;
        }
        if !self.active.contains_key(kind) {
            if self.shards.len() + self.active.len() >= self.limits.max_shards {
                return Err(failure());
            }
            let id = self.created.len() / 2;
            let name = format!("{kind}-{id:06}");
            let data_name = format!("{name}.data");
            let index_name = format!("{name}.index");
            let data = file(&self.root, &data_name, true)?;
            self.created.insert(data_name.clone());
            let index = file(&self.root, &index_name, true)?;
            self.created.insert(index_name.clone());
            self.active.insert(
                kind.into(),
                Active {
                    descriptor: ShardDescriptor {
                        kind: kind.into(),
                        file: data_name,
                        index_file: index_name,
                        start_item: Sequence::new(*self.counts.get(kind).unwrap_or(&0)),
                        count: Sequence::new(0),
                        first_sequence: Sequence::new(self.next_sequence),
                        last_sequence: Sequence::new(self.next_sequence),
                        data_bytes: Sequence::new(0),
                        index_bytes: Sequence::new(0),
                        sha256: String::new(),
                        index_sha256: String::new(),
                    },
                    data,
                    index,
                    data_hash: Sha256::new(),
                    index_hash: Sha256::new(),
                },
            );
        }
        Ok(())
    }
    fn normalize(&mut self, record: &ResultRecord) -> QfResult<Option<ResultRecord>> {
        match record {
            ResultRecord::Log(row) => {
                let count = *self.counts.get("logs").unwrap_or(&0);
                if count >= self.limits.log_records || self.log_bytes >= self.limits.log_bytes {
                    self.notices.logs_dropped =
                        Sequence::new(self.notices.logs_dropped.get().saturating_add(1));
                    self.notices.logs_truncated = true;
                    return Ok(None);
                }
                let mut row = row.clone();
                let limit = 4096.min((self.limits.log_bytes - self.log_bytes) as usize);
                if row.message.len() > limit {
                    let mut end = limit;
                    while !row.message.is_char_boundary(end) {
                        end -= 1;
                    }
                    row.message.truncate(end);
                    row.truncated = true;
                }
                if row.truncated {
                    self.notices.log_messages_truncated =
                        Sequence::new(self.notices.log_messages_truncated.get().saturating_add(1));
                    self.notices.logs_truncated = true;
                }
                self.log_bytes += row.message.len() as u64;
                Ok(Some(ResultRecord::Log(row)))
            }
            ResultRecord::Record(row) => {
                if row.values.is_empty()
                    || row.values.len() > 64
                    || self.record_points >= self.limits.record_points
                {
                    return Err(failure());
                }
                for (name, value) in &row.values {
                    crate::types::keys::label(name, 128)?;
                    if matches!(value,super::RecordValue::Text(s) if s.len()>4096) {
                        return Err(failure());
                    }
                    self.names.insert(name.clone());
                    if self.names.len() > 128 {
                        return Err(failure());
                    }
                }
                self.record_points += 1;
                Ok(Some(record.clone()))
            }
            ResultRecord::Summary(summary) => {
                if self.performance.is_some() {
                    return Err(invalid());
                }
                self.performance = Some(summary.as_ref().clone());
                Ok(Some(record.clone()))
            }
            _ => Ok(Some(record.clone())),
        }
    }
    fn write_batch(&mut self, batch: &ResultBatch) -> QfResult<()> {
        self.check()?;
        if !self.credit_issued || batch.first_sequence().get() != self.next_sequence {
            return Err(invalid());
        }
        self.credit_issued = false;
        encode_bounded(batch, MAX_CONTROL_BYTES)?;
        if batch.records().len() > 1024 {
            return Err(failure());
        }
        for record in batch.records() {
            self.check()?;
            if let Some(record) = self.normalize(record)? {
                let kind = partition(&record);
                let envelope = serde_json::to_value(&record).map_err(|_| invalid())?;
                let buffer = encode_bounded(
                    &envelope["record"],
                    self.limits.max_page_bytes.saturating_sub(1024),
                )?;
                let size = buffer.len() as u64 + 20;
                // Reserve small terminal metadata, accounted again at commit.
                let reserve = self.limits.total_bytes.min(64 * 1024);
                if self
                    .bytes
                    .checked_add(size)
                    .and_then(|n| n.checked_add(reserve))
                    .is_none_or(|n| n > self.limits.total_bytes)
                {
                    return Err(failure());
                }
                self.ensure_active(kind, buffer.len())?;
                let active = self.active.get_mut(kind).ok_or_else(invalid)?;
                identity(&self.root, &active.descriptor.file, &active.data)?;
                identity(&self.root, &active.descriptor.index_file, &active.index)?;
                let length = (buffer.len() as u32).to_le_bytes();
                let offset = active.descriptor.data_bytes.get();
                let mut index = [0_u8; 16];
                index[..8].copy_from_slice(&offset.to_le_bytes());
                index[8..].copy_from_slice(&self.next_sequence.to_le_bytes());
                active.data.write_all(&length).map_err(|_| failure())?;
                active.data.write_all(&buffer).map_err(|_| failure())?;
                active.index.write_all(&index).map_err(|_| failure())?;
                active.data_hash.update(length);
                active.data_hash.update(&buffer);
                active.index_hash.update(index);
                active.descriptor.count = Sequence::new(active.descriptor.count.get() + 1);
                active.descriptor.data_bytes = Sequence::new(offset + buffer.len() as u64 + 4);
                active.descriptor.index_bytes =
                    Sequence::new(active.descriptor.index_bytes.get() + 16);
                active.descriptor.last_sequence = Sequence::new(self.next_sequence);
                *self.counts.entry(kind.into()).or_default() += 1;
                self.bytes += size;
            }
            self.next_sequence = self.next_sequence.checked_add(1).ok_or_else(failure)?;
        }
        Ok(())
    }
    pub fn note_dropped_log(&mut self) {
        self.notices.logs_dropped =
            Sequence::new(self.notices.logs_dropped.get().saturating_add(1));
        self.notices.logs_truncated = true;
    }
    pub fn overview(&self) -> Value {
        json!({"schema_id":FORMAT,"outcome":self.outcome,"partial":!matches!(self.outcome,Some(RunOutcome::Succeeded{..})),
            "counts":self.counts,"result_bytes":self.bytes,"max_page_read":self.max_page_read,
            "notices":self.notices,"performance":self.performance})
    }
    pub fn manifest(&self) -> Option<ResultManifest> {
        self.outcome.clone().map(|outcome| ResultManifest {
            schema_id: FORMAT.into(),
            run_id: self.run_id.clone(),
            partial: !matches!(outcome, RunOutcome::Succeeded { .. }),
            outcome,
            counts: self
                .counts
                .iter()
                .map(|(k, n)| (k.clone(), Sequence::new(*n)))
                .collect(),
            shards: self.shards.clone(),
            result_bytes: Sequence::new(self.bytes),
            notices: self.notices.clone(),
            performance: self.performance.clone(),
        })
    }
    /// D12/D13 explicitly take retained lifetime after a terminal commit.
    /// Preparation writes/syncs all required output first; a service can then
    /// hold its short data-consistency/claim/cancel guard through local commit.
    pub fn finalize_guarded<G>(
        &mut self,
        outcome: &RunOutcome,
        acquire: impl FnOnce() -> QfResult<G>,
    ) -> QfResult<()> {
        self.check()?;
        if !matches!(outcome,RunOutcome::Succeeded{result_id} if result_id==&self.run_id)
            || self.limits.require_summary && self.performance.is_none()
        {
            return Err(invalid());
        }
        let result = self.commit_guarded(outcome.clone(), acquire);
        if let Err(e) = &result {
            self.fault.get_or_insert_with(|| e.clone());
        }
        result
    }
    pub fn retain(&mut self) -> QfResult<()> {
        if self.outcome.is_none() || !self.created.contains("manifest.json") {
            return Err(invalid());
        }
        self.retain = true;
        Ok(())
    }
    fn commit(&mut self, outcome: RunOutcome) -> QfResult<()> {
        self.commit_guarded(outcome, || Ok(()))
    }
    fn commit_guarded<G>(
        &mut self,
        outcome: RunOutcome,
        acquire: impl FnOnce() -> QfResult<G>,
    ) -> QfResult<()> {
        self.check_location()?;
        let kinds: Vec<_> = self.active.keys().cloned().collect();
        for kind in kinds {
            self.seal(&kind)?;
        }
        let ignore_cancel = AtomicBool::new(false);
        let commit_cancel = if matches!(outcome, RunOutcome::Succeeded { .. }) {
            self.cancelled.as_ref()
        } else {
            &ignore_cancel
        };
        for descriptor in &self.shards {
            let data = file(&self.root, &descriptor.file, false)?;
            let index = file(&self.root, &descriptor.index_file, false)?;
            // Recheck sealed shards too; finalization may follow an earlier seal.
            if digest(&data, descriptor.data_bytes.get(), commit_cancel)? != descriptor.sha256
                || digest(&index, descriptor.index_bytes.get(), commit_cancel)?
                    != descriptor.index_sha256
            {
                return Err(failure());
            }
        }
        for name in ["manifest.pending", "manifest.json"] {
            if self.created.remove(name) {
                fs::unlinkat(&self.root, name, AtFlags::empty()).map_err(|_| failure())?;
            }
        }
        let mut manifest = ResultManifest {
            schema_id: FORMAT.into(),
            run_id: self.run_id.clone(),
            partial: !matches!(outcome, RunOutcome::Succeeded { .. }),
            outcome: outcome.clone(),
            counts: self
                .counts
                .iter()
                .map(|(k, n)| (k.clone(), Sequence::new(*n)))
                .collect(),
            shards: self.shards.clone(),
            result_bytes: Sequence::new(self.bytes),
            notices: self.notices.clone(),
            performance: self.performance.clone(),
        };
        let mut bytes = encode_bounded(&manifest, MAX_CONTROL_BYTES)?;
        // Account the metadata itself without recursively hashing its size.
        for _ in 0..3 {
            manifest.result_bytes = Sequence::new(self.bytes + bytes.len() as u64);
            bytes = encode_bounded(&manifest, MAX_CONTROL_BYTES)?;
        }
        if self.bytes + bytes.len() as u64 > self.limits.total_bytes {
            return Err(failure());
        }
        let mut control = file(&self.root, "manifest.pending", true)?;
        self.created.insert("manifest.pending".into());
        control.write_all(&bytes).map_err(|_| failure())?;
        control.sync_all().map_err(|_| failure())?;
        let _guard = acquire()?; // Held through rename, directory sync and publication.
        if matches!(outcome, RunOutcome::Succeeded { .. }) {
            self.check()?;
        }
        identity(&self.root, "manifest.pending", &control)?;
        fs::renameat_with(
            &self.root,
            "manifest.pending",
            &self.root,
            "manifest.json",
            fs::RenameFlags::NOREPLACE,
        )
        .map_err(|_| failure())?;
        self.created.remove("manifest.pending");
        self.created.insert("manifest.json".into());
        if fs::fsync(&self.root).is_err() {
            let _ = fs::unlinkat(&self.root, "manifest.json", AtFlags::empty());
            self.created.remove("manifest.json");
            return Err(failure());
        }
        self.bytes += bytes.len() as u64;
        self.outcome = Some(outcome);
        Ok(())
    }
    pub fn page(&mut self, name: &str, after: Option<&str>, limit: usize) -> QfResult<Value> {
        self.check_location()?;
        if ![
            "equity",
            "orders",
            "trades",
            "positions",
            "records",
            "logs",
            "metadata",
        ]
        .contains(&name)
            || limit == 0
            || limit > super::MAX_PAGE_ITEMS
        {
            return Err(invalid());
        }
        let prefix = format!("{}:{name}:", self.token);
        let start = match after {
            None => 0,
            Some(cursor) => {
                if cursor.len() > super::MAX_CURSOR_BYTES {
                    return Err(invalid());
                }
                let suffix = cursor.strip_prefix(&prefix).ok_or_else(invalid)?;
                let n: u64 = suffix.parse().map_err(|_| invalid())?;
                if suffix != n.to_string() {
                    return Err(invalid());
                }
                n
            }
        };
        let count = *self.counts.get(name).unwrap_or(&0);
        if start > count {
            return Err(invalid());
        }
        let mut descriptors: Vec<_> = self
            .shards
            .iter()
            .filter(|s| s.kind == name)
            .cloned()
            .collect();
        if let Some(a) = self.active.get(name) {
            descriptors.push(a.descriptor.clone());
        }
        descriptors.sort_by_key(|s| s.start_item.get());
        let mut items = Vec::new();
        let mut used = 0;
        let mut next = start;
        for descriptor in descriptors {
            if next >= descriptor.start_item.get() + descriptor.count.get() {
                continue;
            }
            let (data, index) = if let Some(active) = self.active.get(name)
                && active.descriptor.file == descriptor.file
            {
                identity(&self.root, &descriptor.file, &active.data)?;
                identity(&self.root, &descriptor.index_file, &active.index)?;
                (
                    active.data.try_clone().map_err(|_| failure())?,
                    active.index.try_clone().map_err(|_| failure())?,
                )
            } else {
                (
                    file(&self.root, &descriptor.file, false)?,
                    file(&self.root, &descriptor.index_file, false)?,
                )
            };
            if !descriptor.sha256.is_empty()
                && (digest(&data, descriptor.data_bytes.get(), &AtomicBool::new(false))?
                    != descriptor.sha256
                    || digest(
                        &index,
                        descriptor.index_bytes.get(),
                        &AtomicBool::new(false),
                    )? != descriptor.index_sha256)
            {
                return Err(failure());
            }
            while next < descriptor.start_item.get() + descriptor.count.get() && items.len() < limit
            {
                let mut word = [0_u8; 16];
                index
                    .read_exact_at(&mut word, (next - descriptor.start_item.get()) * 16)
                    .map_err(|_| failure())?;
                let offset = u64::from_le_bytes(word[..8].try_into().expect("word"));
                let sequence = u64::from_le_bytes(word[8..].try_into().expect("word"));
                let mut word = [0_u8; 4];
                data.read_exact_at(&mut word, offset)
                    .map_err(|_| failure())?;
                let size = u32::from_le_bytes(word) as usize;
                if size > self.limits.max_page_bytes.saturating_sub(1024)
                    || offset + 4 + size as u64 > descriptor.data_bytes.get()
                {
                    return Err(failure());
                }
                if used + size + 128 > self.limits.max_page_bytes {
                    break;
                }
                let mut bytes = vec![0; size];
                data.read_exact_at(&mut bytes, offset + 4)
                    .map_err(|_| failure())?;
                let mut item: Value = serde_json::from_slice(&bytes).map_err(|_| failure())?;
                item.as_object_mut()
                    .ok_or_else(invalid)?
                    .insert("result_sequence".into(), json!(sequence.to_string()));
                items.push(item);
                used += size + 128;
                next += 1;
            }
            if items.len() >= limit || next < descriptor.start_item.get() + descriptor.count.get() {
                break;
            }
        }
        if items.is_empty() && next < count {
            return Err(failure());
        }
        self.max_page_read = self.max_page_read.max(items.len());
        let value = json!({"items":items,"next_cursor":(next<count).then(||format!("{prefix}{next}")),
            "partial":!matches!(self.outcome,Some(RunOutcome::Succeeded{..})),"schema_id":FORMAT,"notices":self.notices});
        encode_bounded(&value, MAX_CONTROL_BYTES)?;
        Ok(value)
    }
}
impl ResultSink for FileResultSink {
    fn credit(&mut self) -> QfResult<SinkCredit> {
        self.check()?;
        self.credit_issued = true;
        Ok(SinkCredit {
            max_records: 1024,
            max_bytes: MAX_CONTROL_BYTES,
        })
    }
    fn write(&mut self, batch: &ResultBatch) -> QfResult<()> {
        let result = self.write_batch(batch);
        if let Err(e) = &result {
            self.fault.get_or_insert_with(|| e.clone());
        }
        result
    }
    fn finalize(&mut self, outcome: &RunOutcome) -> QfResult<()> {
        self.finalize_guarded(outcome, || Ok(()))
    }
    fn abort(&mut self, error: &QfError) -> QfResult<()> {
        if self.outcome.is_some() {
            return Ok(());
        }
        let outcome = if error.code == ErrorCode::Cancelled {
            RunOutcome::Cancelled {
                partial: self.next_sequence > 1,
            }
        } else {
            RunOutcome::Failed {
                error: error.clone(),
                partial: self.next_sequence > 1,
            }
        };
        if let Some(p) = &mut self.performance {
            p.invalidate("PARTIAL_RESULT");
        }
        // Failure metadata is best effort; lack of disk capacity cannot invent success.
        let result = self.commit(outcome.clone());
        self.outcome = Some(outcome);
        result
    }
}
impl Drop for FileResultSink {
    fn drop(&mut self) {
        self.active.clear();
        if self.retain {
            return;
        }
        for name in &self.created {
            let _ = fs::unlinkat(&self.root, name.as_str(), AtFlags::empty());
        }
        // Never remove a replacement directory after a path rename/swap.
        if let (Ok(a), Ok(b)) = (
            fs::fstat(&self.root),
            fs::statat(&self.parent, self.name.as_str(), AtFlags::SYMLINK_NOFOLLOW),
        ) && a.st_ino == b.st_ino
            && a.st_dev == b.st_dev
        {
            let _ = fs::unlinkat(&self.parent, self.name.as_str(), AtFlags::REMOVEDIR);
        }
    }
}

/// Retained history uses exactly the same bounded indexed page implementation.
/// D13 must authorize owner/run and fence the manifest before opening it.
pub struct FileResultReader {
    inner: FileResultSink,
}
impl FileResultReader {
    pub fn open(parent_path: &Path, directory_name: &str, run_id: &str) -> QfResult<Self> {
        crate::types::keys::label(run_id, 128)?;
        if !directory_name.starts_with("qf-results-") || directory_name.contains(['/', '\\']) {
            return Err(invalid());
        }
        let parent = directory(parent_path)?;
        let root = fs::openat(
            &parent,
            directory_name,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|_| invalid())?;
        let control = file(&root, "manifest.json", false)?;
        let size = control.metadata().map_err(|_| failure())?.len();
        if size == 0 || size > MAX_CONTROL_BYTES as u64 {
            return Err(failure());
        }
        let mut bytes = vec![0; size as usize];
        control
            .read_exact_at(&mut bytes, 0)
            .map_err(|_| failure())?;
        let manifest: ResultManifest = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
        if manifest.schema_id != FORMAT
            || manifest.run_id != run_id
            || manifest.shards.len() > 4096
            || manifest.partial == matches!(manifest.outcome, RunOutcome::Succeeded { .. })
            || manifest.result_bytes.get() > 8 * 1024 * 1024 * 1024
        {
            return Err(invalid());
        }
        if let RunOutcome::Succeeded { result_id } = &manifest.outcome
            && result_id != run_id
        {
            return Err(invalid());
        }
        let mut counts = BTreeMap::<String, u64>::new();
        let mut names = BTreeSet::new();
        let mut descriptors = manifest.shards.clone();
        descriptors.sort_by_key(|s| (s.kind.clone(), s.start_item.get()));
        let mut file_bytes = size;
        for shard in &descriptors {
            if ![
                "summary",
                "equity",
                "orders",
                "trades",
                "positions",
                "records",
                "logs",
                "metadata",
            ]
            .contains(&shard.kind.as_str())
                || shard.count.get() == 0
                || shard.index_bytes.get()
                    != shard.count.get().checked_mul(16).ok_or_else(invalid)?
                || shard.data_bytes.get() > 64 * 1024 * 1024
                || shard.start_item.get() != *counts.get(&shard.kind).unwrap_or(&0)
                || shard.first_sequence.get() == 0
                || shard.last_sequence.get() < shard.first_sequence.get()
                || shard.sha256.len() != 64
                || shard.index_sha256.len() != 64
            {
                return Err(invalid());
            }
            for (name, suffix) in [(&shard.file, ".data"), (&shard.index_file, ".index")] {
                let id = name
                    .strip_prefix(&format!("{}-", shard.kind))
                    .and_then(|s| s.strip_suffix(suffix))
                    .ok_or_else(invalid)?;
                if id.len() != 6
                    || !id.bytes().all(|b| b.is_ascii_digit())
                    || !names.insert(name.clone())
                {
                    return Err(invalid());
                }
                file(&root, name, false)?;
            }
            counts.insert(
                shard.kind.clone(),
                shard
                    .start_item
                    .get()
                    .checked_add(shard.count.get())
                    .ok_or_else(invalid)?,
            );
            file_bytes = file_bytes
                .checked_add(shard.data_bytes.get())
                .and_then(|n| n.checked_add(shard.index_bytes.get()))
                .ok_or_else(invalid)?;
        }
        if counts
            != manifest
                .counts
                .iter()
                .map(|(k, n)| (k.clone(), n.get()))
                .collect()
            || file_bytes != manifest.result_bytes.get()
        {
            return Err(invalid());
        }
        let token = format!("retained-{run_id}-{directory_name}");
        let inner = FileResultSink {
            parent,
            root,
            name: directory_name.into(),
            run_id: run_id.into(),
            token,
            limits: FileLimits::default(),
            active: BTreeMap::new(),
            shards: descriptors,
            counts,
            created: BTreeSet::new(),
            names: BTreeSet::new(),
            next_sequence: 0,
            bytes: manifest.result_bytes.get(),
            record_points: 0,
            log_bytes: 0,
            notices: manifest.notices,
            performance: manifest.performance,
            outcome: Some(manifest.outcome),
            fault: None,
            credit_issued: false,
            cancelled: Arc::new(AtomicBool::new(false)),
            retain: true,
            max_page_read: 0,
        };
        inner.check_location()?;
        Ok(Self { inner })
    }
    pub fn overview(&self) -> Value {
        self.inner.overview()
    }
    pub fn page(
        &mut self,
        kind: ResultKind,
        cursor: Option<&str>,
        limit: usize,
    ) -> QfResult<Value> {
        self.inner
            .page(public_kind(kind).ok_or_else(invalid)?, cursor, limit)
    }
}

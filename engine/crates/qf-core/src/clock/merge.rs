//! Bounded k-way merge. Input chunks must already be ordered by the D01 key.
//! A duplicate full key is an ambiguous contract error, never a deduplication.
use crate::types::{EventKey, MarketEvent};
use crate::{ErrorCode, QfError, QfResult};
use std::cmp::Reverse;
use std::collections::{BinaryHeap, VecDeque};

pub trait Checkpoint {
    /// D12 supplies cancellation/deadline checks. Called before every source read
    /// and every market event, including priming thousands of independent feeds.
    fn check(&mut self) -> QfResult<()>;
}
pub trait EventSource {
    /// None is permanent EOF; both length/capacity must be <= max_events.
    /// An empty/nonbounded chunk is rejected. D04 owns
    /// decoding and dependency checks around reads, with interruptible I/O.
    fn next_chunk(&mut self, max_events: usize) -> QfResult<Option<Vec<MarketEvent>>>;
}

#[derive(Debug, Clone, Copy)]
pub struct MergeLimits {
    pub max_sources: usize,
    pub chunk_events: usize,
    pub max_buffered_events: usize,
    pub max_buffered_bytes: usize,
}
impl Default for MergeLimits {
    fn default() -> Self {
        Self {
            max_sources: 10_000,
            chunk_events: 1024,
            max_buffered_events: 10_000,
            max_buffered_bytes: crate::data::MAX_ARROW_BATCH_BYTES,
        }
    }
}
struct Cursor<S> {
    source: S,
    chunk: VecDeque<(MarketEvent, usize)>,
    last_read: Option<EventKey>,
    eof: bool,
}
#[derive(Debug, Default, Clone, Copy)]
pub struct MergeStats {
    pub peak_buffered_events: usize,
    pub peak_buffered_bytes: usize,
    pub source_reads: u64,
}
pub struct StreamingMerge<S> {
    cursors: Vec<Cursor<S>>,
    heads: BinaryHeap<Reverse<(EventKey, usize)>>,
    limits: MergeLimits,
    chunk_events: usize,
    primed: bool,
    buffered_events: usize,
    buffered_bytes: usize,
    last_output: Option<EventKey>,
    stats: MergeStats,
}
fn invalid(message: &str) -> QfError {
    QfError::new(ErrorCode::InvalidContract, "event_merge", message)
}
fn limit() -> QfError {
    QfError::new(
        ErrorCode::ResourceLimit,
        "event_merge",
        "事件源或合并缓冲超过预算",
    )
}

impl<S: EventSource> StreamingMerge<S> {
    /// Construction performs no reads. Memory depends on this budget and source
    /// count, not the full event history. Sources must have stable upstream keys.
    pub fn new(sources: Vec<S>, limits: MergeLimits) -> QfResult<Self> {
        if sources.len() > limits.max_sources
            || limits.max_sources > 10_000
            || limits.chunk_events == 0
            || limits.chunk_events > 10_000
            || limits.max_buffered_events == 0
            || limits.max_buffered_events < sources.len()
            || limits.max_buffered_bytes == 0
            || limits.max_buffered_bytes > crate::data::MAX_ARROW_BATCH_BYTES
        {
            return Err(limit());
        }
        let chunk_events = limits
            .chunk_events
            .min(limits.max_buffered_events / sources.len().max(1));
        Ok(Self {
            cursors: sources
                .into_iter()
                .map(|source| Cursor {
                    source,
                    chunk: VecDeque::new(),
                    last_read: None,
                    eof: false,
                })
                .collect(),
            heads: BinaryHeap::new(),
            limits,
            chunk_events,
            primed: false,
            buffered_events: 0,
            buffered_bytes: 0,
            last_output: None,
            stats: MergeStats::default(),
        })
    }
    pub fn stats(&self) -> MergeStats {
        self.stats
    }
    fn fill(&mut self, index: usize, checkpoint: &mut dyn Checkpoint) -> QfResult<()> {
        let cursor = &mut self.cursors[index];
        if cursor.eof || !cursor.chunk.is_empty() {
            return Ok(());
        }
        checkpoint.check()?;
        self.stats.source_reads = self.stats.source_reads.checked_add(1).ok_or_else(limit)?;
        let Some(chunk) = cursor.source.next_chunk(self.chunk_events)? else {
            cursor.eof = true;
            return Ok(());
        };
        if chunk.is_empty() {
            return Err(invalid("事件源返回空块，必须明确返回EOF"));
        }
        if chunk.len() > self.chunk_events || chunk.capacity() > self.chunk_events {
            return Err(limit());
        }
        for event in chunk {
            checkpoint.check()?;
            event.validate()?;
            let key = event.key();
            if cursor.last_read.as_ref().is_some_and(|last| &key <= last) {
                return Err(invalid("来源块内或跨块事件键反序或重复"));
            }
            // Serialized row size is a bounded wire/cache budget, not a claim of
            // measuring process RSS. D04 separately accounts Arrow ownership.
            let bytes = serde_json::to_vec(&event)
                .map_err(|_| invalid("事件编码无效"))?
                .len();
            self.buffered_events = self.buffered_events.checked_add(1).ok_or_else(limit)?;
            self.buffered_bytes = self.buffered_bytes.checked_add(bytes).ok_or_else(limit)?;
            if self.buffered_events > self.limits.max_buffered_events
                || self.buffered_bytes > self.limits.max_buffered_bytes
            {
                return Err(limit());
            }
            cursor.last_read = Some(key);
            cursor.chunk.push_back((event, bytes));
        }
        self.stats.peak_buffered_events = self.stats.peak_buffered_events.max(self.buffered_events);
        self.stats.peak_buffered_bytes = self.stats.peak_buffered_bytes.max(self.buffered_bytes);
        self.heads.push(Reverse((
            cursor.chunk.front().expect("nonempty chunk").0.key(),
            index,
        )));
        Ok(())
    }
    fn prime(&mut self, checkpoint: &mut dyn Checkpoint) -> QfResult<()> {
        if !self.primed {
            for index in 0..self.cursors.len() {
                self.fill(index, checkpoint)?;
            }
            self.primed = true;
        }
        Ok(())
    }
    pub fn peek(&mut self, checkpoint: &mut dyn Checkpoint) -> QfResult<Option<&MarketEvent>> {
        checkpoint.check()?;
        self.prime(checkpoint)?;
        Ok(self
            .heads
            .peek()
            .map(|Reverse((_, index))| &self.cursors[*index].chunk.front().expect("heap head").0))
    }
    pub fn pop(&mut self, checkpoint: &mut dyn Checkpoint) -> QfResult<Option<MarketEvent>> {
        checkpoint.check()?;
        self.prime(checkpoint)?;
        let Some(Reverse((key, index))) = self.heads.pop() else {
            return Ok(None);
        };
        if self
            .heads
            .peek()
            .is_some_and(|Reverse((next, _))| next == &key)
        {
            return Err(invalid("跨来源完整事件键冲突，不能依来源读取顺序决定赢家"));
        }
        if self.last_output.as_ref().is_some_and(|last| &key <= last) {
            return Err(invalid("跨来源完整事件键重复或反序，不可静默删除"));
        }
        let (event, bytes) = self.cursors[index].chunk.pop_front().expect("heap head");
        self.buffered_events -= 1;
        self.buffered_bytes -= bytes;
        self.last_output = Some(key);
        if let Some((next, _)) = self.cursors[index].chunk.front() {
            self.heads.push(Reverse((next.key(), index)));
        } else {
            self.fill(index, checkpoint)?;
        }
        Ok(Some(event))
    }
}

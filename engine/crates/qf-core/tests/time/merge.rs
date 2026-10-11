use super::support::*;
use qf_core::clock::{Checkpoint, EventSource, MergeLimits, StreamingMerge};
use qf_core::types::{EventKey, MarketEvent};
use qf_core::{ErrorCode, QfResult};

fn collect(mut merger: StreamingMerge<Source>) -> QfResult<Vec<EventKey>> {
    let mut control = Control::default();
    let mut keys = vec![];
    while let Some(event) = merger.pop(&mut control)? {
        keys.push(event.key());
    }
    Ok(keys)
}
#[test]
fn source_permutations_and_chunk_partitions_preserve_full_identity() {
    let session = row(0);
    let time = local(&session, 600);
    let streams = [
        (1..=4)
            .map(|n| tick(&session, "A", "a", time, n))
            .collect::<Vec<_>>(),
        (1..=3).map(|n| tick(&session, "B", "a", time, n)).collect(),
        vec![
            tick(&session, "A", "b", time, 1),
            quote(&session, "B", time, 4),
        ],
    ];
    let mut expected: Vec<_> = streams.iter().flatten().map(MarketEvent::key).collect();
    expected.sort();
    // Exhaustive small property: independent source chunk sizes and every feed
    // ordering. Nothing uses source index to break a legitimate market tie.
    for a in 1..=4 {
        for b in 1..=4 {
            for c in 1..=4 {
                for order in [
                    [0, 1, 2],
                    [0, 2, 1],
                    [1, 0, 2],
                    [1, 2, 0],
                    [2, 0, 1],
                    [2, 1, 0],
                ] {
                    let chunks = [a, b, c];
                    let sources = order
                        .into_iter()
                        .map(|i| Source::new(streams[i].clone(), chunks[i]))
                        .collect();
                    let merged = collect(
                        StreamingMerge::new(
                            sources,
                            MergeLimits {
                                chunk_events: 4,
                                max_buffered_events: 12,
                                ..MergeLimits::default()
                            },
                        )
                        .unwrap(),
                    )
                    .unwrap();
                    assert_eq!(merged, expected);
                }
            }
        }
    }
    assert_eq!(expected.len(), 9);
}
#[test]
fn identical_values_at_same_nanosecond_with_distinct_sequences_are_not_deduplicated() {
    let session = row(0);
    let events = vec![
        tick(&session, "A", "trade", local(&session, 600), 1),
        tick(&session, "A", "trade", local(&session, 600), 2),
    ];
    assert_eq!(
        collect(StreamingMerge::new(vec![Source::new(events, 1)], MergeLimits::default()).unwrap())
            .unwrap()
            .len(),
        2
    );
}
#[test]
fn exact_key_collisions_unsorted_sources_and_empty_chunks_fail() {
    let session = row(0);
    let first = tick(&session, "A", "a", local(&session, 600), 1);
    let second = tick(&session, "A", "a", local(&session, 600), 2);
    assert_eq!(
        collect(
            StreamingMerge::new(
                vec![
                    Source::new(vec![first.clone()], 1),
                    Source::new(vec![first.clone()], 1)
                ],
                MergeLimits::default()
            )
            .unwrap()
        )
        .unwrap_err()
        .code,
        ErrorCode::InvalidContract
    );
    let mut reversed = Source::new(vec![first, second], 1);
    reversed.events.make_contiguous().reverse();
    assert_eq!(
        collect(StreamingMerge::new(vec![reversed], MergeLimits::default()).unwrap())
            .unwrap_err()
            .code,
        ErrorCode::InvalidContract
    );
    let empty_chunk = Source::new(vec![tick(&session, "A", "a", local(&session, 600), 1)], 0);
    assert_eq!(
        collect(StreamingMerge::new(vec![empty_chunk], MergeLimits::default()).unwrap())
            .unwrap_err()
            .code,
        ErrorCode::InvalidContract
    );
}
#[test]
fn merge_errors_are_terminal_and_retry_cannot_skip_a_conflicting_event() {
    let session = row(0);
    let event = tick(&session, "A", "a", local(&session, 600), 1);
    let source = Source::new(vec![event.clone()], 1);
    let reads = source.reads.clone();
    let mut merge = StreamingMerge::new(
        vec![source, Source::new(vec![event], 1)],
        MergeLimits::default(),
    )
    .unwrap();
    let mut control = Control::default();
    let failure = merge.pop(&mut control).unwrap_err();
    assert_eq!(failure.code, ErrorCode::InvalidContract);
    for _ in 0..3 {
        assert_eq!(merge.pop(&mut control).unwrap_err(), failure);
        assert_eq!(merge.peek(&mut control).unwrap_err(), failure);
    }
    assert_eq!(reads.get(), 1);
}
#[test]
fn missing_source_sequence_uses_declared_stable_input_identity() {
    let session = row(0);
    let events: Vec<_> = (1..=3)
        .map(|n| {
            let mut event = tick(&session, "A", "a", local(&session, 600), n);
            if let MarketEvent::TradeTick(t) = &mut event {
                t.identity.sequence = None;
            }
            event
        })
        .collect();
    assert_eq!(
        collect(StreamingMerge::new(vec![Source::new(events, 2)], MergeLimits::default()).unwrap())
            .unwrap()
            .len(),
        3
    );
}
struct GeneratedSource {
    remaining: u64,
    sequence: u64,
    template: MarketEvent,
}
impl EventSource for GeneratedSource {
    fn next_chunk(&mut self, maximum: usize) -> QfResult<Option<Vec<MarketEvent>>> {
        if self.remaining == 0 {
            return Ok(None);
        }
        let count = maximum.min(self.remaining as usize);
        let mut events = Vec::with_capacity(count);
        for _ in 0..count {
            self.sequence += 1;
            let mut event = self.template.clone();
            if let MarketEvent::TradeTick(tick) = &mut event {
                tick.identity.sequence = Some(qf_core::types::Sequence::new(self.sequence));
                tick.identity.stable_input_sequence = qf_core::types::Sequence::new(self.sequence);
            }
            events.push(event);
        }
        self.remaining -= count as u64;
        Ok(Some(events))
    }
}
#[test]
fn long_lazy_source_stays_within_global_buffer_budget() {
    let session = row(0);
    let sources = ["A", "B"]
        .into_iter()
        .map(|symbol| GeneratedSource {
            remaining: 25_000,
            sequence: 0,
            template: tick(&session, symbol, "a", local(&session, 600), 0),
        })
        .collect();
    let mut merger = StreamingMerge::new(
        sources,
        MergeLimits {
            chunk_events: 31,
            max_buffered_events: 62,
            ..MergeLimits::default()
        },
    )
    .unwrap();
    let mut control = Control::default();
    let mut count = 0;
    while merger.pop(&mut control).unwrap().is_some() {
        count += 1;
    }
    assert_eq!(count, 50_000);
    assert!(merger.stats().peak_buffered_events <= 62);
    assert!(merger.stats().source_reads > 1000);
}
#[test]
fn cancellation_during_priming_and_byte_budget_are_bounded() {
    let session = row(0);
    let first = Source::new(vec![tick(&session, "A", "a", local(&session, 600), 1)], 1);
    let reads = first.reads.clone();
    let mut merge = StreamingMerge::new(vec![first], MergeLimits::default()).unwrap();
    let mut control = Control {
        cancel_at_check: Some(1),
        ..Control::default()
    };
    assert_eq!(
        merge.pop(&mut control).unwrap_err().code,
        ErrorCode::Cancelled
    );
    assert_eq!(reads.get(), 0);
    let source = Source::new(vec![tick(&session, "A", "a", local(&session, 600), 1)], 1);
    assert_eq!(
        collect(
            StreamingMerge::new(
                vec![source],
                MergeLimits {
                    max_buffered_bytes: 1,
                    ..MergeLimits::default()
                }
            )
            .unwrap()
        )
        .unwrap_err()
        .code,
        ErrorCode::ResourceLimit
    );
    let mut noop = Control::default();
    noop.check().unwrap();
}

use arrow_array::{ArrayRef, Int64Array, RecordBatch, StringArray, UInt64Array};
use arrow_ipc::writer::StreamWriter;
use arrow_schema::{DataType, Field, Schema};
use qf_core::clock::EventSource;
use qf_core::data::arrow::{ArrowEventSource, BatchSource, decode_market};
use qf_core::data::ipc::IpcGateway;
use qf_core::data::{ActualScope, BatchMetadata, DataBatch, DataGateway, RunScope, TimeUnit};
use qf_core::types::market::{Currency, QuantityUnit};
use qf_core::types::{Nanoseconds, SecurityKey};
use qf_core::{ErrorCode, QfResult};
use std::collections::{HashMap, VecDeque};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

const BASE: i64 = 1767225600000000123;
fn batch(price: &str, sequences: Vec<u64>) -> DataBatch {
    let mut fields = Vec::new();
    let mut arrays: Vec<ArrayRef> = Vec::new();
    let texts = [
        "kind",
        "security",
        "session",
        "source_session",
        "channel",
        "open",
        "high",
        "low",
        "close",
        "price",
        "bid",
        "ask",
    ];
    let len = sequences.len();
    for name in texts {
        fields.push(Field::new(name, DataType::Utf8, true));
        let value = match name {
            "kind" => Some("trade_tick"),
            "security" => Some("A.SH"),
            "session" | "source_session" => Some("S"),
            "channel" => Some("C"),
            "price" => Some(price),
            _ => None,
        };
        arrays.push(Arc::new(StringArray::from(vec![value; len])));
    }
    for name in [
        "time_ns",
        "interval_start_ns",
        "interval_end_ns",
        "quantity",
        "bid_quantity",
        "ask_quantity",
    ] {
        fields.push(Field::new(name, DataType::Int64, true));
        let value = match name {
            "time_ns" => Some(BASE),
            "quantity" => Some(7),
            _ => None,
        };
        arrays.push(Arc::new(Int64Array::from(vec![value; len])));
    }
    for name in ["sequence", "stable_input_sequence"] {
        fields.push(Field::new(name, DataType::UInt64, true));
        arrays.push(Arc::new(UInt64Array::from(sequences.clone())));
    }
    let metadata: HashMap<String, String> = [
        ("schema_id", "qf.market.v1"),
        ("time_unit", "utc_nanoseconds"),
        ("price_currency", "CNY"),
        ("quantity_unit", "shares"),
        ("price_basis", "raw"),
        ("exchange_timezone", "Asia/Shanghai"),
    ]
    .into_iter()
    .map(|(k, v)| (k.into(), v.into()))
    .collect();
    let schema = Arc::new(Schema::new_with_metadata(fields, metadata));
    let rb = RecordBatch::try_new(schema.clone(), arrays).unwrap();
    let mut bytes = Vec::new();
    {
        let mut writer = StreamWriter::try_new(&mut bytes, &schema).unwrap();
        writer.write(&rb).unwrap();
        writer.finish().unwrap();
    }
    DataBatch::new(
        BatchMetadata {
            schema_id: "qf.market.v1".into(),
            time_unit: TimeUnit::UtcNanoseconds,
            quantity_unit: QuantityUnit::Shares,
            price_currency: Currency::CNY,
            rows: len as u64,
            actual_scope: ActualScope {
                start_ns: Some(Nanoseconds::new(BASE)),
                end_ns: Some(Nanoseconds::new(BASE)),
                securities: vec![SecurityKey::new("A.SH").unwrap()],
            },
            limitations: vec!["synthetic".into()],
        },
        bytes,
        64 * 1024 * 1024,
    )
    .unwrap()
}
#[test]
fn arrow_exact_decimal_nanosecond_and_distinct_identity() {
    let events = decode_market(
        &batch("10.123456789012345678", vec![1, 2]),
        2,
        64 * 1024 * 1024,
    )
    .unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].key().time_ns.get(), BASE);
    assert_ne!(events[0].key(), events[1].key());
    let json = serde_json::to_value(&events[0]).unwrap();
    assert_eq!(json["event"]["price"], "10.123456789012345678");
}
#[test]
fn input_limits_precision_and_duplicate_full_key_fail_closed() {
    let error = decode_market(
        &batch("12345678901234567890.123456789012345678", vec![1]),
        2,
        64 * 1024 * 1024,
    )
    .unwrap_err();
    assert_eq!(error.code, ErrorCode::NumericRangeUnsupported);
    assert_eq!(
        decode_market(&batch("1", vec![1, 1]), 2, 64 * 1024 * 1024)
            .unwrap_err()
            .code,
        ErrorCode::InvalidContract
    );
    assert_eq!(
        decode_market(&batch("1", vec![1, 2]), 1, 64 * 1024 * 1024)
            .unwrap_err()
            .code,
        ErrorCode::ResourceLimit
    );
}
#[test]
fn malicious_arrow_metadata_length_rejected_before_arrow_reader() {
    let good = batch("1", vec![1]);
    let mut bad = vec![255; 8];
    bad[4..].copy_from_slice(&u32::MAX.to_le_bytes());
    let input = DataBatch::new(good.metadata, bad, 1024).unwrap();
    assert_eq!(
        decode_market(&input, 2, 1024).unwrap_err().code,
        ErrorCode::ResourceLimit
    );
}

fn rewrite_buffer(
    input: &DataBatch,
    index: usize,
    change: impl Fn(i64, i64) -> (i64, i64),
) -> DataBatch {
    let payload = input.payload();
    let mut position = 0;
    loop {
        let mut size = u32::from_le_bytes(payload[position..position + 4].try_into().unwrap());
        position += 4;
        if size == u32::MAX {
            size = u32::from_le_bytes(payload[position..position + 4].try_into().unwrap());
            position += 4;
        }
        assert_ne!(size, 0);
        let message =
            arrow_ipc::root_as_message(&payload[position..position + size as usize]).unwrap();
        if let Some(record) = message.header_as_record_batch() {
            let buffer = record.buffers().unwrap().get(index);
            let location = buffer.0.as_ptr() as usize - payload.as_ptr() as usize;
            let (offset, length) = change(buffer.offset(), buffer.length());
            let mut rewritten = payload.to_vec();
            rewritten[location..location + 16]
                .copy_from_slice(&arrow_ipc::Buffer::new(offset, length).0);
            return DataBatch::new(input.metadata.clone(), rewritten, 64 * 1024 * 1024).unwrap();
        }
        position += size as usize + message.bodyLength() as usize;
    }
}

#[test]
fn overlapping_unaligned_and_inflated_arrow_buffers_fail_before_decoding() {
    let good = batch("1", vec![1]);
    assert!(decode_market(&good, 2, 64 * 1024 * 1024).is_ok());
    for bad in [
        rewrite_buffer(&good, 1, |offset, length| (offset + 1, length)),
        rewrite_buffer(&good, 4, |_, length| (0, length)),
        // Still fits the IPC body/padding; not a valid (rows+1)*4 offset array.
        rewrite_buffer(&good, 1, |offset, length| (offset, length + 1)),
        // A tiny row count cannot advertise an oversized fixed-width buffer.
        rewrite_buffer(&good, 37, |offset, _| (offset, 256)),
    ] {
        assert_eq!(
            decode_market(&bad, 2, 64 * 1024 * 1024).unwrap_err().code,
            ErrorCode::InvalidContract
        );
    }
}
struct Source {
    queue: VecDeque<DataBatch>,
    closed: Arc<AtomicBool>,
}
impl BatchSource for Source {
    fn next_batch(&mut self, _: usize) -> QfResult<Option<DataBatch>> {
        Ok(self.queue.pop_front())
    }
    fn close(&mut self) -> QfResult<()> {
        self.closed.store(true, Ordering::Relaxed);
        Ok(())
    }
}
#[test]
fn event_source_error_sticky_and_releases_owner() {
    let closed = Arc::new(AtomicBool::new(false));
    let source = Source {
        queue: VecDeque::from([batch("1", vec![2]), batch("1", vec![1])]),
        closed: closed.clone(),
    };
    let mut stream = ArrowEventSource::new(source, 64 * 1024 * 1024).unwrap();
    assert!(stream.next_chunk(1).unwrap().is_some());
    let error = stream.next_chunk(1).unwrap_err();
    assert_eq!(error.code, ErrorCode::InvalidContract);
    assert_eq!(stream.next_chunk(1).unwrap_err(), error);
    assert!(closed.load(Ordering::Relaxed));
}
fn scope() -> RunScope {
    RunScope {
        run_id: "run".into(),
        universe: vec![SecurityKey::new("A.SH").unwrap()],
    }
}
#[test]
fn ipc_length_limit_rejected_before_payload_allocated() {
    let (client, mut server) = UnixStream::pair().unwrap();
    let sender = std::thread::spawn(move || {
        let mut prefix = [0; 16];
        server.read_exact(&mut prefix).unwrap();
        let len = u32::from_be_bytes(prefix[4..8].try_into().unwrap()) as usize;
        let mut head = vec![0; len];
        server.read_exact(&mut head).unwrap();
        prefix[..4].copy_from_slice(b"QFD4");
        prefix[4..8].copy_from_slice(&1_u32.to_be_bytes());
        prefix[8..].copy_from_slice(&u64::MAX.to_be_bytes());
        server.write_all(&prefix).unwrap();
    });
    let mut gateway = IpcGateway::new(
        client,
        scope(),
        4096,
        Duration::from_secs(2),
        Arc::new(AtomicBool::new(false)),
    )
    .unwrap();
    assert_eq!(
        gateway.open(&scope()).unwrap_err().code,
        ErrorCode::ResourceLimit
    );
    sender.join().unwrap();
}
#[test]
fn ipc_cancellation_interrupts_read_and_disconnect_is_terminal() {
    let (client, _server) = UnixStream::pair().unwrap();
    let cancel = Arc::new(AtomicBool::new(false));
    let flag = cancel.clone();
    let timer = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(50));
        flag.store(true, Ordering::Relaxed);
    });
    let mut gateway =
        IpcGateway::new(client, scope(), 4096, Duration::from_secs(2), cancel).unwrap();
    let began = Instant::now();
    let error = gateway.open(&scope()).unwrap_err();
    assert_eq!(error.code, ErrorCode::Cancelled);
    assert!(began.elapsed() < Duration::from_secs(1));
    assert_eq!(gateway.open(&scope()).unwrap_err(), error);
    timer.join().unwrap();
}

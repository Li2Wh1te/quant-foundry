//! Owned, uncompressed, bounded Arrow IPC -> exact D01 market events.
use super::{DataBatch, MAX_ARROW_BATCH_BYTES};
use crate::clock::EventSource;
use crate::types::keys::ChannelKey;
use crate::types::market::{Currency, MarketUnits, QuantityUnit};
use crate::types::{
    Bar, EventIdentity, EventKey, MarketEvent, Nanoseconds, Price, Quantity, QuoteTick,
    SecurityKey, Sequence, SessionKey, TradeTick,
};
use crate::{ErrorCode, QfError, QfResult};
use arrow_array::{Array, Int64Array, RecordBatch, StringArray, UInt64Array};
use arrow_ipc::{MessageHeader, reader::StreamReader};
use arrow_schema::{DataType, Schema};
use std::io::Cursor;

pub const SCHEMA_ID: &str = "qf.market.v1";
const TEXT: &[&str] = &[
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
const INTS: &[&str] = &[
    "time_ns",
    "interval_start_ns",
    "interval_end_ns",
    "quantity",
    "bid_quantity",
    "ask_quantity",
];
const SEQS: &[&str] = &["sequence", "stable_input_sequence"];
fn invalid() -> QfError {
    QfError::new(
        ErrorCode::InvalidContract,
        "arrow_input",
        "Arrow输入布局、身份或单位无效",
    )
}
fn limit() -> QfError {
    QfError::new(
        ErrorCode::ResourceLimit,
        "arrow_input",
        "Arrow输入超过解码预算",
    )
}

/// Bound flatbuffer lengths/body BEFORE Arrow allocates. No compression,
/// dictionaries or nested types; one schema and one data batch per credit.
fn preflight(payload: &[u8], max_rows: usize) -> QfResult<()> {
    let mut position = 0_usize;
    let mut schemas = 0;
    let mut batches = 0;
    loop {
        let word = payload.get(position..position + 4).ok_or_else(invalid)?;
        position += 4;
        let mut size = u32::from_le_bytes(word.try_into().map_err(|_| invalid())?);
        if size == u32::MAX {
            size = u32::from_le_bytes(
                payload
                    .get(position..position + 4)
                    .ok_or_else(invalid)?
                    .try_into()
                    .map_err(|_| invalid())?,
            );
            position += 4;
        }
        if size == 0 {
            if position != payload.len() || schemas != 1 || batches != 1 {
                return Err(invalid());
            }
            return Ok(());
        }
        if size as usize > crate::run::MAX_CONTROL_BYTES {
            return Err(limit());
        }
        let end = position.checked_add(size as usize).ok_or_else(limit)?;
        let message = arrow_ipc::root_as_message(payload.get(position..end).ok_or_else(invalid)?)
            .map_err(|_| invalid())?;
        position = end;
        let body = usize::try_from(message.bodyLength()).map_err(|_| invalid())?;
        if body > payload.len().saturating_sub(position) {
            return Err(invalid());
        }
        match message.header_type() {
            MessageHeader::Schema if schemas == 0 && batches == 0 && body == 0 => {
                let schema = message.header_as_schema().ok_or_else(invalid)?;
                if schema.endianness() != arrow_ipc::Endianness::Little {
                    return Err(invalid());
                }
                let fields = schema.fields().ok_or_else(invalid)?;
                if fields.len() != TEXT.len() + INTS.len() + SEQS.len() {
                    return Err(invalid());
                }
                for (index, f) in fields.iter().enumerate() {
                    if f.dictionary().is_some() || f.children().is_some_and(|c| !c.is_empty()) {
                        return Err(invalid());
                    }
                    let name = TEXT
                        .get(index)
                        .or_else(|| INTS.get(index - TEXT.len()))
                        .or_else(|| SEQS.get(index - TEXT.len() - INTS.len()));
                    if f.name() != name.copied() {
                        return Err(invalid());
                    }
                    if index < TEXT.len() {
                        if f.type_type() != arrow_ipc::Type::Utf8 {
                            return Err(invalid());
                        }
                    } else {
                        let integer = f.type_as_int().ok_or_else(invalid)?;
                        if integer.bitWidth() != 64
                            || integer.is_signed() != (index < TEXT.len() + INTS.len())
                        {
                            return Err(invalid());
                        }
                    }
                }
                schemas += 1;
            }
            MessageHeader::RecordBatch if schemas == 1 && batches == 0 => {
                let batch = message.header_as_record_batch().ok_or_else(invalid)?;
                let rows = usize::try_from(batch.length()).map_err(|_| invalid())?;
                if rows > max_rows
                    || batch.compression().is_some()
                    || batch.variadicBufferCounts().is_some_and(|v| !v.is_empty())
                {
                    return Err(limit());
                }
                let nodes = batch.nodes().ok_or_else(invalid)?;
                if nodes.len() != TEXT.len() + INTS.len() + SEQS.len() {
                    return Err(invalid());
                }
                for node in nodes {
                    if node.length() != batch.length()
                        || node.null_count() < 0
                        || node.null_count() > node.length()
                    {
                        return Err(invalid());
                    }
                }
                let buffers = batch.buffers().ok_or_else(invalid)?;
                if buffers.len() != 3 * TEXT.len() + 2 * (INTS.len() + SEQS.len()) {
                    return Err(invalid());
                }
                let mut previous_end = 0;
                for b in buffers {
                    let offset = usize::try_from(b.offset()).map_err(|_| invalid())?;
                    let length = usize::try_from(b.length()).map_err(|_| invalid())?;
                    let end = offset.checked_add(length).ok_or_else(limit)?;
                    // Arrow's default reader copies misaligned buffers before
                    // validating array sizes. Disallow overlap/alignment tricks
                    // here so one body cannot cause many oversized copies.
                    if offset % 8 != 0 || offset < previous_end || end > body {
                        return Err(invalid());
                    }
                    previous_end = end;
                }
                let bitmap = rows.div_ceil(8);
                let mut buffer = 0;
                for column in 0..nodes.len() {
                    let validity =
                        usize::try_from(buffers.get(buffer).length()).map_err(|_| invalid())?;
                    if (nodes.get(column).null_count() > 0 && validity < bitmap)
                        || (validity != 0 && (validity < bitmap || validity > bitmap + 7))
                    {
                        return Err(invalid());
                    }
                    let values =
                        usize::try_from(buffers.get(buffer + 1).length()).map_err(|_| invalid())?;
                    if column < TEXT.len() {
                        if values != (rows + 1) * 4 {
                            return Err(invalid());
                        }
                        let strings = usize::try_from(buffers.get(buffer + 2).length())
                            .map_err(|_| invalid())?;
                        if strings > rows * 128 {
                            return Err(limit());
                        }
                        buffer += 3;
                    } else {
                        if values != rows * 8 {
                            return Err(invalid());
                        }
                        buffer += 2;
                    }
                }
                batches += 1;
            }
            _ => return Err(invalid()),
        }
        position = position.checked_add(body).ok_or_else(limit)?;
    }
}
fn schema_valid(schema: &Schema) -> bool {
    let meta = schema.metadata();
    for (key, value) in [
        ("schema_id", SCHEMA_ID),
        ("time_unit", "utc_nanoseconds"),
        ("exchange_timezone", "Asia/Shanghai"),
        ("price_currency", "CNY"),
        ("quantity_unit", "shares"),
        ("price_basis", "raw"),
    ] {
        if meta.get(key).map(String::as_str) != Some(value) {
            return false;
        }
    }
    schema.fields().len() == TEXT.len() + INTS.len() + SEQS.len()
        && TEXT.iter().all(|n| {
            schema
                .field_with_name(n)
                .is_ok_and(|f| f.data_type() == &DataType::Utf8)
        })
        && INTS.iter().all(|n| {
            schema
                .field_with_name(n)
                .is_ok_and(|f| f.data_type() == &DataType::Int64)
        })
        && SEQS.iter().all(|n| {
            schema
                .field_with_name(n)
                .is_ok_and(|f| f.data_type() == &DataType::UInt64)
        })
}
fn string<'a>(batch: &'a RecordBatch, name: &str, row: usize) -> QfResult<Option<&'a str>> {
    let array = batch
        .column_by_name(name)
        .ok_or_else(invalid)?
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(invalid)?;
    if array.is_null(row) {
        return Ok(None);
    }
    let value = array.value(row);
    if value.len() > 128 {
        return Err(limit());
    }
    Ok(Some(value))
}
fn required<'a>(batch: &'a RecordBatch, name: &str, row: usize) -> QfResult<&'a str> {
    string(batch, name, row)?.ok_or_else(invalid)
}
fn integer(batch: &RecordBatch, name: &str, row: usize) -> QfResult<Option<i64>> {
    let a = batch
        .column_by_name(name)
        .ok_or_else(invalid)?
        .as_any()
        .downcast_ref::<Int64Array>()
        .ok_or_else(invalid)?;
    Ok((!a.is_null(row)).then(|| a.value(row)))
}
fn sequence(batch: &RecordBatch, name: &str, row: usize) -> QfResult<Option<Sequence>> {
    let a = batch
        .column_by_name(name)
        .ok_or_else(invalid)?
        .as_any()
        .downcast_ref::<UInt64Array>()
        .ok_or_else(invalid)?;
    Ok((!a.is_null(row)).then(|| Sequence::new(a.value(row))))
}
fn price(batch: &RecordBatch, name: &str, row: usize) -> QfResult<Option<Price>> {
    string(batch, name, row)?.map(str::parse).transpose()
}
fn quantity(batch: &RecordBatch, name: &str, row: usize) -> QfResult<Option<Quantity>> {
    integer(batch, name, row)?.map(Quantity::new).transpose()
}

pub fn decode_market(
    batch: &DataBatch,
    max_rows: usize,
    byte_budget: usize,
) -> QfResult<Vec<MarketEvent>> {
    if max_rows == 0
        || max_rows > 10000
        || byte_budget == 0
        || byte_budget > MAX_ARROW_BATCH_BYTES
        || batch.payload().len() > byte_budget
        || batch.metadata.rows > max_rows as u64
    {
        return Err(limit());
    }
    if batch.metadata.schema_id != SCHEMA_ID {
        return Err(invalid());
    }
    preflight(batch.payload(), max_rows)?;
    let mut reader =
        StreamReader::try_new(Cursor::new(batch.payload()), None).map_err(|_| invalid())?;
    if !schema_valid(reader.schema().as_ref()) {
        return Err(invalid());
    }
    let input = reader.next().ok_or_else(invalid)?.map_err(|_| invalid())?;
    if input.num_rows() != batch.metadata.rows as usize || reader.next().is_some() {
        return Err(invalid());
    }
    if input.get_array_memory_size() > byte_budget {
        return Err(limit());
    }
    let mut result = Vec::with_capacity(input.num_rows());
    let mut previous = None;
    let mut bytes = 0_usize;
    for row in 0..input.num_rows() {
        let security = SecurityKey::new(required(&input, "security", row)?)?;
        let session = SessionKey::new(required(&input, "session", row)?)?;
        let identity = EventIdentity {
            source_session: SessionKey::new(required(&input, "source_session", row)?)?,
            channel: ChannelKey::new(required(&input, "channel", row)?)?,
            sequence: sequence(&input, "sequence", row)?,
            stable_input_sequence: sequence(&input, "stable_input_sequence", row)?
                .ok_or_else(invalid)?,
        };
        let time = Nanoseconds::new(integer(&input, "time_ns", row)?.ok_or_else(invalid)?);
        let units = MarketUnits {
            price_currency: Currency::CNY,
            quantity_unit: QuantityUnit::Shares,
        };
        let event = match required(&input, "kind", row)? {
            "bar" => MarketEvent::Bar(Bar {
                security,
                session,
                identity,
                interval_start_ns: Nanoseconds::new(
                    integer(&input, "interval_start_ns", row)?.ok_or_else(invalid)?,
                ),
                interval_end_ns: Nanoseconds::new(
                    integer(&input, "interval_end_ns", row)?.ok_or_else(invalid)?,
                ),
                open: price(&input, "open", row)?.ok_or_else(invalid)?,
                high: price(&input, "high", row)?.ok_or_else(invalid)?,
                low: price(&input, "low", row)?.ok_or_else(invalid)?,
                close: price(&input, "close", row)?.ok_or_else(invalid)?,
                quantity: quantity(&input, "quantity", row)?,
                units,
            }),
            "trade_tick" => MarketEvent::TradeTick(TradeTick {
                security,
                session,
                identity,
                time_ns: time,
                price: price(&input, "price", row)?.ok_or_else(invalid)?,
                quantity: quantity(&input, "quantity", row)?.ok_or_else(invalid)?,
                units,
            }),
            "quote_tick" => MarketEvent::QuoteTick(QuoteTick {
                security,
                session,
                identity,
                time_ns: time,
                bid: price(&input, "bid", row)?,
                ask: price(&input, "ask", row)?,
                bid_quantity: quantity(&input, "bid_quantity", row)?,
                ask_quantity: quantity(&input, "ask_quantity", row)?,
                units,
            }),
            _ => return Err(invalid()),
        };
        event.validate()?;
        let key = event.key();
        if key.time_ns != time
            || previous.as_ref().is_some_and(|p| p >= &key)
            || !batch
                .metadata
                .actual_scope
                .securities
                .contains(&key.security)
            || batch
                .metadata
                .actual_scope
                .start_ns
                .is_none_or(|s| time < s)
            || batch.metadata.actual_scope.end_ns.is_none_or(|e| time > e)
        {
            return Err(invalid());
        }
        previous = Some(key);
        bytes = bytes
            .checked_add(serde_json::to_vec(&event).map_err(|_| invalid())?.len())
            .ok_or_else(limit)?;
        if bytes > byte_budget {
            return Err(limit());
        }
        result.push(event);
    }
    Ok(result)
}

/// D04 pull seam. Each source owns at most one bounded payload; owners drop
/// after exact conversion, before the engine requests the next credit.
pub trait BatchSource {
    fn next_batch(&mut self, max_rows: usize) -> QfResult<Option<DataBatch>>;
    fn close(&mut self) -> QfResult<()>;
}
pub struct ArrowEventSource<S: BatchSource> {
    source: S,
    budget: usize,
    last: Option<EventKey>,
    eof: bool,
    failure: Option<QfError>,
}
impl<S: BatchSource> ArrowEventSource<S> {
    pub fn new(source: S, budget: usize) -> QfResult<Self> {
        if budget == 0 || budget > MAX_ARROW_BATCH_BYTES {
            return Err(limit());
        }
        Ok(Self {
            source,
            budget,
            last: None,
            eof: false,
            failure: None,
        })
    }
    pub fn close(&mut self) -> QfResult<()> {
        self.eof = true;
        self.source.close()
    }
    fn read(&mut self, max_events: usize) -> QfResult<Option<Vec<MarketEvent>>> {
        let mut empty_batches = 0;
        loop {
            let Some(batch) = self.source.next_batch(max_events)? else {
                self.eof = true;
                return Ok(None);
            };
            let events = decode_market(&batch, max_events, self.budget)?;
            if events.is_empty() {
                empty_batches += 1;
                if empty_batches > 16 {
                    return Err(limit());
                }
                continue;
            }
            if self
                .last
                .as_ref()
                .is_some_and(|last| last >= &events[0].key())
            {
                return Err(invalid());
            }
            self.last = events.last().map(MarketEvent::key);
            return Ok(Some(events));
        }
    }
}
impl<S: BatchSource> EventSource for ArrowEventSource<S> {
    fn next_chunk(&mut self, max_events: usize) -> QfResult<Option<Vec<MarketEvent>>> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        if self.eof {
            return Ok(None);
        }
        let result = self.read(max_events);
        if let Err(error) = &result {
            self.failure = Some(error.clone());
            let _ = self.source.close();
        }
        result
    }
}

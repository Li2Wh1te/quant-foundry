use crate::accounting::PositionView;
use crate::analysis::EquityPoint;
use crate::matching::Fill;
use crate::orders::Order;
use crate::run::{MAX_CONTROL_BYTES, RunOutcome};
use crate::types::{ExactDecimal, Nanoseconds, Sequence};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Deserializer, Serialize, de};
use std::collections::BTreeMap;

pub mod credit;
pub mod file;

pub const MAX_PAGE_ITEMS: usize = 10_000;
pub const MAX_CURSOR_BYTES: usize = 4096;
pub fn validate_page(count: usize, cursor: Option<&str>) -> QfResult<()> {
    if count > MAX_PAGE_ITEMS || cursor.is_some_and(|v| v.is_empty() || v.len() > MAX_CURSOR_BYTES)
    {
        return Err(QfError::new(
            ErrorCode::ResourceLimit,
            "result_page",
            "结果页或游标超过边界",
        ));
    }
    Ok(())
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResultKind {
    Summary,
    Equity,
    Orders,
    Trades,
    Positions,
    Records,
    Logs,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
#[serde(transparent)]
pub struct FiniteStatistic(f64);
impl FiniteStatistic {
    pub fn new(value: f64) -> QfResult<Self> {
        if !value.is_finite() {
            return Err(QfError::new(
                ErrorCode::NumericRangeUnsupported,
                "record",
                "统计值必须有限",
            ));
        }
        Ok(Self(value))
    }
    pub fn get(self) -> f64 {
        self.0
    }
}
impl<'de> Deserialize<'de> for FiniteStatistic {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::new(f64::deserialize(d)?).map_err(de::Error::custom)
    }
}
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(
    tag = "kind",
    content = "value",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum RecordValue {
    Decimal(ExactDecimal),
    Integer(i64),
    Text(String),
    Statistic(FiniteStatistic),
}
impl<'de> Deserialize<'de> for RecordValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            kind: String,
            value: Box<serde_json::value::RawValue>,
        }
        let wire = Wire::deserialize(deserializer)?;
        macro_rules! decode {
            ($variant:ident) => {
                serde_json::from_str(wire.value.get())
                    .map(Self::$variant)
                    .map_err(de::Error::custom)
            };
        }
        match wire.kind.as_str() {
            "decimal" => decode!(Decimal),
            "integer" => decode!(Integer),
            "text" => decode!(Text),
            "statistic" => decode!(Statistic),
            _ => Err(de::Error::custom("unknown record value kind")),
        }
    }
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UserRecord {
    pub time_ns: Nanoseconds,
    pub values: BTreeMap<String, RecordValue>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LogLevel {
    Info,
    Warning,
    Error,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LogRecord {
    pub time_ns: Nanoseconds,
    pub level: LogLevel,
    pub message: String,
    pub truncated: bool,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CorporateActionEvent {
    pub time_ns: Nanoseconds,
    pub session: crate::types::SessionKey,
    pub effect: crate::accounting::CorporateEffect,
}
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(
    tag = "kind",
    content = "record",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum ResultRecord {
    Summary(Box<crate::analysis::PerformanceSummary>),
    ExecutionModel(crate::matching::ExecutionModelDescription),
    Equity(EquityPoint),
    Order(Order),
    Trade(Fill),
    Position(PositionView),
    CorporateAction(CorporateActionEvent),
    Record(UserRecord),
    Log(LogRecord),
}
impl<'de> Deserialize<'de> for ResultRecord {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        // serde's buffered adjacently-tagged enum turns arbitrary_precision
        // JSON numbers into private maps before FiniteStatistic sees them.
        // Keep the raw bounded record, then decode its typed fields directly.
        // In particular, a literal private-number map must not become a float.
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            kind: String,
            record: Box<serde_json::value::RawValue>,
        }
        let wire = Wire::deserialize(deserializer)?;
        macro_rules! decode {
            ($variant:ident) => {
                serde_json::from_str(wire.record.get())
                    .map(Self::$variant)
                    .map_err(de::Error::custom)
            };
        }
        match wire.kind.as_str() {
            "summary" => decode!(Summary),
            "execution_model" => decode!(ExecutionModel),
            "equity" => decode!(Equity),
            "order" => decode!(Order),
            "trade" => decode!(Trade),
            "position" => decode!(Position),
            "corporate_action" => decode!(CorporateAction),
            "record" => decode!(Record),
            "log" => decode!(Log),
            _ => Err(de::Error::custom("unknown result record kind")),
        }
    }
}

/// A credit grant comes from the trusted writer. A producer must not send beyond
/// it; close/cancel must interrupt a blocked credit wait in D12/D13.
#[derive(Debug, Clone, Copy)]
pub struct SinkCredit {
    pub max_records: usize,
    pub max_bytes: usize,
}
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ResultBatch {
    first_sequence: Sequence,
    records: Vec<ResultRecord>,
}
impl ResultBatch {
    pub fn new(
        first_sequence: Sequence,
        records: Vec<ResultRecord>,
        credit: &SinkCredit,
    ) -> QfResult<Self> {
        if records.is_empty()
            || records.len() > credit.max_records
            || records.len() > MAX_PAGE_ITEMS
            || credit.max_bytes == 0
            || credit.max_bytes > MAX_CONTROL_BYTES
            || first_sequence
                .get()
                .checked_add(records.len() as u64 - 1)
                .is_none()
        {
            return Err(QfError::new(
                ErrorCode::ResultBudgetExceeded,
                "result_sink",
                "结果批次超过信用或序号范围",
            ));
        }
        let batch = Self {
            first_sequence,
            records,
        };
        if encode_bounded(&batch, credit.max_bytes)?.len() > credit.max_bytes {
            return Err(QfError::new(
                ErrorCode::ResultBudgetExceeded,
                "result_sink",
                "结果批次超过消息字节预算",
            ));
        }
        Ok(batch)
    }
    pub fn decode(input: &[u8], credit: &SinkCredit) -> QfResult<Self> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            first_sequence: Sequence,
            records: Vec<ResultRecord>,
        }
        if input.len() > credit.max_bytes || input.len() > MAX_CONTROL_BYTES {
            return Err(QfError::new(
                ErrorCode::ResultBudgetExceeded,
                "result_sink",
                "结果消息超过信用",
            ));
        }
        let wire: Wire = serde_json::from_slice(input)
            .map_err(|_| QfError::new(ErrorCode::InvalidContract, "result_sink", "结果消息无效"))?;
        Self::new(wire.first_sequence, wire.records, credit)
    }
    pub fn first_sequence(&self) -> Sequence {
        self.first_sequence
    }
    pub fn records(&self) -> &[ResultRecord] {
        &self.records
    }
}
/// D13 provides the trusted persistence implementation. finalize must complete
/// required writes and coordinate dependency recheck before committing success.
pub trait ResultSink {
    fn credit(&mut self) -> QfResult<SinkCredit>;
    fn write(&mut self, batch: &ResultBatch) -> QfResult<()>;
    fn finalize(&mut self, outcome: &RunOutcome) -> QfResult<()>;
    fn abort(&mut self, error: &QfError) -> QfResult<()>;
}

/// Bound allocations as well as the serialized result message.
pub(crate) fn encode_bounded(value: &impl Serialize, maximum: usize) -> QfResult<Vec<u8>> {
    struct Buffer {
        bytes: Vec<u8>,
        maximum: usize,
    }
    impl std::io::Write for Buffer {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            if bytes.len() > self.maximum.saturating_sub(self.bytes.len()) {
                return Err(std::io::Error::other("result budget"));
            }
            self.bytes.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut buffer = Buffer {
        bytes: Vec::with_capacity(maximum.min(4096)),
        maximum,
    };
    serde_json::to_writer(&mut buffer, value).map_err(|_| {
        QfError::new(
            ErrorCode::ResultBudgetExceeded,
            "result_sink",
            "结果编码超过字节预算",
        )
    })?;
    Ok(buffer.bytes)
}

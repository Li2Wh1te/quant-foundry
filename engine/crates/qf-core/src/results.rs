use crate::accounting::PositionView;
use crate::analysis::EquityPoint;
use crate::matching::Fill;
use crate::orders::Order;
use crate::run::{MAX_CONTROL_BYTES, RunOutcome};
use crate::types::{ExactDecimal, Nanoseconds, Sequence};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Deserializer, Serialize, de};
use std::collections::BTreeMap;

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
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
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
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(
    tag = "kind",
    content = "record",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum ResultRecord {
    Equity(EquityPoint),
    Order(Order),
    Trade(Fill),
    Position(PositionView),
    Record(UserRecord),
    Log(LogRecord),
}

/// A credit grant comes from the trusted writer. A producer must not send beyond
/// it; close/cancel must interrupt a blocked credit wait in D12/D13.
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
        if serde_json::to_vec(&batch)
            .map_err(|_| QfError::new(ErrorCode::InvalidContract, "result_sink", "结果序列化无效"))?
            .len()
            > credit.max_bytes
        {
            return Err(QfError::new(
                ErrorCode::ResultBudgetExceeded,
                "result_sink",
                "结果批次超过消息字节预算",
            ));
        }
        Ok(batch)
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

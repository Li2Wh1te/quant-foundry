use crate::run::Frequency;
use crate::types::{Nanoseconds, SecurityKey};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};

pub mod arrow;
pub mod ipc;
pub mod views;

pub const MAX_ARROW_BATCH_BYTES: usize = 64 * 1024 * 1024;
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Adjustment {
    None,
    Pre,
    Post,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataRequest {
    pub securities: Vec<SecurityKey>,
    pub fields: Vec<String>,
    pub frequency: Frequency,
    pub start_ns: Option<Nanoseconds>,
    pub end_ns: Nanoseconds,
    pub count_per_security: Option<u32>,
    pub adjustment: Adjustment,
}
impl DataRequest {
    pub fn validate(&self, visible_through: Nanoseconds) -> QfResult<()> {
        if self.end_ns > visible_through {
            return Err(QfError::new(
                ErrorCode::LookaheadForbidden,
                "data_request",
                "查询终点晚于策略当前可见时点",
            ));
        }
        if self.start_ns.is_some() == self.count_per_security.is_some()
            || self.start_ns.is_some_and(|s| s > self.end_ns)
            || self.count_per_security == Some(0)
            || self.securities.len() > 10_000
            || self.fields.is_empty()
            || self.fields.len() > 128
            || self
                .fields
                .iter()
                .any(|f| crate::types::keys::label(f, 128).is_err())
        {
            return Err(QfError::new(
                ErrorCode::InvalidContract,
                "data_request",
                "请求范围或列边界无效",
            ));
        }
        Ok(())
    }
}
/// Current dependency/readability identifiers; never market snapshots.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DependencyState {
    pub dataset: String,
    pub generation: String,
    pub readability: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DependencyContext {
    pub scope_id: String,
    pub dependencies: Vec<DependencyState>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DependencyCheck {
    Unchanged,
    DataChanged,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunScope {
    pub run_id: String,
    pub universe: Vec<SecurityKey>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActualScope {
    pub start_ns: Option<Nanoseconds>,
    pub end_ns: Option<Nanoseconds>,
    pub securities: Vec<SecurityKey>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BatchMetadata {
    pub schema_id: String,
    pub time_unit: TimeUnit,
    pub quantity_unit: crate::types::market::QuantityUnit,
    pub price_currency: crate::types::market::Currency,
    pub rows: u64,
    pub actual_scope: ActualScope,
    pub limitations: Vec<String>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TimeUnit {
    UtcNanoseconds,
}
/// D04 owns Arrow encoding/lifetimes. This container bounds each IPC payload and
/// exposes no database connection, vendor token or user-selected path.
pub struct DataBatch {
    pub metadata: BatchMetadata,
    payload: Vec<u8>,
}
impl DataBatch {
    pub fn new(metadata: BatchMetadata, payload: Vec<u8>, budget: usize) -> QfResult<Self> {
        crate::types::keys::label(&metadata.schema_id, 128)?;
        if serde_json::to_vec(&metadata)
            .map_err(|_| {
                QfError::new(ErrorCode::InvalidContract, "data_batch", "批次头序列化无效")
            })?
            .len()
            > crate::run::MAX_CONTROL_BYTES
        {
            return Err(QfError::new(
                ErrorCode::ResourceLimit,
                "data_batch",
                "批次头超过消息预算",
            ));
        }
        if budget == 0 || budget > MAX_ARROW_BATCH_BYTES || payload.len() > budget {
            return Err(QfError::new(
                ErrorCode::ResourceLimit,
                "data_batch",
                "Arrow批次超过预算",
            ));
        }
        Ok(Self { metadata, payload })
    }
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }
}
pub trait DataGateway {
    fn open(&mut self, scope: &RunScope) -> QfResult<DependencyContext>;
    fn read(
        &mut self,
        request: &DataRequest,
        context: &mut DependencyContext,
    ) -> QfResult<DataBatch>;
    fn check(&mut self, context: &DependencyContext) -> QfResult<DependencyCheck>;
    fn close(&mut self, context: DependencyContext) -> QfResult<()>;
}

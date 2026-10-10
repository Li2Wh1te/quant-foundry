use crate::rules::{CostOverrides, FeeConfig};
use crate::types::keys::label;
use crate::types::{ExactDecimal, Nanoseconds, SecurityKey};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Deserializer, Serialize, de};
use serde_json::{Map, Value};
use std::collections::BTreeSet;
use std::ops::Deref;

mod parameters;

pub const API_SCHEMA: &str = "qf.backtest.v2";
pub const MAX_CONTROL_BYTES: usize = 1024 * 1024;
pub const MAX_PARAMETER_BYTES: usize = 64 * 1024;
pub const MAX_PARAMETER_DEPTH: usize = 8;
pub const MAX_BATCH_RUNS: usize = 256;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Frequency {
    #[serde(rename = "1d")]
    Day,
    #[serde(rename = "1m")]
    Minute,
    #[serde(rename = "5m")]
    FiveMinutes,
    #[serde(rename = "15m")]
    FifteenMinutes,
    #[serde(rename = "30m")]
    ThirtyMinutes,
    #[serde(rename = "60m")]
    Hour,
    #[serde(rename = "tick")]
    Tick,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionModel {
    BarNextIntervalV1,
    TradeTickV1,
    QuoteTickV1,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ResultSampling {
    #[default]
    SessionClose,
    BarClose,
}

fn currency() -> String {
    "CNY".into()
}
fn participation() -> ExactDecimal {
    "0.10".parse().expect("constant decimal")
}
fn annualization() -> u16 {
    252
}

/// Shape for builders. Use RunConfig::new before accepting/queueing a run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunConfigFields {
    pub api_schema: String,
    pub strategy_revision_id: String,
    pub account_id: String,
    pub initial_cash: ExactDecimal,
    pub start: String,
    pub end: String,
    pub frequency: Frequency,
    pub universe: Vec<SecurityKey>,
    #[serde(deserialize_with = "parameters::deserialize")]
    pub parameters: Map<String, Value>,
    pub execution_model: ExecutionModel,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub benchmark: Option<String>,
    #[serde(default = "currency")]
    pub currency: String,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "crate::types::optional_non_null"
    )]
    pub reference_calendar: Option<String>,
    #[serde(default)]
    pub seed: u32,
    #[serde(
        default = "participation",
        deserialize_with = "crate::types::numeric::nonnegative_decimal"
    )]
    pub participation_rate: ExactDecimal,
    #[serde(
        default = "zero",
        deserialize_with = "crate::types::numeric::nonnegative_decimal"
    )]
    pub slippage_bps: ExactDecimal,
    #[serde(default = "zero")]
    pub risk_free_rate: ExactDecimal,
    #[serde(default = "annualization")]
    pub annualization_sessions: u16,
    #[serde(default)]
    pub result_sampling: ResultSampling,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "crate::types::optional_non_null"
    )]
    pub cost_overrides: Option<CostOverrides>,
}
fn zero() -> ExactDecimal {
    ExactDecimal::ZERO
}

/// Validated immutable configuration. Authentication/rules/capabilities are D13.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct RunConfig(RunConfigFields);
impl Deref for RunConfig {
    type Target = RunConfigFields;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl<'de> Deserialize<'de> for RunConfig {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::new(RunConfigFields::deserialize(d)?).map_err(de::Error::custom)
    }
}

pub fn config_error() -> QfError {
    QfError::new(
        ErrorCode::InvalidRunConfig,
        "run_config",
        "运行配置格式、数值、日期或资源边界无效",
    )
}

/// Strict Gregorian date without relying on a locale or Python datetime conversion.
pub(crate) fn valid_date(value: &str) -> bool {
    let b = value.as_bytes();
    if b.len() != 10
        || b[4] != b'-'
        || b[7] != b'-'
        || b.iter()
            .enumerate()
            .any(|(i, c)| i != 4 && i != 7 && !c.is_ascii_digit())
    {
        return false;
    }
    let year: u32 = value[0..4].parse().unwrap_or(0);
    let month: u32 = value[5..7].parse().unwrap_or(0);
    let day: u32 = value[8..10].parse().unwrap_or(0);
    let leap = year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let days = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => 0,
    };
    year > 0 && day > 0 && day <= days
}

fn depth(value: &Value) -> usize {
    match value {
        Value::Object(m) => 1 + m.values().map(depth).max().unwrap_or(0),
        Value::Array(a) => 1 + a.iter().map(depth).max().unwrap_or(0),
        _ => 0,
    }
}

fn finite_json(value: &Value) -> bool {
    match value {
        // Integer tokens are exact and finite even beyond f64's range. JSON
        // real numbers must remain finite at the Python float boundary.
        Value::Number(n) => {
            !n.as_str().contains(['.', 'e', 'E']) || n.as_f64().is_some_and(f64::is_finite)
        }
        Value::Object(m) => m.values().all(finite_json),
        Value::Array(a) => a.iter().all(finite_json),
        _ => true,
    }
}

impl RunConfig {
    pub fn new(mut fields: RunConfigFields) -> QfResult<Self> {
        let bad = || Err(config_error());
        if fields.api_schema != API_SCHEMA
            || fields.currency != "CNY"
            || label(&fields.strategy_revision_id, 128).is_err()
            || label(&fields.account_id, 128).is_err()
            || !valid_date(&fields.start)
            || !valid_date(&fields.end)
            || fields.start > fields.end
            || fields.initial_cash <= ExactDecimal::ZERO
            || fields.seed > 2_147_483_647
            || fields.participation_rate <= ExactDecimal::ZERO
            || fields.participation_rate > ExactDecimal::ONE
            || fields.slippage_bps.is_negative()
            || fields.slippage_bps > ExactDecimal::from_integer(10_000)
            || fields.risk_free_rate <= ExactDecimal::from_integer(-1)
            || !(1..=366).contains(&fields.annualization_sessions)
        {
            return bad();
        }
        if fields
            .reference_calendar
            .as_ref()
            .is_some_and(|v| label(v, 128).is_err())
            || fields
                .benchmark
                .as_ref()
                .is_some_and(|v| label(v, 128).is_err())
        {
            return bad();
        }
        if fields.universe.is_empty()
            || fields.universe.len() > 10_000
            || fields.universe.iter().collect::<BTreeSet<_>>().len() != fields.universe.len()
        {
            return bad();
        }
        let params = Value::Object(fields.parameters.clone());
        if fields.parameters.len() > 128
            || depth(&params) > MAX_PARAMETER_DEPTH
            || !finite_json(&params)
            || serde_json::to_vec(&params)
                .map_err(|_| config_error())?
                .len()
                > MAX_PARAMETER_BYTES
        {
            return bad();
        }
        if let Some(costs) = &fields.cost_overrides {
            costs.validate().map_err(|_| config_error())?;
        }
        let tick_model = matches!(
            fields.execution_model,
            ExecutionModel::TradeTickV1 | ExecutionModel::QuoteTickV1
        );
        if (fields.frequency == Frequency::Tick) != tick_model
            || (tick_model && fields.result_sampling != ResultSampling::SessionClose)
        {
            return bad();
        }
        // Universe is a set; sorted canonical configuration supports idempotency.
        fields.universe.sort();
        Ok(Self(fields))
    }

    pub fn from_json(input: &str) -> QfResult<Self> {
        if input.len() > MAX_CONTROL_BYTES {
            return Err(config_error());
        }
        serde_json::from_str(input).map_err(|_| config_error())
    }

    pub fn normalized_json(&self) -> QfResult<String> {
        let mut value = serde_json::to_value(self).map_err(|_| config_error())?;
        for (field, decimal) in [
            ("initial_cash", self.initial_cash),
            ("participation_rate", self.participation_rate),
            ("slippage_bps", self.slippage_bps),
            ("risk_free_rate", self.risk_free_rate),
        ] {
            value[field] = Value::String(decimal.canonical());
        }
        if let Some(cost) = &self.cost_overrides {
            if let Some(rate) = cost.commission_rate {
                value["cost_overrides"]["commission_rate"] = rate.canonical().into();
            }
            if let Some(min) = cost.minimum_commission {
                value["cost_overrides"]["minimum_commission"] = min.canonical().into();
            }
        }
        serde_json::to_string(&value).map_err(|_| config_error())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BatchRequest {
    pub configs: Vec<RunConfig>,
}
impl BatchRequest {
    pub fn validate(&self) -> QfResult<()> {
        if self.configs.is_empty() || self.configs.len() > MAX_BATCH_RUNS {
            return Err(config_error());
        }
        let signature = |config: &RunConfig| -> QfResult<Value> {
            let mut value: Value =
                serde_json::from_str(&config.normalized_json()?).map_err(|_| config_error())?;
            value
                .as_object_mut()
                .ok_or_else(config_error)?
                .remove("parameters");
            Ok(value)
        };
        let first = signature(&self.configs[0])?;
        for config in &self.configs[1..] {
            if signature(config)? != first {
                return Err(config_error());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    Queued,
    Starting,
    Running,
    Succeeded,
    Failed,
    Cancelled,
}
impl RunStatus {
    pub fn terminal(self) -> bool {
        matches!(self, Self::Succeeded | Self::Failed | Self::Cancelled)
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunSummary {
    pub run_id: String,
    pub status: RunStatus,
    pub cancel_requested: bool,
    pub progress_completed: u64,
    pub progress_total: Option<u64>,
    pub partial: bool,
    pub error: Option<QfError>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum RunOutcome {
    Succeeded { result_id: String },
    Failed { error: QfError, partial: bool },
    Cancelled { partial: bool },
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreflightResponse {
    pub errors: Vec<QfError>,
    pub warnings: Vec<String>,
    pub normalized_config: Option<RunConfig>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AcceptedRunConfig {
    pub config: RunConfig,
    pub fee_config: FeeConfig,
    pub rule_reference: String,
}

/// Internal current scheduling state; never exposed as a market history version.
pub struct RunClaim {
    pub run_id: String,
    pub token: String,
    pub fence: u64,
    pub lease_until: Nanoseconds,
}
pub struct ClaimRequest {
    pub worker_id: String,
    pub now: Nanoseconds,
    pub lease_until: Nanoseconds,
}

/// D13 implements atomically with owner isolation, idempotency and stale fences.
/// commit_outcome rejects stale claims; terminal winners cannot be overwritten.
pub trait RunRepository {
    fn enqueue(
        &mut self,
        owner: &str,
        config: &AcceptedRunConfig,
        idempotency_key: &str,
    ) -> QfResult<RunSummary>;
    fn status(&self, owner: &str, run_id: &str) -> QfResult<RunSummary>;
    fn claim(&mut self, request: &ClaimRequest) -> QfResult<Option<RunClaim>>;
    fn heartbeat(&mut self, claim: &RunClaim, lease_until: Nanoseconds) -> QfResult<()>;
    fn request_cancel(&mut self, owner: &str, run_id: &str) -> QfResult<RunSummary>;
    fn commit_outcome(&mut self, claim: &RunClaim, outcome: &RunOutcome) -> QfResult<RunSummary>;
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Capabilities {
    pub api_schema: String,
    pub contract_version: String,
    pub implemented: Vec<String>,
    pub execution_models: Vec<ExecutionModel>,
    pub frequencies: Vec<Frequency>,
    pub production_data_available: bool,
}
pub fn capabilities() -> Capabilities {
    Capabilities {
        api_schema: API_SCHEMA.into(),
        contract_version: "1.1".into(),
        implemented: vec!["checked_numeric".into(), "shared_contracts".into()],
        execution_models: vec![],
        frequencies: vec![],
        production_data_available: false,
    }
}

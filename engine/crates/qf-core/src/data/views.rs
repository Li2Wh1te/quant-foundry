//! D05 callback-scoped, point-in-time research views; D11 must not replace this.
//! Owned bounded values may outlive a callback; a live view never may.
use crate::clock::{CalendarSession, Visibility};
use crate::data::arrow::{self, integer, required, sequence, string};
use crate::data::{Adjustment, DataRequest, MAX_ARROW_BATCH_BYTES};
use crate::run::Frequency;
use crate::types::keys::ChannelKey;
use crate::types::market::EventPhase;
use crate::types::{
    EventIdentity, EventKey, ExactDecimal, Nanoseconds, RoundingPolicy, SecurityKey, SessionKey,
};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

pub const RESEARCH_SCHEMA_ID: &str = "qf.research.v1";
pub const RESEARCH_TEXT: &[&str] = &[
    "security",
    "instrument_id",
    "record_id",
    "field",
    "value_kind",
    "value",
    "unit",
    "report_period",
];
pub const RESEARCH_INTS: &[&str] = &["time_ns", "effective_from_ns", "effective_until_ns"];
fn invalid(message: &str) -> QfError {
    QfError::new(ErrorCode::InvalidContract, "research_view", message)
}
fn unavailable(message: &str) -> QfError {
    QfError::new(ErrorCode::CapabilityUnavailable, "research_view", message)
}
fn limit() -> QfError {
    QfError::new(
        ErrorCode::ResourceLimit,
        "research_view",
        "研究窗口超过有界内存/行数预算",
    )
}
pub fn bounded_json<T: serde::de::DeserializeOwned>(input: &str) -> QfResult<T> {
    if input.len() > crate::run::MAX_CONTROL_BYTES {
        return Err(limit());
    }
    serde_json::from_str(input).map_err(|_| invalid("研究参数契约无效"))
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Boundary {
    pub now_ns: Nanoseconds,
    pub market_through: Option<EventKey>,
}
impl Boundary {
    pub fn visibility(&self) -> QfResult<Visibility> {
        if self
            .market_through
            .as_ref()
            .is_some_and(|k| k.time_ns > self.now_ns || k.phase != EventPhase::Market)
        {
            return Err(invalid("发布边界无效"));
        }
        Ok(Visibility {
            now_ns: self.now_ns,
            market_through: self.market_through.clone(),
        })
    }
}
#[derive(Debug, Clone)]
pub struct ViewLease(Arc<AtomicBool>);
impl Default for ViewLease {
    fn default() -> Self {
        Self(Arc::new(AtomicBool::new(true)))
    }
}
impl ViewLease {
    pub fn expire(&self) {
        self.0.store(false, Ordering::Release);
    }
    pub fn check(&self) -> QfResult<()> {
        if self.0.load(Ordering::Acquire) {
            Ok(())
        } else {
            Err(invalid("回调读取视图已过期"))
        }
    }
}
#[derive(Debug, Clone)]
pub struct PriceRow {
    pub key: EventKey,
    pub session: SessionKey,
    pub start_ns: Option<Nanoseconds>,
    pub kind: String,
    pub values: BTreeMap<String, Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ResearchRow {
    pub security: String,
    pub instrument_id: String,
    pub record_id: String,
    pub field: String,
    pub value_kind: String,
    pub value: Option<String>,
    pub unit: String,
    pub report_period: Option<String>,
    pub time_ns: Nanoseconds,
    pub effective_from_ns: Nanoseconds,
    pub effective_until_ns: Option<Nanoseconds>,
}
impl ResearchRow {
    fn validate(&self) -> QfResult<()> {
        for s in [
            &self.security,
            &self.instrument_id,
            &self.record_id,
            &self.field,
            &self.unit,
        ] {
            crate::types::keys::label(s, 128)?;
        }
        if self
            .effective_until_ns
            .is_some_and(|n| n <= self.effective_from_ns)
        {
            return Err(invalid("有效期无效"));
        }
        if let Some(period) = &self.report_period {
            period.parse::<crate::rules::date::RuleDate>()?;
        }
        if let Some(v) = &self.value {
            if v.len() > 128 {
                return Err(limit());
            }
            match self.value_kind.as_str() {
                "decimal" if exact_decimal_text(v) => {}
                "integer" if v.parse::<i64>().is_ok_and(|n| n.to_string() == *v) => {}
                "boolean" if matches!(v.as_str(), "true" | "false") => {}
                "text" => {}
                _ => return Err(invalid("研究值类型或精确表示无效")),
            }
        } else if !matches!(
            self.value_kind.as_str(),
            "decimal" | "integer" | "boolean" | "text"
        ) {
            return Err(invalid("未知研究值类型"));
        }
        Ok(())
    }
    fn visible(&self, at: Nanoseconds) -> bool {
        self.time_ns <= at
            && self.effective_from_ns <= at
            && self.effective_until_ns.is_none_or(|end| at < end)
    }
    fn typed(&self) -> Value {
        json!({"kind":self.value_kind,"value":self.value,"unit":self.unit})
    }
}
/// Research preserves up to 128 characters, including values outside the
/// execution Decimal range. No conversion of these strings to a Price.
pub fn exact_decimal_text(text: &str) -> bool {
    if text.is_empty() || text.len() > 128 {
        return false;
    }
    let text = text.strip_prefix('-').unwrap_or(text);
    let mut parts = text.split('.');
    let integer = parts.next().unwrap_or("");
    let fraction = parts.next();
    !integer.is_empty()
        && (integer == "0" || !integer.starts_with('0'))
        && integer.bytes().all(|b| b.is_ascii_digit())
        && fraction.is_none_or(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
        && parts.next().is_none()
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Predicate {
    pub field: String,
    pub op: String,
    pub value: Value,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchRequest {
    pub kind: String,
    pub securities: Vec<String>,
    pub fields: Vec<String>,
    pub as_of_ns: Nanoseconds,
    #[serde(default)]
    pub filters: Vec<Predicate>,
    #[serde(default)]
    pub order_by: Vec<String>,
    #[serde(default = "default_limit")]
    pub limit: usize,
}
fn default_limit() -> usize {
    10000
}
pub fn allowed_fields(kind: &str) -> &'static [&'static str] {
    match kind {
        "fundamentals" => &[
            "revenue",
            "net_profit",
            "total_assets",
            "total_liabilities",
            "eps",
            "roe",
        ],
        "valuation" => &[
            "pe_ratio",
            "pb_ratio",
            "market_cap",
            "circulating_market_cap",
        ],
        "industry" => &["industry_code", "industry_name", "taxonomy"],
        "instruments" => &[
            "name",
            "exchange",
            "asset_type",
            "currency",
            "listing_date",
            "end_date",
        ],
        "index_stocks" => &["member"],
        "adjustment" => &["factor"],
        "status" => &["halted"],
        "price" => &["open", "high", "low", "close", "quantity", "turnover"],
        _ => &[],
    }
}
impl ResearchRequest {
    pub fn validate(&self, now: Nanoseconds) -> QfResult<()> {
        if self.as_of_ns > now {
            return Err(QfError::new(
                ErrorCode::LookaheadForbidden,
                "research_view",
                "查询时点晚于模拟时间",
            ));
        }
        let allowed = allowed_fields(&self.kind);
        if allowed.is_empty()
            || self.fields.is_empty()
            || self.fields.iter().any(|f| !allowed.contains(&f.as_str()))
        {
            return Err(unavailable("字段不在研究白名单内"));
        }
        if self.limit == 0
            || self.limit > 10000
            || self.securities.len() > 10000
            || self.filters.len() > 32
            || self.order_by.len() > 16
        {
            return Err(limit());
        }
        if self.fields.iter().collect::<BTreeSet<_>>().len() != self.fields.len()
            || self.securities.iter().collect::<BTreeSet<_>>().len() != self.securities.len()
        {
            return Err(invalid("重复字段或标的"));
        }
        for p in &self.filters {
            if !allowed.contains(&p.field.as_str())
                || !self.fields.contains(&p.field)
                || !matches!(
                    p.op.as_str(),
                    "eq" | "ne" | "lt" | "le" | "gt" | "ge" | "in"
                )
            {
                return Err(invalid("结构化筛选无效"));
            }
            if p.op == "in" && p.value.as_array().is_none_or(|v| v.len() > 128) {
                return Err(limit());
            }
        }
        for order in &self.order_by {
            if !self
                .fields
                .iter()
                .any(|f| f == order.trim_start_matches('-'))
                && order != "security"
                && order != "-security"
            {
                return Err(invalid("排序字段无效"));
            }
        }
        Ok(())
    }
}
pub struct ReadView {
    visibility: Visibility,
    lease: ViewLease,
    prices: Vec<PriceRow>,
    research: Vec<ResearchRow>,
    max_rows: usize,
    max_bytes: usize,
    bytes: usize,
}
impl ReadView {
    pub fn new(boundary: Boundary, max_rows: usize, max_bytes: usize) -> QfResult<Self> {
        if max_rows == 0 || max_rows > 100000 || max_bytes == 0 || max_bytes > MAX_ARROW_BATCH_BYTES
        {
            return Err(limit());
        }
        Ok(Self {
            visibility: boundary.visibility()?,
            lease: ViewLease::default(),
            prices: vec![],
            research: vec![],
            max_rows,
            max_bytes,
            bytes: 0,
        })
    }
    pub fn lease(&self) -> ViewLease {
        self.lease.clone()
    }
    pub fn check(&self) -> QfResult<()> {
        self.lease.check()
    }
    pub fn contains_market(&self, key: &EventKey) -> QfResult<bool> {
        self.check()?;
        Ok(self.visibility.contains_market(key))
    }
    pub fn expire(&mut self) {
        self.prices.clear();
        self.research.clear();
        self.bytes = 0;
        self.lease.expire();
    }
    pub fn reset(&mut self) -> QfResult<()> {
        self.check()?;
        self.prices.clear();
        self.research.clear();
        self.bytes = 0;
        Ok(())
    }
    fn reservation(&self, rows: usize, bytes: usize) -> QfResult<usize> {
        self.check()?;
        if self.prices.len() + self.research.len() + rows > self.max_rows
            || self.bytes.saturating_add(bytes) > self.max_bytes
        {
            return Err(limit());
        }
        Ok(self.bytes + bytes)
    }
    fn reserve(&mut self, rows: usize, bytes: usize) -> QfResult<()> {
        self.bytes = self.reservation(rows, bytes)?;
        Ok(())
    }
    pub fn push_market(&mut self, payload: &[u8], metadata: &Value) -> QfResult<()> {
        self.check()?;
        let input = arrow::decode_flat(
            payload,
            10000,
            self.max_bytes,
            arrow::TEXT,
            arrow::INTS,
            arrow::SEQS,
        )?;
        if !arrow::schema_valid(input.schema().as_ref())
            || metadata["schema_id"] != arrow::SCHEMA_ID
            || metadata["rows"].as_u64() != Some(input.num_rows() as u64)
        {
            return Err(invalid("行情批次头与布局不一致"));
        }
        self.reserve(
            input.num_rows(),
            input
                .get_array_memory_size()
                .saturating_mul(2)
                .saturating_add(input.num_rows().saturating_mul(2048)),
        )?;
        for row in 0..input.num_rows() {
            let key = EventKey {
                time_ns: Nanoseconds::new(
                    integer(&input, "time_ns", row)?.ok_or_else(|| invalid("时间缺失"))?,
                ),
                phase: EventPhase::Market,
                security: SecurityKey::new(required(&input, "security", row)?)?,
                identity: EventIdentity {
                    source_session: SessionKey::new(required(&input, "source_session", row)?)?,
                    channel: ChannelKey::new(required(&input, "channel", row)?)?,
                    sequence: sequence(&input, "sequence", row)?,
                    stable_input_sequence: sequence(&input, "stable_input_sequence", row)?
                        .ok_or_else(|| invalid("事件身份缺失"))?,
                },
            };
            if !self.visibility.contains_market(&key) {
                continue;
            }
            let kind = required(&input, "kind", row)?.to_owned();
            if !matches!(kind.as_str(), "bar" | "trade_tick" | "quote_tick") {
                return Err(invalid("未知市场事件"));
            }
            let start_ns = integer(&input, "interval_start_ns", row)?.map(Nanoseconds::new);
            if kind == "bar"
                && (start_ns.is_none_or(|n| n >= key.time_ns)
                    || integer(&input, "interval_end_ns", row)? != Some(key.time_ns.get()))
            {
                return Err(invalid("Bar 区间不一致"));
            }
            let mut values = BTreeMap::new();
            for field in ["open", "high", "low", "close", "price", "bid", "ask"] {
                let value = string(&input, field, row)?;
                if value.is_some_and(|v| !exact_decimal_text(v) || !compare_decimal(v, "0").is_gt())
                {
                    return Err(invalid("原价格精确表示无效"));
                }
                values.insert(field.into(), json!(value));
            }
            for field in ["quantity", "bid_quantity", "ask_quantity"] {
                let value = integer(&input, field, row)?;
                if value.is_some_and(|n| n < 0) {
                    return Err(invalid("份额不能为负"));
                }
                values.insert(field.into(), json!(value.map(|n| n.to_string())));
            }
            values.insert("kind".into(), json!(kind));
            values.insert("channel".into(), json!(key.identity.channel));
            values.insert("sequence".into(), json!(key.identity.sequence));
            self.prices.push(PriceRow {
                key,
                session: SessionKey::new(required(&input, "session", row)?)?,
                start_ns,
                kind,
                values,
            });
        }
        Ok(())
    }
    pub fn push_research(&mut self, payload: &[u8], metadata: &Value) -> QfResult<()> {
        self.check()?;
        let input = arrow::decode_flat(
            payload,
            10000,
            self.max_bytes,
            RESEARCH_TEXT,
            RESEARCH_INTS,
            &[],
        )?;
        let schema = input.schema();
        let meta = schema.metadata();
        if meta.get("schema_id").map(String::as_str) != Some(RESEARCH_SCHEMA_ID)
            || meta.get("time_unit").map(String::as_str) != Some("utc_nanoseconds")
            || meta.get("knowledge_basis").map(String::as_str) != Some("verified_public_time")
            || metadata["schema_id"] != RESEARCH_SCHEMA_ID
            || metadata["rows"].as_u64() != Some(input.num_rows() as u64)
        {
            return Err(unavailable("研究公开时间口径尚未验证"));
        }
        self.reserve(
            input.num_rows(),
            input
                .get_array_memory_size()
                .saturating_mul(2)
                .saturating_add(input.num_rows().saturating_mul(512)),
        )?;
        for i in 0..input.num_rows() {
            let row = ResearchRow {
                security: required(&input, "security", i)?.into(),
                instrument_id: required(&input, "instrument_id", i)?.into(),
                record_id: required(&input, "record_id", i)?.into(),
                field: required(&input, "field", i)?.into(),
                value_kind: required(&input, "value_kind", i)?.into(),
                value: string(&input, "value", i)?.map(String::from),
                unit: required(&input, "unit", i)?.into(),
                report_period: string(&input, "report_period", i)?.map(String::from),
                time_ns: Nanoseconds::new(
                    integer(&input, "time_ns", i)?.ok_or_else(|| invalid("公开时间缺失"))?,
                ),
                effective_from_ns: Nanoseconds::new(
                    integer(&input, "effective_from_ns", i)?
                        .ok_or_else(|| invalid("有效时间缺失"))?,
                ),
                effective_until_ns: integer(&input, "effective_until_ns", i)?.map(Nanoseconds::new),
            };
            row.validate()?;
            if row.time_ns <= self.visibility.now_ns {
                self.research.push(row);
            }
        }
        Ok(())
    }
    pub fn prices(&mut self, request: &DataRequest) -> QfResult<Vec<Value>> {
        self.check()?;
        request.validate(self.visibility.now_ns)?;
        let allowed = if request.frequency == Frequency::Tick {
            &[
                "price",
                "bid",
                "ask",
                "quantity",
                "bid_quantity",
                "ask_quantity",
                "kind",
                "channel",
                "sequence",
            ][..]
        } else {
            &[
                "open",
                "high",
                "low",
                "close",
                "quantity",
                "interval_start_ns",
                "interval_end_ns",
                "is_partial",
                "status",
            ][..]
        };
        if request
            .fields
            .iter()
            .any(|f| !allowed.contains(&f.as_str()))
        {
            return Err(unavailable("频率与字段不匹配，Tick 不能使用 close 默认列"));
        }
        self.prices
            .sort_by(|a, b| a.key.security.cmp(&b.key.security).then(a.key.cmp(&b.key)));
        if self.prices.windows(2).any(|w| w[0].key == w[1].key) {
            return Err(invalid("重复完整市场事件键"));
        }
        let mut output = Vec::new();
        let mut counts = BTreeMap::new();
        for row in self.prices.iter().rev() {
            if !request.securities.contains(&row.key.security)
                || row.key.time_ns > request.end_ns
                || request.start_ns.is_some_and(|n| row.key.time_ns < n)
            {
                continue;
            }
            let count = counts.entry(row.key.security.clone()).or_insert(0_u32);
            if request.count_per_security.is_some_and(|n| *count >= n) {
                continue;
            }
            *count += 1;
            let mut object = serde_json::Map::new();
            object.insert("security".into(), json!(row.key.security));
            object.insert("time_ns".into(), json!(row.key.time_ns));
            for f in &request.fields {
                let value = match f.as_str() {
                    "interval_start_ns" => json!(row.start_ns),
                    "interval_end_ns" => json!(row.key.time_ns),
                    "is_partial" => row.values.get(f).cloned().unwrap_or(json!(false)),
                    "status" => row.values.get(f).cloned().unwrap_or_else(|| {
                        json!(if request.fields.iter().any(|f| matches!(
                            f.as_str(),
                            "open" | "high" | "low" | "close" | "price" | "bid" | "ask"
                        ) && row
                            .values
                            .get(f)
                            .is_none_or(Value::is_null))
                        {
                            "missing"
                        } else {
                            "observed"
                        })
                    }),
                    _ => row.values.get(f).cloned().unwrap_or(Value::Null),
                };
                object.insert(f.clone(), value);
            }
            output.push(Value::Object(object));
        }
        output.reverse();
        Ok(output)
    }
    pub fn current(&self, securities: &[String]) -> QfResult<Value> {
        self.check()?;
        let mut result = serde_json::Map::new();
        for security in securities {
            let mut rows: Vec<_> = self
                .prices
                .iter()
                .filter(|r| r.key.security.as_str() == security)
                .collect();
            rows.sort_by_key(|r| r.key.clone());
            let latest = rows.last();
            let price = rows.iter().rev().find_map(|r| {
                r.values
                    .get(if r.kind == "bar" { "close" } else { "price" })
                    .filter(|v| !v.is_null())
                    .map(|v| (v, r.key.time_ns))
            });
            let halted = self
                .research
                .iter()
                .filter(|r| {
                    r.security == *security
                        && r.field == "halted"
                        && r.visible(self.visibility.now_ns)
                })
                .max_by_key(|r| r.time_ns)
                .and_then(|r| r.value.as_ref())
                .map(|v| v == "true");
            let mut quote = json!({"security":security,"time_ns":self.visibility.now_ns,"last_price":price.map(|p|p.0),
                "price_time_ns":price.map(|p|p.1),"is_stale":price.is_none_or(|p|p.1 < self.visibility.now_ns),"halted":halted});
            if let Some(row) = latest
                && row.kind != "bar"
            {
                let q = quote
                    .as_object_mut()
                    .ok_or_else(|| invalid("报价输出无效"))?;
                q.insert("time_ns".into(), json!(row.key.time_ns));
                if price.is_none() {
                    let known = ["bid", "ask"]
                        .iter()
                        .any(|f| row.values.get(*f).is_some_and(|v| !v.is_null()));
                    q.insert(
                        "is_stale".into(),
                        json!(!known || row.key.time_ns < self.visibility.now_ns),
                    );
                }
                for f in [
                    "channel",
                    "sequence",
                    "bid",
                    "ask",
                    "quantity",
                    "bid_quantity",
                    "ask_quantity",
                ] {
                    q.insert(f.into(), row.values.get(f).cloned().unwrap_or(Value::Null));
                }
                q.insert(
                    "kind".into(),
                    json!(if row.kind == "trade_tick" {
                        "trade"
                    } else {
                        "quote"
                    }),
                );
            }
            result.insert(security.clone(), quote);
        }
        Ok(Value::Object(result))
    }
    pub fn research(&self, request: &ResearchRequest) -> QfResult<Vec<Value>> {
        self.check()?;
        request.validate(self.visibility.now_ns)?;
        let mut groups: BTreeMap<(String, String, String), Vec<&ResearchRow>> = BTreeMap::new();
        let mut identities: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for row in &self.research {
            if !request.securities.contains(&row.security)
                || !request.fields.contains(&row.field)
                || !row.visible(request.as_of_ns)
            {
                continue;
            }
            identities
                .entry(row.security.clone())
                .or_default()
                .insert(row.instrument_id.clone());
            groups
                .entry((
                    row.security.clone(),
                    row.instrument_id.clone(),
                    row.record_id.clone(),
                ))
                .or_default()
                .push(row);
        }
        if identities.values().any(|ids| ids.len() > 1) {
            return Err(unavailable("显示代码在此时点不能唯一解析为稳定身份"));
        }
        let mut selected: BTreeMap<String, (Option<String>, Nanoseconds, Value)> = BTreeMap::new();
        let mut members = Vec::new();
        for ((security, instrument_id, _), rows) in groups {
            let public = rows
                .iter()
                .map(|r| r.time_ns)
                .max()
                .ok_or_else(|| invalid("公开时间缺失"))?;
            let period = rows[0].report_period.clone();
            if request.kind == "fundamentals" && period.is_none() {
                return Err(unavailable("财务报告期末缺失"));
            }
            if rows
                .iter()
                .any(|r| r.report_period != period || r.time_ns != public)
            {
                return Err(invalid("同记录字段公开边界不一致"));
            }
            let mut object = serde_json::Map::new();
            object.insert("security".into(), json!(security));
            object.insert("time_ns".into(), json!(public));
            object.insert("instrument_id".into(), json!(instrument_id));
            object.insert("report_period".into(), json!(period));
            for field in &request.fields {
                let field_rows: Vec<_> = rows.iter().filter(|r| &r.field == field).collect();
                if field_rows.len() > 1 {
                    return Err(invalid("同记录字段重复"));
                }
                object.insert(
                    field.clone(),
                    field_rows.first().map_or(Value::Null, |r| r.typed()),
                );
            }
            let value = Value::Object(object);
            if request.kind == "index_stocks" {
                members.push((rows[0].effective_from_ns, public, value));
                continue;
            }
            let order_period = if request.kind == "fundamentals" {
                period
            } else {
                None
            };
            let replace = selected
                .get(&security)
                .is_none_or(|(p, t, _)| (&order_period, public) > (p, *t));
            if replace {
                selected.insert(security, (order_period, public, value));
            }
        }
        let output: Vec<_> = if request.kind == "index_stocks" {
            let latest = members
                .iter()
                .map(|(effective, public, _)| (*effective, *public))
                .max();
            members
                .into_iter()
                .filter(|(effective, public, _)| Some((*effective, *public)) == latest)
                .map(|(_, _, v)| v)
                .collect()
        } else {
            selected.into_values().map(|(_, _, v)| v).collect()
        };
        let mut filtered = Vec::new();
        for row in output {
            let mut keep = true;
            for p in &request.filters {
                keep &= predicate(&row, p)?;
            }
            if keep {
                filtered.push(row);
            }
        }
        let mut output = filtered;
        output.sort_by(|a, b| {
            for key in &request.order_by {
                let field = key.trim_start_matches('-');
                let order = compare_cell(&a[field], &b[field]).unwrap_or(std::cmp::Ordering::Equal);
                let order = if key.starts_with('-') {
                    order.reverse()
                } else {
                    order
                };
                if !order.is_eq() {
                    return order;
                }
            }
            a["security"].as_str().cmp(&b["security"].as_str())
        });
        output.truncate(request.limit);
        Ok(output)
    }
    pub fn adjust(&mut self, adjustment: Adjustment, anchor: Nanoseconds) -> QfResult<()> {
        self.check()?;
        if adjustment == Adjustment::None {
            return Ok(());
        }
        if anchor > self.visibility.now_ns {
            return Err(QfError::new(
                ErrorCode::LookaheadForbidden,
                "adjustment",
                "复权锚点晚于模拟时间",
            ));
        }
        for price in &mut self.prices {
            let mut factors: Vec<_> = self
                .research
                .iter()
                .filter(|r| {
                    r.security == price.key.security.as_str()
                        && r.field == "factor"
                        && r.time_ns <= anchor
                        && r.effective_from_ns <= anchor
                        && r.unit == "verified_cumulative_factor"
                })
                .collect();
            factors.sort_by_key(|r| (r.effective_from_ns, r.time_ns));
            if factors
                .iter()
                .map(|r| &r.instrument_id)
                .collect::<BTreeSet<_>>()
                .len()
                > 1
            {
                return Err(unavailable("同显示代码的复权因子不能跨稳定身份串接"));
            }
            let factor = factors.iter().rev().find(|r| {
                r.effective_from_ns <= price.key.time_ns
                    && r.effective_until_ns
                        .is_none_or(|end| price.key.time_ns < end)
            });
            let basis = if adjustment == Adjustment::Pre {
                factors.last()
            } else {
                factors.first()
            };
            let parse = |f: Option<&&ResearchRow>| -> QfResult<ExactDecimal> {
                let f = f.ok_or_else(|| unavailable("缺少已验证因子/锚点，不能研究复权"))?;
                let value: ExactDecimal = f
                    .value
                    .as_deref()
                    .ok_or_else(|| unavailable("复权因子缺失"))?
                    .parse()?;
                if value.is_negative() || value.is_zero() {
                    return Err(unavailable("复权因子必须为正"));
                }
                Ok(value)
            };
            let factor = parse(factor)?;
            let basis = parse(basis)?;
            for field in ["open", "high", "low", "close", "price", "bid", "ask"] {
                if let Some(value) = price.values.get_mut(field)
                    && let Some(text) = value.as_str()
                {
                    let adjusted = text
                        .parse::<ExactDecimal>()?
                        .checked_mul(factor)?
                        .div_rounded(basis, 12, RoundingPolicy::HalfEven)?;
                    *value = json!(adjusted.to_string());
                }
            }
        }
        Ok(())
    }
    pub fn research_prices(&mut self, request: &DataRequest) -> QfResult<()> {
        self.check()?;
        if request.frequency != Frequency::Day {
            return Err(unavailable("研究点数/原价投影尚只支持已接受的日线"));
        }
        type DailyRecords<'a> =
            BTreeMap<(String, Nanoseconds), BTreeMap<(Nanoseconds, String), Vec<&'a ResearchRow>>>;
        let mut records: DailyRecords<'_> = BTreeMap::new();
        let mut identities: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for row in &self.research {
            if row.time_ns <= request.end_ns
                && row.effective_from_ns <= request.end_ns
                && request
                    .securities
                    .iter()
                    .any(|s| s.as_str() == row.security)
                && request.fields.contains(&row.field)
            {
                if (row.field == "quantity" && row.value_kind != "integer")
                    || (row.field != "quantity" && row.value_kind != "decimal")
                {
                    return Err(unavailable("研究行情原值类型尚未验证"));
                }
                identities
                    .entry(row.security.clone())
                    .or_default()
                    .insert(row.instrument_id.clone());
                records
                    .entry((row.security.clone(), row.effective_from_ns))
                    .or_default()
                    .entry((row.time_ns, row.record_id.clone()))
                    .or_default()
                    .push(row);
            }
        }
        if identities.values().any(|ids| ids.len() > 1) {
            return Err(unavailable("研究行情显示代码对应多个身份，不能拼接"));
        }
        let additional = records.len();
        self.bytes =
            self.reservation(additional, additional.checked_mul(2048).ok_or_else(limit)?)?;
        for ((security, _), revisions) in records {
            let ((_, record), rows) = revisions
                .into_iter()
                .next_back()
                .ok_or_else(|| invalid("空研究行情记录"))?;
            if rows.iter().any(|r| {
                r.effective_from_ns != rows[0].effective_from_ns
                    || r.instrument_id != rows[0].instrument_id
            }) {
                return Err(invalid("研究行情记录身份/日期不一致"));
            }
            let mut values = BTreeMap::new();
            for row in &rows {
                if values.insert(row.field.clone(), json!(row.value)).is_some() {
                    return Err(invalid("重复研究行情字段"));
                }
            }
            let missing = rows.iter().any(|r| r.value.is_none());
            values.insert(
                "status".into(),
                json!(if missing { "missing" } else { "observed" }),
            );
            let session = SessionKey::new(record)?;
            self.prices.push(PriceRow {
                key: EventKey {
                    time_ns: rows[0].effective_from_ns,
                    phase: EventPhase::Market,
                    security: SecurityKey::new(security)?,
                    identity: EventIdentity {
                        source_session: session.clone(),
                        channel: ChannelKey::new("research_daily")?,
                        sequence: None,
                        stable_input_sequence: crate::types::Sequence::new(0),
                    },
                },
                session,
                start_ns: None,
                kind: "bar".into(),
                values,
            });
        }
        Ok(())
    }
    pub fn resample(
        &mut self,
        source: Frequency,
        target: Frequency,
        calendar: &[CalendarSession],
    ) -> QfResult<()> {
        self.check()?;
        if source == target {
            return Ok(());
        }
        let duration = |f| match f {
            Frequency::Minute => Some(1),
            Frequency::FiveMinutes => Some(5),
            Frequency::FifteenMinutes => Some(15),
            Frequency::ThirtyMinutes => Some(30),
            Frequency::Hour => Some(60),
            _ => None,
        };
        let (Some(fine), Some(coarse)) = (duration(source), duration(target)) else {
            return Err(unavailable("此降采样频率组合不受支持"));
        };
        if fine >= coarse || coarse % fine != 0 {
            return Err(unavailable("不能从粗粒度升频或跨不整除的桶"));
        }
        for session in calendar {
            session.validate()?;
        }
        self.prices.sort_by_key(|r| r.key.clone());
        if self.prices.windows(2).any(|w| w[0].key == w[1].key) {
            return Err(invalid("降采样输入完整事件键重复"));
        }
        let mut groups: BTreeMap<(SecurityKey, SessionKey, Nanoseconds), Vec<PriceRow>> =
            BTreeMap::new();
        for row in self.prices.drain(..) {
            if row.kind != "bar" {
                return Err(unavailable("Tick 不能冒充分钟 Bar"));
            }
            let session = calendar
                .iter()
                .find(|s| s.session.key == row.session)
                .ok_or_else(|| unavailable("缺少来源会话的已接受分桶日历"))?;
            if !session
                .buckets(source)?
                .any(|b| row.start_ns == Some(b.start_ns) && row.key.time_ns == b.end_ns)
            {
                return Err(unavailable("输入 Bar 不符合声明细粒度会话桶"));
            }
            let bucket = session
                .buckets(target)?
                .find(|b| {
                    row.start_ns.is_some_and(|start| start >= b.start_ns)
                        && row.key.time_ns <= b.end_ns
                })
                .ok_or_else(|| unavailable("输入 Bar 跨午休、会话或目标桶"))?;
            if bucket.end_ns > self.visibility.now_ns {
                continue;
            }
            groups
                .entry((row.key.security.clone(), row.session.clone(), bucket.end_ns))
                .or_default()
                .push(row);
        }
        for ((_, session, end), mut rows) in groups {
            rows.sort_by_key(|r| r.key.clone());
            let accepted = calendar
                .iter()
                .find(|s| s.session.key == session)
                .ok_or_else(|| unavailable("会话缺失"))?;
            let bucket = accepted
                .buckets(target)?
                .find(|b| b.end_ns == end)
                .ok_or_else(|| unavailable("桶缺失"))?;
            let mut row = rows[0].clone();
            let last = rows.last().ok_or_else(|| invalid("空桶"))?;
            row.key.time_ns = end;
            row.start_ns = Some(bucket.start_ns);
            row.values.insert(
                "close".into(),
                last.values.get("close").cloned().unwrap_or(Value::Null),
            );
            for (field, maximum) in [("high", true), ("low", false)] {
                let values: Vec<&str> = rows
                    .iter()
                    .filter_map(|r| r.values[field].as_str())
                    .collect();
                let value = if maximum {
                    values.iter().max_by(|a, b| compare_decimal(a, b))
                } else {
                    values.iter().min_by(|a, b| compare_decimal(a, b))
                };
                row.values.insert(field.into(), json!(value));
            }
            let quantity =
                rows.iter()
                    .try_fold(Some(0_i64), |total, r| -> QfResult<Option<i64>> {
                        match (total, r.values["quantity"].as_str()) {
                            (Some(a), Some(b)) => a
                                .checked_add(b.parse::<i64>().map_err(|_| invalid("量无效"))?)
                                .map(Some)
                                .ok_or_else(limit),
                            _ => Ok(None),
                        }
                    })?;
            row.values
                .insert("quantity".into(), json!(quantity.map(|n| n.to_string())));
            let complete = rows[0].start_ns == Some(bucket.start_ns)
                && last.key.time_ns == bucket.end_ns
                && rows.iter().all(|r| {
                    ["open", "high", "low", "close"]
                        .iter()
                        .all(|f| !r.values[*f].is_null())
                })
                && rows
                    .windows(2)
                    .all(|w| w[1].start_ns == Some(w[0].key.time_ns));
            let partial = (bucket.end_ns.get() - bucket.start_ns.get())
                != i64::from(coarse) * crate::clock::calendar::MINUTE_NS;
            row.values.insert("is_partial".into(), json!(partial));
            row.values.insert(
                "status".into(),
                json!(if complete { "observed" } else { "missing" }),
            );
            if !complete {
                for f in ["open", "high", "low", "close", "quantity"] {
                    row.values.insert(f.into(), Value::Null);
                }
            }
            self.prices.push(row);
        }
        Ok(())
    }
}
fn cell_text(value: &Value) -> Option<&str> {
    value.as_str().or_else(|| value["value"].as_str())
}
fn compare_cell(a: &Value, b: &Value) -> QfResult<std::cmp::Ordering> {
    match (cell_text(a), cell_text(b)) {
        (Some(a_text), Some(b_text))
            if matches!(a["kind"].as_str(), Some("decimal" | "integer"))
                || matches!(b["kind"].as_str(), Some("decimal" | "integer")) =>
        {
            // Exact decimal lexical comparison at arbitrary research precision.
            if !exact_decimal_text(a_text) || !exact_decimal_text(b_text) {
                return Err(invalid("筛选数值类型不一致"));
            }
            Ok(compare_decimal(a_text, b_text))
        }
        (Some(a), Some(b)) => Ok(a.cmp(b)),
        (None, None) => Ok(std::cmp::Ordering::Equal),
        (None, Some(_)) => Ok(std::cmp::Ordering::Greater),
        (Some(_), None) => Ok(std::cmp::Ordering::Less),
    }
}
pub fn compare_decimal(a: &str, b: &str) -> std::cmp::Ordering {
    let negative = |s: &str| s.starts_with('-') && s.bytes().any(|c| (b'1'..=b'9').contains(&c));
    let (an, bn) = (negative(a), negative(b));
    if an != bn {
        return if an {
            std::cmp::Ordering::Less
        } else {
            std::cmp::Ordering::Greater
        };
    }
    let parts = |s: &str| {
        let mut p = s.trim_start_matches('-').split('.');
        (
            p.next().unwrap_or("0").to_string(),
            p.next().unwrap_or("").trim_end_matches('0').to_string(),
        )
    };
    let ((ai, af), (bi, bf)) = (parts(a), parts(b));
    let ordering = ai.len().cmp(&bi.len()).then(ai.cmp(&bi)).then_with(|| {
        let n = af.len().max(bf.len());
        let a = format!("{af:0<n$}");
        let b = format!("{bf:0<n$}");
        a.cmp(&b)
    });
    if an { ordering.reverse() } else { ordering }
}
fn predicate(row: &Value, p: &Predicate) -> QfResult<bool> {
    let cell = &row[&p.field];
    if cell.is_null() || cell["value"].is_null() {
        return Ok(false);
    }
    let comparison = |v: &Value| compare_cell(cell, &json!({"kind":cell["kind"],"value":v}));
    if p.op == "in" {
        return p
            .value
            .as_array()
            .ok_or_else(|| invalid("in 必须是值集合"))?
            .iter()
            .try_fold(false, |matched, v| Ok(matched || comparison(v)?.is_eq()));
    }
    let order = comparison(&p.value)?;
    Ok(match p.op.as_str() {
        "eq" => order.is_eq(),
        "ne" => !order.is_eq(),
        "lt" => order.is_lt(),
        "le" => !order.is_gt(),
        "gt" => order.is_gt(),
        "ge" => !order.is_lt(),
        _ => return Err(invalid("未知筛选操作")),
    })
}

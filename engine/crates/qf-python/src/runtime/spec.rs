//! Trusted worker wiring, not a client REST request or a production data mapper.
use qf_core::accounting::{
    AccountTerms, CorporateAction, SaleProceedsRule, SaleProceedsTiming, ValuationMark,
};
use qf_core::clock::CalendarSession;
use qf_core::data::DataRequest;
use qf_core::orders::{AdmissionFacts, OrderFacts, Side};
use qf_core::rules::{
    FeeConfig,
    date::RuleDate,
    fees::{FeeScope, InvestorKind, ScopedFeeConfig},
    market::*,
};
use qf_core::run::RunConfig;
use qf_core::types::*;
use qf_core::{ErrorCode, QfError, QfResult};
use serde::Deserialize;
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Spec {
    pub run_id: String,
    pub config: RunConfig,
    pub calendar: Vec<CalendarSession>,
    pub calendar_reference: String,
    pub next_session: Option<SessionKey>,
    #[serde(default)]
    pub following_session: Option<CalendarSession>,
    pub rules: Vec<TradingRule>,
    pub facts: Vec<Fact>,
    #[serde(default)]
    pub stream_session_facts: bool,
    pub corporate_actions: Vec<CorporateAction>,
    pub initial_marks: BTreeMap<SecurityKey, ValuationMark>,
    pub source: Source,
    pub synthetic_model: Option<String>,
    #[serde(default)]
    pub benchmark_origin: Option<qf_core::analysis::BenchmarkPoint>,
    pub result_budget_bytes: u64,
    pub data_budget_bytes: usize,
    #[serde(default = "default_data_max_rows")]
    pub data_max_rows: usize,
    pub max_active_orders: usize,
    pub max_commands_per_boundary: usize,
    pub chunk_events: usize,
    pub wall_time_seconds: u64,
}
fn default_data_max_rows() -> usize {
    10000
}
#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Source {
    /// Small isolated fixtures only, bounded by the control message as well.
    Fixture { events: Vec<MarketEvent> },
    Ipc {
        socket_path: String,
        binding: String,
        request: DataRequest,
    },
}
#[derive(Clone, Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct Fact {
    pub instrument: Instrument,
    pub day: TradeDayFacts,
    pub permissions: TradePermissions,
    pub submission_reference: Option<Price>,
    pub buy_fees: FeeConfig,
    pub sell_fees: FeeConfig,
    pub investor: InvestorKind,
    pub origin: RuleOrigin,
    pub sale_proceeds: String,
    pub sale_proceeds_basis: String,
    pub disposal_settlement_date: Option<RuleDate>,
    pub holding_bands: BTreeMap<RuleDate, qf_core::rules::dividends::HoldingBand>,
}
#[derive(Clone)]
pub struct Facts {
    pub book: RuleBook,
    pub usage: RuleUse,
    pub rows: Arc<Mutex<BTreeMap<(SessionKey, SecurityKey), Fact>>>,
    pub gateway: Option<super::source::Gateway>,
    streamed: bool,
    universe: BTreeSet<SecurityKey>,
}
pub fn invalid(message: &str) -> QfError {
    QfError::new(ErrorCode::InvalidContract, "strategy_worker", message)
}
impl Facts {
    pub fn new(spec: &Spec) -> QfResult<Self> {
        if spec.facts.len() > 10_000
            || spec.calendar.is_empty()
            || spec.calendar.len() > 10000
            || spec.stream_session_facts
                && (!spec.facts.is_empty() || !matches!(spec.source, Source::Ipc { .. }))
            || spec.rules.len() > 1024
            || spec.corporate_actions.len() > 10_000
            || spec.wall_time_seconds == 0
            || spec.wall_time_seconds > 3600
            || spec.data_budget_bytes < 16 * 1024 * 1024
            || spec.data_budget_bytes > 1024 * 1024 * 1024
            || spec.data_max_rows == 0
            || spec.data_max_rows > 1000000
            || spec.chunk_events == 0
            || spec.chunk_events > 10_000
        {
            return Err(invalid("工作进程事实或资源预算无效"));
        }
        crate::runtime::label(&spec.run_id)?;
        let usage = match &spec.synthetic_model {
            Some(model) => {
                crate::runtime::label(model)?;
                RuleUse::Synthetic(model.clone())
            }
            None => RuleUse::Market,
        };
        if matches!(spec.source, Source::Fixture { .. }) && spec.synthetic_model.is_none() {
            return Err(QfError::new(
                ErrorCode::CapabilityUnavailable,
                "strategy_worker",
                "内存夹具仅允许明确命名的隔离合成模型",
            ));
        }
        let mut rows = BTreeMap::new();
        for row in &spec.facts {
            if !spec.config.universe.contains(&row.instrument.security)
                || row.instrument.security != row.day.security
                || !spec
                    .calendar
                    .iter()
                    .any(|s| s.session.key == row.day.session && s.date == row.day.date)
                || rows
                    .insert(
                        (row.day.session.clone(), row.instrument.security.clone()),
                        row.clone(),
                    )
                    .is_some()
            {
                return Err(invalid("账户事实身份、日期或授权集合不一致"));
            }
        }
        Ok(Self {
            book: RuleBook::new(spec.rules.clone())?,
            usage,
            rows: Arc::new(Mutex::new(rows)),
            gateway: None,
            streamed: spec.stream_session_facts,
            universe: spec.config.universe.iter().cloned().collect(),
        })
    }
    pub fn row(&self, security: &SecurityKey, session: &SessionKey) -> QfResult<Fact> {
        self.rows
            .lock()
            .map_err(|_| invalid("会话事实锁失败"))?
            .get(&(session.clone(), security.clone()))
            .cloned()
            .ok_or_else(|| {
                QfError::new(
                    ErrorCode::RuleUnavailable,
                    "account_terms",
                    "缺少该标的/会话的已接受规则、费用或交易事实",
                )
            })
    }
    fn load_session(&self, session: &CalendarSession) -> QfResult<()> {
        if !self.streamed {
            return Ok(());
        }
        let gateway = self
            .gateway
            .as_ref()
            .ok_or_else(|| invalid("会话事实通道未绑定"))?;
        let mut rows = BTreeMap::new();
        let mut offset = 0;
        loop {
            let (status, body, payload) = gateway
                .lock()
                .map_err(|_| invalid("事实通道锁失败"))?
                .callback_request(
                    "session_facts",
                    json!({"session":session.session.key,"offset":offset}),
                )?;
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Page {
                session: SessionKey,
                facts: Vec<Fact>,
                next_offset: Option<usize>,
            }
            let page: Page = serde_json::from_value(body).map_err(|_| invalid("会话事实页无效"))?;
            if status != "ok"
                || !payload.is_empty()
                || page.session != session.session.key
                || page.facts.is_empty()
                || page.facts.len() > 256
                || offset + page.facts.len() > self.universe.len()
                || page
                    .next_offset
                    .is_some_and(|n| n != offset + page.facts.len())
            {
                return Err(invalid("会话事实身份或信用无效"));
            }
            offset += page.facts.len();
            for row in page.facts {
                if row.day.session != session.session.key
                    || row.day.date != session.date
                    || row.instrument.security != row.day.security
                    || !self.universe.contains(&row.instrument.security)
                    || rows
                        .insert(
                            (row.day.session.clone(), row.instrument.security.clone()),
                            row,
                        )
                        .is_some()
                {
                    return Err(invalid("会话事实身份、日期、重复或授权无效"));
                }
            }
            if page.next_offset.is_none() {
                break;
            }
        }
        if rows.len() != self.universe.len() {
            return Err(QfError::new(
                ErrorCode::RuleUnavailable,
                "account_terms",
                "会话事实未覆盖授权标的，不能猜测缺失规则",
            ));
        }
        // One complete admitted session replaces the previous cache atomically.
        *self.rows.lock().map_err(|_| invalid("会话事实锁失败"))? = rows;
        Ok(())
    }
}
impl OrderFacts for Facts {
    fn session_terms(&mut self, session: &CalendarSession) -> QfResult<Vec<AccountTerms>> {
        self.load_session(session)?;
        self.rows
            .lock()
            .map_err(|_| invalid("会话事实锁失败"))?
            .iter()
            .filter(|((s, _), _)| s == &session.session.key)
            .map(|(_, row)| {
                let scope = |side| FeeScope {
                    instrument: row.instrument.clone(),
                    side,
                    investor: row.investor,
                    origin: row.origin.clone(),
                };
                let mut terms = AccountTerms::resolve(
                    row.instrument.clone(),
                    &self.book,
                    row.day.clone(),
                    ScopedFeeConfig::new(scope(Side::Buy), row.buy_fees.clone(), &self.usage)?,
                    ScopedFeeConfig::new(scope(Side::Sell), row.sell_fees.clone(), &self.usage)?,
                    SaleProceedsRule {
                        timing: match row.sale_proceeds.as_str() {
                            "immediate" => SaleProceedsTiming::Immediate,
                            "next_session" => SaleProceedsTiming::NextSession,
                            _ => return Err(invalid("资金可用事实未知")),
                        },
                        basis: row.sale_proceeds_basis.clone(),
                        origin: row.origin.clone(),
                    },
                    self.usage.clone(),
                )?;
                terms.disposal_settlement_date = row.disposal_settlement_date.clone();
                terms.holding_bands = row.holding_bands.clone();
                Ok(terms)
            })
            .collect()
    }
    fn admission(
        &self,
        security: &SecurityKey,
        _side: Side,
        session: &CalendarSession,
        _now: Nanoseconds,
    ) -> QfResult<AdmissionFacts> {
        let row = self.row(security, &session.session.key)?;
        Ok(AdmissionFacts {
            instrument: row.instrument.clone(),
            permissions: row.permissions.clone(),
            submission_reference: row.submission_reference,
        })
    }
}

//! Dated secondary-market rules. No symbol-prefix inference, calendar fetching,
//! account mutation or matching. Identity and each day's facts come from D04;
//! session keys/time boundaries come from D03. Prices here are unadjusted.
use super::date::{EffectiveRange, RuleDate};
use crate::orders::Side;
use crate::types::{
    ExactDecimal, Price, Quantity, QuantityStep, RoundingPolicy, SecurityKey, SessionKey,
};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Exchange {
    Shanghai,
    Shenzhen,
    Beijing,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Product {
    MainBoardStock,
    StarStock,
    ChiNextStock,
    BeijingStock,
    EquityEtf,
    BondEtf,
    MoneyEtf,
    GoldEtf,
    CommodityEtf,
    CrossBorderEtf,
    Index,
    Unknown,
}
impl Product {
    pub fn is_etf(self) -> bool {
        matches!(
            self,
            Self::EquityEtf
                | Self::BondEtf
                | Self::MoneyEtf
                | Self::GoldEtf
                | Self::CommodityEtf
                | Self::CrossBorderEtf
        )
    }
    pub fn is_stock(self) -> bool {
        matches!(
            self,
            Self::MainBoardStock | Self::StarStock | Self::ChiNextStock | Self::BeijingStock
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Instrument {
    pub security: SecurityKey,
    pub exchange: Exchange,
    pub product: Product,
}
impl Instrument {
    pub fn validate(&self) -> QfResult<()> {
        let valid = match (self.exchange, self.product) {
            (Exchange::Shanghai, Product::MainBoardStock | Product::StarStock)
            | (Exchange::Shenzhen, Product::MainBoardStock | Product::ChiNextStock)
            | (Exchange::Beijing, Product::BeijingStock) => true,
            (Exchange::Shanghai | Exchange::Shenzhen, product) if product.is_etf() => true,
            (_, Product::Index) => {
                return Err(self.error(
                    None,
                    ErrorCode::InvalidOrder,
                    "product",
                    "指数仅用于研究和基准，不可下单",
                ));
            }
            _ => false,
        };
        if !valid {
            return Err(self.error(
                None,
                ErrorCode::RuleUnavailable,
                "identity",
                "市场与产品身份未知或冲突",
            ));
        }
        Ok(())
    }
    pub(crate) fn error(
        &self,
        date: Option<&RuleDate>,
        code: ErrorCode,
        field: &str,
        message: &str,
    ) -> QfError {
        let mut error = QfError::new(code, "market_rules", message);
        error
            .scope
            .insert("security".into(), self.security.as_str().into());
        if let Some(date) = date {
            error.scope.insert("date".into(), date.to_string());
        }
        error.scope.insert("field".into(), field.into());
        error
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "reference", rename_all = "snake_case")]
pub enum RuleOrigin {
    Official(String),
    Synthetic(String),
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuleUse {
    Market,
    Synthetic(String),
}
pub(crate) fn same_origin_namespace(a: &RuleOrigin, b: &RuleOrigin) -> bool {
    match (a, b) {
        (RuleOrigin::Official(_), RuleOrigin::Official(_)) => true,
        (RuleOrigin::Synthetic(a), RuleOrigin::Synthetic(b)) => a == b,
        _ => false,
    }
}
impl RuleOrigin {
    pub(crate) fn permits(&self, usage: &RuleUse) -> bool {
        match (self, usage) {
            (Self::Official(_), RuleUse::Market) => true,
            (Self::Synthetic(a), RuleUse::Synthetic(b)) => a == b,
            _ => false,
        }
    }
    pub(crate) fn validate(&self) -> QfResult<()> {
        let reference = match self {
            Self::Official(v) | Self::Synthetic(v) => v,
        };
        crate::types::keys::label(reference, 128)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SellAvailability {
    SameSession,
    NextSession,
    CrossBorderUnderlying,
}

/// A small reference to local trading phases; D03 joins this to real calendars
/// and TradingSession. Endpoints are boundaries, not an invented daily calendar.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionTemplate {
    ChinaAuction,
    ShanghaiFundBefore2026,
}
impl SessionTemplate {
    pub fn timezone(self) -> &'static str {
        "Asia/Shanghai"
    }
    pub fn continuous_windows(self) -> &'static [(u16, u16)] {
        match self {
            Self::ChinaAuction => &[(570, 690), (780, 897)],
            Self::ShanghaiFundBefore2026 => &[(570, 690), (780, 900)],
        }
    }
    pub fn opening_call(self) -> (u16, u16) {
        (555, 565)
    }
    pub fn closing_call(self) -> Option<(u16, u16)> {
        match self {
            Self::ChinaAuction => Some((897, 900)),
            Self::ShanghaiFundBefore2026 => None,
        }
    }
}

/// D03 supplies the actual phase; these values do not generate a calendar.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AuctionPhase {
    OpeningCall,
    Continuous,
    ClosingCall,
    TemporaryHaltCall,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NoLimitCallPolicy {
    ShanghaiMainBoard,
    ShenzhenStock,
    Unrestricted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RiskState {
    Normal,
    RiskWarning,
    Delisting,
    Unknown,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TradingStatus {
    Trading,
    Halted,
    Unknown,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FundPriceBand {
    TenPercent,
    TwentyPercent,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ListingKind {
    Ipo,
    Relisting,
    Other,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ListingWindow {
    pub first_trading_date: RuleDate,
    /// Inclusive final trading date, if terminated within verified coverage.
    pub last_trading_date: Option<RuleDate>,
    /// None for last_trading_date means still listed only through this date.
    pub verified_through: RuleDate,
    pub kind: ListingKind,
    pub session_ordinal: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NoLimitReason {
    Ipo,
    Relisting,
    DelistingFirstSession,
    ExchangeNotice,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum DailyPriceLimit {
    Unknown,
    Limited {
        rate: ExactDecimal,
        lower: Price,
        upper: Price,
    },
    /// The ordinal counts actual applicable trading sessions from D03/D04.
    /// ExchangeNotice also requires a specific accepted daily source reference.
    NoLimit {
        reason: NoLimitReason,
        session_ordinal: u32,
        basis: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradeDayFacts {
    pub security: SecurityKey,
    pub date: RuleDate,
    pub session: SessionKey,
    pub listing: Option<ListingWindow>,
    pub status: TradingStatus,
    pub risk: RiskState,
    pub price_limit: DailyPriceLimit,
    /// Required for ETF price-band classification; never inferred from a code.
    pub fund_price_band: Option<FundPriceBand>,
    /// A foreign underlying's round-trip capability must be an explicit fact.
    pub underlying_same_session: Option<bool>,
    pub delisting_session_ordinal: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradePermissions {
    pub buy: Option<bool>,
    pub sell: Option<bool>,
    /// Includes the actual board/product suitability permission, not a
    /// fabricated calculation using a backtest's initial_cash.
    pub product_buy: Option<bool>,
    pub risk_disclosure: Option<bool>,
    pub delisting_buy: Option<bool>,
    /// Already bought plus all still-open buys for the same investor/security/day.
    pub risk_bought_or_open: Option<Quantity>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PositionAvailability {
    /// Remaining settled holdings; sells already executed are removed by D09.
    pub settled: Quantity,
    pub bought_today: Quantity,
    pub frozen: Quantity,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceCage {
    pub fraction: ExactDecimal,
    pub minimum_ticks: u32,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradingRule {
    pub exchange: Exchange,
    pub product: Product,
    pub effective: EffectiveRange,
    pub origin: RuleOrigin,
    pub session_template: SessionTemplate,
    pub price_tick: Price,
    pub minimum_buy: Quantity,
    pub buy_step: QuantityStep,
    pub minimum_sell: Quantity,
    pub sell_step: QuantityStep,
    pub maximum_limit: Quantity,
    pub maximum_market: Quantity,
    pub sell_availability: SellAvailability,
    pub normal_limit_rate: ExactDecimal,
    pub risk_limit_rate: ExactDecimal,
    pub ipo_no_limit_sessions: u32,
    pub relisting_no_limit: bool,
    pub risk_rules_verified: bool,
    pub risk_buy_cap: Option<Quantity>,
    pub risk_requires_limit_order: bool,
    pub continuous_cage: Option<PriceCage>,
    pub no_limit_call_policy: NoLimitCallPolicy,
    pub market_requires_daily_limit: bool,
    /// Official Shanghai/Shenzhen price-band rounding. Beijing bounds must be
    /// supplied as daily facts: no unverified rounding convention is inserted.
    pub band_rounding: Option<RoundingPolicy>,
}
impl TradingRule {
    pub fn validate(&self) -> QfResult<()> {
        self.effective.validate()?;
        self.origin.validate()?;
        Instrument {
            security: SecurityKey::new("rule-profile")?,
            exchange: self.exchange,
            product: self.product,
        }
        .validate()?;
        let range = ExactDecimal::ZERO..=ExactDecimal::ONE;
        if self.minimum_buy == Quantity::ZERO
            || self.minimum_sell == Quantity::ZERO
            || self.maximum_limit < self.minimum_buy
            || self.maximum_limit < self.minimum_sell
            || self.maximum_market < self.minimum_buy
            || self.maximum_market < self.minimum_sell
            || !range.contains(&self.normal_limit_rate)
            || !range.contains(&self.risk_limit_rate)
            || self.normal_limit_rate.is_zero()
            || self.risk_limit_rate.is_zero()
            || self.continuous_cage.is_some_and(|c| {
                c.fraction <= ExactDecimal::ZERO || c.fraction >= ExactDecimal::ONE
            })
        {
            return Err(QfError::new(
                ErrorCode::RuleUnavailable,
                "rule_profile",
                "规则数量、价格或限制范围冲突",
            ));
        }
        Ok(())
    }
    /// Derive a verified exchange daily band from its published reference price
    /// (including ex-rights reference). No previous bar-close substitution.
    pub fn price_band(&self, reference: Price, rate: ExactDecimal) -> QfResult<(Price, Price)> {
        if !self.price_on_grid(reference)? {
            return Err(QfError::new(
                ErrorCode::RuleUnavailable,
                "price_band",
                "权威基准价的价格步长冲突",
            ));
        }
        let rounding = self.band_rounding.ok_or_else(|| {
            QfError::new(
                ErrorCode::RuleUnavailable,
                "price_band",
                "该市场需要权威当日价格上下限",
            )
        })?;
        if rate <= ExactDecimal::ZERO || rate >= ExactDecimal::ONE {
            return Err(QfError::new(
                ErrorCode::RuleUnavailable,
                "price_band",
                "涨跌幅比例无效",
            ));
        }
        let tick = self.price_tick.get();
        let round = |raw: ExactDecimal| -> QfResult<ExactDecimal> {
            raw.div_rounded(tick, 0, rounding)?.checked_mul(tick)
        };
        let lower = round(
            reference
                .get()
                .checked_mul(ExactDecimal::ONE.checked_sub(rate)?)?,
        )?;
        let upper = round(
            reference
                .get()
                .checked_mul(ExactDecimal::ONE.checked_add(rate)?)?,
        )?;
        // Published minimum one-tick movement and price floor.
        let lower = lower.min(reference.get().checked_sub(tick)?).max(tick);
        let upper = upper.max(reference.get().checked_add(tick)?);
        Ok((Price::new(lower)?, Price::new(upper)?))
    }
    fn price_on_grid(&self, price: Price) -> QfResult<bool> {
        let units =
            price
                .get()
                .div_rounded(self.price_tick.get(), 0, RoundingPolicy::TowardZero)?;
        Ok(units.checked_mul(self.price_tick.get())? == price.get())
    }
}

#[derive(Debug, Clone)]
pub struct RuleBook {
    records: Vec<TradingRule>,
}
impl RuleBook {
    pub fn new(records: Vec<TradingRule>) -> QfResult<Self> {
        if records.len() > 1024 {
            return Err(QfError::new(
                ErrorCode::ResourceLimit,
                "rule_book",
                "规则配置超过预算",
            ));
        }
        for (i, rule) in records.iter().enumerate() {
            rule.validate()?;
            if records[..i].iter().any(|r| {
                r.exchange == rule.exchange
                    && r.product == rule.product
                    && same_origin_namespace(&r.origin, &rule.origin)
                    && r.effective.overlaps(&rule.effective)
            }) {
                return Err(QfError::new(
                    ErrorCode::RuleUnavailable,
                    "rule_book",
                    "同一规则存在重叠有效期",
                ));
            }
        }
        Ok(Self { records })
    }
    pub fn resolve<'a>(
        &'a self,
        instrument: &'a Instrument,
        facts: &'a TradeDayFacts,
        usage: &RuleUse,
    ) -> QfResult<ResolvedTradingRule<'a>> {
        instrument.validate().map_err(|mut e| {
            e.scope.insert("date".into(), facts.date.to_string());
            e
        })?;
        if instrument.security != facts.security {
            return Err(instrument.error(
                Some(&facts.date),
                ErrorCode::RuleUnavailable,
                "security",
                "当日事实与标的身份不一致",
            ));
        }
        let mut applicable = self.records.iter().filter(|r| {
            r.exchange == instrument.exchange
                && r.product == instrument.product
                && r.effective.contains(&facts.date)
                && r.origin.permits(usage)
        });
        let rule = applicable.next().ok_or_else(|| {
            instrument.error(
                Some(&facts.date),
                ErrorCode::RuleUnavailable,
                "effective_date",
                "此市场、产品及日期没有已核验规则",
            )
        })?;
        if applicable.next().is_some() {
            return Err(instrument.error(
                Some(&facts.date),
                ErrorCode::RuleUnavailable,
                "effective_date",
                "适用规则冲突",
            ));
        }
        let resolved = ResolvedTradingRule {
            rule,
            instrument,
            facts,
        };
        resolved.validate_facts()?;
        Ok(resolved)
    }
}

#[derive(Debug)]
pub struct ResolvedTradingRule<'a> {
    rule: &'a TradingRule,
    instrument: &'a Instrument,
    facts: &'a TradeDayFacts,
}
impl ResolvedTradingRule<'_> {
    pub fn rule(&self) -> &TradingRule {
        self.rule
    }
    pub fn session(&self) -> &SessionKey {
        &self.facts.session
    }
    fn error(&self, code: ErrorCode, field: &str, message: &str) -> QfError {
        self.instrument
            .error(Some(&self.facts.date), code, field, message)
    }
    fn validate_facts(&self) -> QfResult<()> {
        let listing = self.facts.listing.as_ref().ok_or_else(|| {
            self.error(ErrorCode::RuleUnavailable, "listing", "上市及退市边界未知")
        })?;
        if listing.verified_through < listing.first_trading_date
            || listing
                .last_trading_date
                .as_ref()
                .is_some_and(|d| d < &listing.first_trading_date || d > &listing.verified_through)
        {
            return Err(self.error(ErrorCode::RuleUnavailable, "listing", "上市边界事实冲突"));
        }
        if self.facts.date > listing.verified_through {
            return Err(self.error(ErrorCode::RuleUnavailable, "listing", "当日上市状态未核验"));
        }
        if self.facts.date < listing.first_trading_date
            || listing
                .last_trading_date
                .as_ref()
                .is_some_and(|d| &self.facts.date > d)
        {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "listing",
                "标的尚未上市或已结束交易",
            ));
        }
        if listing.kind == ListingKind::Unknown
            || listing.session_ordinal == 0
            || (self.facts.date == listing.first_trading_date) != (listing.session_ordinal == 1)
            || (self.facts.risk == RiskState::Delisting
                && self.facts.delisting_session_ordinal.is_none_or(|n| n == 0))
        {
            return Err(self.error(
                ErrorCode::RuleUnavailable,
                "listing_session",
                "上市类型或实际交易会话序号未知或冲突",
            ));
        }
        if self.facts.status == TradingStatus::Unknown || self.facts.risk == RiskState::Unknown {
            return Err(self.error(
                ErrorCode::RuleUnavailable,
                "trading_status",
                "当日交易或风险状态未知",
            ));
        }
        if self.instrument.product.is_etf() && self.facts.risk != RiskState::Normal {
            return Err(self.error(
                ErrorCode::RuleUnavailable,
                "risk",
                "股票风险状态不可套用到ETF",
            ));
        }
        if self.facts.risk != RiskState::Normal && !self.rule.risk_rules_verified {
            return Err(self.error(
                ErrorCode::RuleUnavailable,
                "risk",
                "风险警示或退市规则的生效日未核验",
            ));
        }
        self.sell_availability()?;
        match &self.facts.price_limit {
            DailyPriceLimit::Unknown => {
                return Err(self.error(
                    ErrorCode::RuleUnavailable,
                    "price_limit",
                    "当日价格限制未知",
                ));
            }
            DailyPriceLimit::Limited { rate, lower, upper } => {
                if self.instrument.product.is_stock()
                    && ((listing.kind == ListingKind::Ipo
                        && listing.session_ordinal <= self.rule.ipo_no_limit_sessions)
                        || (listing.kind == ListingKind::Relisting && listing.session_ordinal == 1)
                        || (self.facts.risk == RiskState::Delisting
                            && self.facts.delisting_session_ordinal == Some(1)))
                {
                    return Err(self.error(
                        ErrorCode::RuleUnavailable,
                        "price_limit",
                        "首日或IPO豁免会话与涨跌幅事实冲突",
                    ));
                }
                let expected = if self.instrument.product.is_etf() {
                    match self.facts.fund_price_band.ok_or_else(|| {
                        self.error(
                            ErrorCode::RuleUnavailable,
                            "fund_price_band",
                            "ETF价格限制细分类未知",
                        )
                    })? {
                        FundPriceBand::TenPercent => ExactDecimal::ONE.div_rounded(
                            ExactDecimal::from_integer(10),
                            1,
                            RoundingPolicy::TowardZero,
                        )?,
                        FundPriceBand::TwentyPercent
                            if self.instrument.product == Product::EquityEtf =>
                        {
                            ExactDecimal::from_integer(2).div_rounded(
                                ExactDecimal::from_integer(10),
                                1,
                                RoundingPolicy::TowardZero,
                            )?
                        }
                        _ => {
                            return Err(self.error(
                                ErrorCode::RuleUnavailable,
                                "fund_price_band",
                                "此ETF不适用股票指数20%分类",
                            ));
                        }
                    }
                } else if self.facts.risk == RiskState::RiskWarning {
                    self.rule.risk_limit_rate
                } else {
                    self.rule.normal_limit_rate
                };
                if *rate != expected
                    || lower > upper
                    || !self.rule.price_on_grid(*lower)?
                    || !self.rule.price_on_grid(*upper)?
                {
                    return Err(self.error(
                        ErrorCode::RuleUnavailable,
                        "price_limit",
                        "当日价格限制与适用规则冲突",
                    ));
                }
            }
            DailyPriceLimit::NoLimit {
                reason,
                session_ordinal,
                basis,
            } => {
                let valid = *session_ordinal > 0
                    && match reason {
                        NoLimitReason::Ipo => {
                            self.instrument.product.is_stock()
                                && listing.kind == ListingKind::Ipo
                                && *session_ordinal == listing.session_ordinal
                                && *session_ordinal <= self.rule.ipo_no_limit_sessions
                                && self.facts.risk == RiskState::Normal
                        }
                        NoLimitReason::Relisting => {
                            self.instrument.product.is_stock()
                                && listing.kind == ListingKind::Relisting
                                && listing.session_ordinal == 1
                                && *session_ordinal == 1
                                && self.rule.relisting_no_limit
                        }
                        NoLimitReason::DelistingFirstSession => {
                            self.instrument.product.is_stock()
                                && self.facts.delisting_session_ordinal == Some(1)
                                && *session_ordinal == 1
                                && self.facts.risk == RiskState::Delisting
                        }
                        NoLimitReason::ExchangeNotice => true,
                    };
                if !valid || crate::types::keys::label(basis, 256).is_err() {
                    return Err(self.error(
                        ErrorCode::RuleUnavailable,
                        "price_limit",
                        "无涨跌幅限制的条件或当日依据冲突",
                    ));
                }
            }
        }
        Ok(())
    }
    pub fn sell_availability(&self) -> QfResult<SellAvailability> {
        match self.rule.sell_availability {
            SellAvailability::CrossBorderUnderlying => match self.facts.underlying_same_session {
                Some(true) => Ok(SellAvailability::SameSession),
                Some(false) => Ok(SellAvailability::NextSession),
                None => Err(self.error(
                    ErrorCode::RuleUnavailable,
                    "underlying_same_session",
                    "跨境ETF底层证券回转规则未知",
                )),
            },
            policy => Ok(policy),
        }
    }
    pub fn sellable(&self, position: &PositionAvailability) -> QfResult<Quantity> {
        let eligible = match self.sell_availability()? {
            SellAvailability::SameSession => position.settled.checked_add(position.bought_today)?,
            SellAvailability::NextSession => position.settled,
            SellAvailability::CrossBorderUnderlying => unreachable!("resolved above"),
        };
        if position.frozen > eligible {
            return Err(self.error(
                ErrorCode::RuleUnavailable,
                "frozen",
                "冻结数量超过适用可卖数量",
            ));
        }
        eligible.checked_sub(position.frozen)
    }
    pub fn validate_quantity(
        &self,
        side: Side,
        quantity: Quantity,
        available: Quantity,
        market_order: bool,
    ) -> QfResult<()> {
        if quantity == Quantity::ZERO {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "quantity",
                "实际委托数量必须为正；无动作由订单模块处理",
            ));
        }
        let maximum = if market_order {
            self.rule.maximum_market
        } else {
            self.rule.maximum_limit
        };
        if quantity > maximum {
            return Err(self.error(ErrorCode::InvalidOrder, "quantity", "超过单笔申报数量上限"));
        }
        let (minimum, step) = match side {
            Side::Buy => (self.rule.minimum_buy, self.rule.buy_step.get()),
            Side::Sell => (self.rule.minimum_sell, self.rule.sell_step.get()),
        };
        if side == Side::Sell {
            if quantity > available {
                return Err(self.error(
                    ErrorCode::InsufficientSellable,
                    "quantity",
                    "委托超过可卖数量",
                ));
            }
            // No separate odd-lot order may leave some of the available odd lot.
            if quantity == available {
                return Ok(());
            }
            let full_odd_part = step > 1 && quantity.get() == available.get() % step;
            if (quantity < minimum && !full_odd_part)
                || (quantity.get() % step != 0 && quantity.get() % step != available.get() % step)
            {
                return Err(self.error(
                    ErrorCode::InvalidOrder,
                    "quantity",
                    "零股必须一次出售，或满足该产品卖出单位",
                ));
            }
        } else if quantity < minimum || quantity.get() % step != 0 {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "quantity",
                "买入数量不满足最小申报单位",
            ));
        }
        Ok(())
    }
    /// Value/percent targets use this explicit downward quantization. It does
    /// not make a rejected quantity order silently legal or reserve cash.
    pub fn quantize_buy(&self, requested: Quantity) -> Quantity {
        let value = self.rule.buy_step.quantize(requested);
        if value < self.rule.minimum_buy {
            Quantity::ZERO
        } else {
            value
        }
    }
    pub fn validate_permissions(
        &self,
        side: Side,
        quantity: Quantity,
        market_order: bool,
        permissions: &TradePermissions,
    ) -> QfResult<()> {
        let require = |fact: Option<bool>, field: &str| -> QfResult<()> {
            match fact {
                Some(true) => Ok(()),
                Some(false) => {
                    Err(self.error(ErrorCode::InvalidOrder, field, "账户没有所需交易权限"))
                }
                None => Err(self.error(ErrorCode::RuleUnavailable, field, "所需账户权限未知")),
            }
        };
        require(
            if side == Side::Buy {
                permissions.buy
            } else {
                permissions.sell
            },
            "side_permission",
        )?;
        if side == Side::Buy {
            require(permissions.product_buy, "product_buy")?;
            if self.facts.risk != RiskState::Normal {
                require(permissions.risk_disclosure, "risk_disclosure")?;
            }
            if self.facts.risk == RiskState::Delisting {
                require(permissions.delisting_buy, "delisting_buy")?;
            }
            if self.facts.risk == RiskState::RiskWarning
                && let Some(cap) = self.rule.risk_buy_cap
            {
                let already = permissions.risk_bought_or_open.ok_or_else(|| {
                    self.error(
                        ErrorCode::RuleUnavailable,
                        "risk_bought_or_open",
                        "风险警示股票累计买入及挂单数量未知",
                    )
                })?;
                if already.checked_add(quantity)? > cap {
                    return Err(self.error(
                        ErrorCode::InvalidOrder,
                        "risk_buy_cap",
                        "超过风险警示股票当日买入上限",
                    ));
                }
            }
        }
        if market_order
            && self.facts.risk != RiskState::Normal
            && self.rule.risk_requires_limit_order
        {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "order_style",
                "此风险警示标的仅接受限价委托",
            ));
        }
        Ok(())
    }
    /// Filling while halted is forbidden; exchange acceptance of orders during
    /// a halt is a separate question. This does not pretend a halt is missing data.
    pub fn validate_execution(&self, price: Price) -> QfResult<()> {
        if self.facts.status == TradingStatus::Halted {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "trading_status",
                "标的停牌，不能成交",
            ));
        }
        self.validate_daily_price(price)
    }
    fn validate_daily_price(&self, price: Price) -> QfResult<()> {
        if !self.rule.price_on_grid(price)? {
            return Err(self.error(ErrorCode::InvalidOrder, "price", "交易价不满足价格步长"));
        }
        if let DailyPriceLimit::Limited { lower, upper, .. } = self.facts.price_limit
            && (price < lower || price > upper)
        {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "price_limit",
                "交易价超出当日涨跌停范围",
            ));
        }
        Ok(())
    }
    /// Auction limit admission, including no-limit listing days. A temporary
    /// halt can accept a limit order but validate_execution still rejects fills.
    /// For opening calls, reference is the published previous/reference price;
    /// for later calls it is the exchange-defined last-trade/reference fact.
    pub fn validate_limit_submission(
        &self,
        side: Side,
        price: Price,
        phase: AuctionPhase,
        reference: Option<Price>,
    ) -> QfResult<()> {
        self.validate_daily_price(price)?;
        if phase == AuctionPhase::Continuous {
            return self.validate_continuous_limit(side, price, reference);
        }
        if !matches!(self.facts.price_limit, DailyPriceLimit::NoLimit { .. })
            || self.rule.no_limit_call_policy == NoLimitCallPolicy::Unrestricted
        {
            return Ok(());
        }
        let reference = reference.ok_or_else(|| {
            self.error(
                ErrorCode::RuleUnavailable,
                "call_reference",
                "集合竞价有效价格范围的基准价未知",
            )
        })?;
        if !self.rule.price_on_grid(reference)? {
            return Err(self.error(
                ErrorCode::RuleUnavailable,
                "call_reference",
                "申报基准价的价格步长冲突",
            ));
        }
        let (lower, upper) = if phase == AuctionPhase::OpeningCall {
            let lower = if self.rule.no_limit_call_policy == NoLimitCallPolicy::ShanghaiMainBoard {
                self.round_bound(
                    reference.get().checked_mul("0.5".parse()?)?,
                    reference,
                    Side::Sell,
                )?
            } else {
                self.rule.price_tick
            };
            (
                lower,
                self.round_bound(
                    reference.get().checked_mul(ExactDecimal::from_integer(9))?,
                    reference,
                    Side::Buy,
                )?,
            )
        } else {
            self.rule.price_band(reference, "0.10".parse()?)?
        };
        if price < lower || price > upper {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "call_price_range",
                "无涨跌幅限制日的集合竞价申报价超出有效范围",
            ));
        }
        Ok(())
    }
    /// Exchange market-order phase capability only. The simulation's market
    /// proxy/protection/slippage is declared by D06/D07, not fabricated here.
    pub fn validate_market_phase(&self, phase: AuctionPhase) -> QfResult<()> {
        if phase != AuctionPhase::Continuous
            || self.facts.status == TradingStatus::Halted
            || (self.rule.market_requires_daily_limit
                && matches!(self.facts.price_limit, DailyPriceLimit::NoLimit { .. }))
        {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "market_phase",
                "此竞价阶段或无涨跌幅限制日不接受该市场市价申报",
            ));
        }
        Ok(())
    }
    fn round_bound(&self, raw: ExactDecimal, reference: Price, side: Side) -> QfResult<Price> {
        let rounding = self.rule.band_rounding.ok_or_else(|| {
            self.error(
                ErrorCode::RuleUnavailable,
                "price_rounding",
                "申报价格范围的舍入方式未核验",
            )
        })?;
        let tick = self.rule.price_tick.get();
        let bound = raw.div_rounded(tick, 0, rounding)?.checked_mul(tick)?;
        let bound = match side {
            Side::Buy => bound.max(reference.get().checked_add(tick)?),
            Side::Sell => bound.min(reference.get().checked_sub(tick)?),
        };
        Price::new(bound.max(tick))
    }
    /// Tick admission cage; caller provides the exchange-defined public opposite
    /// book/own book/last/reference selection. None is never silently yesterday.
    pub fn validate_continuous_limit(
        &self,
        side: Side,
        price: Price,
        reference: Option<Price>,
    ) -> QfResult<()> {
        if !self.rule.price_on_grid(price)? {
            return Err(self.error(ErrorCode::InvalidOrder, "price", "申报价不满足价格步长"));
        }
        let Some(cage) = self.rule.continuous_cage else {
            return Ok(());
        };
        let reference = reference.ok_or_else(|| {
            self.error(
                ErrorCode::RuleUnavailable,
                "cage_reference",
                "有效申报价格范围的基准价未知",
            )
        })?;
        if !self.rule.price_on_grid(reference)? {
            return Err(self.error(
                ErrorCode::RuleUnavailable,
                "cage_reference",
                "申报基准价的价格步长冲突",
            ));
        }
        let offset = self
            .rule
            .price_tick
            .get()
            .checked_mul(ExactDecimal::from_integer(i64::from(cage.minimum_ticks)))?;
        let bound = match side {
            Side::Buy => reference
                .get()
                .checked_mul(ExactDecimal::ONE.checked_add(cage.fraction)?)?
                .max(reference.get().checked_add(offset)?),
            Side::Sell => reference
                .get()
                .checked_mul(ExactDecimal::ONE.checked_sub(cage.fraction)?)?
                .min(reference.get().checked_sub(offset)?),
        };
        let bound = if self.rule.band_rounding.is_some() {
            self.round_bound(bound, reference, side)?.get()
        } else {
            // Beijing's original does not specify boundary rounding. Clearly
            // inside/outside prices are decidable; the one ambiguous tick is
            // unavailable, rather than silently adopting Shanghai rounding.
            let tick = self.rule.price_tick.get();
            let floor = bound
                .div_rounded(tick, 0, RoundingPolicy::TowardZero)?
                .checked_mul(tick)?;
            if bound > ExactDecimal::ZERO
                && floor != bound
                && ((side == Side::Buy && price.get() == floor.checked_add(tick)?)
                    || (side == Side::Sell && price.get() == floor))
            {
                return Err(self.error(
                    ErrorCode::RuleUnavailable,
                    "price_rounding",
                    "此边界价格需要权威申报价格舍入事实",
                ));
            }
            bound
        };
        if (side == Side::Buy && price.get() > bound) || (side == Side::Sell && price.get() < bound)
        {
            return Err(self.error(
                ErrorCode::InvalidOrder,
                "price_cage",
                "连续竞价申报价超出有效范围",
            ));
        }
        Ok(())
    }
}

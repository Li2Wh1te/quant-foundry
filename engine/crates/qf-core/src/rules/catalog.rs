//! Small offline verified catalog. Coverage limits are intentional: absence
//! never triggers a lookup or falls back to another exchange/product's rules.
//! Source IDs, inspected originals and gaps are in engine/rules/SOURCES.md.
use super::date::{EffectiveRange, RuleDate};
use super::fees::{CommissionConfig, FeeScope, InvestorKind, REQUIRED_COMPONENTS, ScopedFeeConfig};
use super::market::*;
use super::{CostOverrides, DatedFeeComponent, FeeComponentKind, FeeConfig, FeeFact};
use crate::orders::Side;
use crate::types::{ExactDecimal, Price, Quantity, QuantityStep, RoundingPolicy};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};

pub const VERIFIED_THROUGH: &str = "2026-10-10";
fn range(from: &str, through: &str) -> QfResult<EffectiveRange> {
    Ok(EffectiveRange {
        from: from.parse()?,
        through: through.parse()?,
    })
}

/// Verified 2026 auction profiles. Earlier dated editions are not inferred from
/// their publication dates. Pending Beijing risk/after-hours activation is not
/// activated by the date printed in the general trading rule.
pub fn verified_market_rules() -> QfResult<RuleBook> {
    let mut rules = Vec::new();
    for exchange in [Exchange::Shanghai, Exchange::Shenzhen, Exchange::Beijing] {
        let products = match exchange {
            Exchange::Shanghai => vec![Product::MainBoardStock, Product::StarStock],
            Exchange::Shenzhen => vec![Product::MainBoardStock, Product::ChiNextStock],
            Exchange::Beijing => vec![Product::BeijingStock],
            Exchange::Unknown => unreachable!(),
        };
        let products: Vec<_> = if exchange != Exchange::Beijing {
            products
                .into_iter()
                .chain([
                    Product::EquityEtf,
                    Product::BondEtf,
                    Product::MoneyEtf,
                    Product::GoldEtf,
                    Product::CommodityEtf,
                    Product::CrossBorderEtf,
                ])
                .collect()
        } else {
            products
        };
        for product in products {
            let star = product == Product::StarStock;
            let beijing = exchange == Exchange::Beijing;
            let growth = product == Product::ChiNextStock;
            let etf = product.is_etf();
            let rate: ExactDecimal = if beijing {
                "0.30"
            } else if star || growth {
                "0.20"
            } else {
                "0.10"
            }
            .parse()?;
            let reference = match exchange {
                Exchange::Shanghai => "sse-trading-2026",
                Exchange::Shenzhen => "szse-trading-2026",
                Exchange::Beijing => "bse-trading-2026",
                Exchange::Unknown => unreachable!(),
            };
            rules.push(TradingRule {
                exchange,
                product,
                effective: range("2026-07-06", VERIFIED_THROUGH)?,
                origin: RuleOrigin::Official(reference.into()),
                session_template: SessionTemplate::ChinaAuction,
                price_tick: if etf { "0.001" } else { "0.01" }.parse::<Price>()?,
                minimum_buy: Quantity::new(if star { 200 } else { 100 })?,
                buy_step: QuantityStep::new(if star || beijing { 1 } else { 100 })?,
                minimum_sell: Quantity::new(if star { 200 } else { 100 })?,
                sell_step: QuantityStep::new(if star || beijing { 1 } else { 100 })?,
                maximum_limit: Quantity::new(if star {
                    100_000
                } else if growth {
                    300_000
                } else {
                    1_000_000
                })?,
                maximum_market: Quantity::new(if star {
                    50_000
                } else if growth {
                    150_000
                } else {
                    1_000_000
                })?,
                sell_availability: match product {
                    Product::BondEtf
                    | Product::MoneyEtf
                    | Product::GoldEtf
                    | Product::CommodityEtf => SellAvailability::SameSession,
                    Product::CrossBorderEtf => SellAvailability::CrossBorderUnderlying,
                    _ => SellAvailability::NextSession,
                },
                normal_limit_rate: rate,
                risk_limit_rate: rate,
                ipo_no_limit_sessions: if beijing {
                    1
                } else if etf {
                    0
                } else {
                    5
                },
                relisting_no_limit: !star && !beijing && !etf,
                risk_rules_verified: !beijing,
                risk_buy_cap: if !etf && !star && !beijing {
                    Some(Quantity::new(500_000)?)
                } else {
                    None
                },
                risk_requires_limit_order: exchange == Exchange::Shanghai
                    && product == Product::MainBoardStock,
                continuous_cage: if etf {
                    None
                } else {
                    Some(PriceCage {
                        fraction: if beijing { "0.05" } else { "0.02" }.parse()?,
                        minimum_ticks: if star { 0 } else { 10 },
                    })
                },
                no_limit_call_policy: match (exchange, product) {
                    (Exchange::Shanghai, Product::MainBoardStock) => {
                        NoLimitCallPolicy::ShanghaiMainBoard
                    }
                    (Exchange::Shenzhen, product) if product.is_stock() => {
                        NoLimitCallPolicy::ShenzhenStock
                    }
                    _ => NoLimitCallPolicy::Unrestricted,
                },
                market_requires_daily_limit: exchange != Exchange::Shanghai,
                band_rounding: if beijing {
                    None
                } else {
                    Some(RoundingPolicy::HalfUp)
                },
            });
        }
    }
    RuleBook::new(rules)
}

/// Trusted accepted fact with exact applicability, not a user-editable fee
/// override. FeeCatalog validates intersections before composing D01 FeeConfig.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeeRuleRecord {
    pub exchange: Exchange,
    pub product: Product,
    pub side: Side,
    pub investor: InvestorKind,
    pub kind: FeeComponentKind,
    pub effective: EffectiveRange,
    pub origin: RuleOrigin,
    pub fact: FeeFact,
}
#[derive(Debug, Clone)]
pub struct FeeCatalog {
    records: Vec<FeeRuleRecord>,
}
impl FeeCatalog {
    pub fn new(records: Vec<FeeRuleRecord>) -> QfResult<Self> {
        if records.len() > 4096 {
            return Err(QfError::new(
                ErrorCode::ResourceLimit,
                "fee_catalog",
                "费用配置超过预算",
            ));
        }
        for (i, record) in records.iter().enumerate() {
            record.effective.validate()?;
            record.origin.validate()?;
            Instrument {
                security: crate::types::SecurityKey::new("fee-profile")?,
                exchange: record.exchange,
                product: record.product,
            }
            .validate()?;
            if matches!(record.investor, InvestorKind::Unknown | InvestorKind::Other) {
                return Err(QfError::new(
                    ErrorCode::RuleUnavailable,
                    "fee_catalog",
                    "费用投资者范围未核验",
                ));
            }
            let config = FeeConfig {
                commission_rate: ExactDecimal::ZERO,
                minimum_commission: ExactDecimal::ZERO,
                currency: crate::types::market::Currency::CNY,
                settlement_scale: 2,
                rounding: RoundingPolicy::HalfEven,
                components: vec![DatedFeeComponent {
                    kind: record.kind,
                    effective_from: record.effective.from.to_string(),
                    effective_through: record.effective.through.to_string(),
                    included_in_commission: false,
                    fact: record.fact.clone(),
                }],
                synthetic_model: None,
            };
            config.validate()?;
            if records[..i].iter().any(|r| {
                r.exchange == record.exchange
                    && r.product == record.product
                    && r.side == record.side
                    && r.investor == record.investor
                    && r.kind == record.kind
                    && same_origin_namespace(&r.origin, &record.origin)
                    && r.effective.overlaps(&record.effective)
            }) {
                return Err(QfError::new(
                    ErrorCode::RuleUnavailable,
                    "fee_catalog",
                    "费用适用范围重叠或项目重复",
                ));
            }
        }
        Ok(Self { records })
    }
    pub fn fact(
        &self,
        scope: &FeeScope,
        kind: FeeComponentKind,
        date: &RuleDate,
        usage: &RuleUse,
    ) -> QfResult<&FeeRuleRecord> {
        scope.instrument.validate()?;
        if !scope.origin.permits(usage) {
            return Err(scope.instrument.error(
                Some(date),
                ErrorCode::RuleUnavailable,
                "fee_origin",
                "真实费用与合成费用来源不能混用",
            ));
        }
        let mut matches = self.records.iter().filter(|r| {
            self.matches(scope, r, usage) && r.kind == kind && r.effective.contains(date)
        });
        let record = matches
            .next()
            .ok_or_else(|| self.missing(scope, date, kind))?;
        if matches.next().is_some() {
            return Err(self.missing(scope, date, kind));
        }
        Ok(record)
    }
    fn matches(&self, scope: &FeeScope, r: &FeeRuleRecord, usage: &RuleUse) -> bool {
        r.exchange == scope.instrument.exchange
            && r.product == scope.instrument.product
            && r.side == scope.side
            && r.investor == scope.investor
            && r.origin.permits(usage)
    }
    fn missing(&self, scope: &FeeScope, date: &RuleDate, kind: FeeComponentKind) -> QfError {
        let mut error = scope.instrument.error(
            Some(date),
            ErrorCode::RuleUnavailable,
            &format!("{kind:?}"),
            "该市场、产品、投资者、方向及日期费用未核验或冲突",
        );
        error.operation = "fee_catalog".into();
        error
    }
    pub fn compose(
        &self,
        scope: FeeScope,
        effective: &EffectiveRange,
        commission: &CommissionConfig,
        overrides: &CostOverrides,
        usage: &RuleUse,
    ) -> QfResult<ScopedFeeConfig> {
        effective.validate()?;
        scope.instrument.validate()?;
        if !scope.origin.permits(usage) {
            return Err(self.missing(&scope, &effective.from, FeeComponentKind::StampDuty));
        }
        let commission = commission.with_overrides(overrides)?;
        let mut components = Vec::new();
        for kind in REQUIRED_COMPONENTS {
            let mut records: Vec<_> = self
                .records
                .iter()
                .filter(|r| {
                    self.matches(&scope, r, usage)
                        && r.kind == kind
                        && r.effective.overlaps(effective)
                })
                .collect();
            records.sort_by(|a, b| a.effective.from.cmp(&b.effective.from));
            let mut cursor = effective.from.clone();
            let mut covered = false;
            for record in records {
                let from = record.effective.from.clone().max(effective.from.clone());
                let through = record
                    .effective
                    .through
                    .clone()
                    .min(effective.through.clone());
                if from != cursor {
                    return Err(self.missing(&scope, &cursor, kind));
                }
                components.push(DatedFeeComponent {
                    kind,
                    effective_from: from.to_string(),
                    effective_through: through.to_string(),
                    included_in_commission: commission.included_components.contains(&kind),
                    fact: record.fact.clone(),
                });
                if through == effective.through {
                    covered = true;
                    break;
                }
                cursor = through.next_day()?;
            }
            if !covered {
                return Err(self.missing(&scope, &cursor, kind));
            }
        }
        let synthetic_model = match &scope.origin {
            RuleOrigin::Official(_) => None,
            RuleOrigin::Synthetic(name) => Some(name.clone()),
        };
        ScopedFeeConfig::new(
            scope,
            FeeConfig {
                commission_rate: commission.commission_rate,
                minimum_commission: commission.minimum_commission,
                currency: commission.currency,
                settlement_scale: commission.settlement_scale,
                rounding: commission.rounding,
                components,
                synthetic_model,
            },
            usage,
        )
    }
}
/// Verified facts may be queried individually. compose refuses if *any* required
/// component lacks full date coverage; inclusion in commission does not waive
/// this check. This catalog intentionally does not assert all ETF/BSE fees.
pub fn verified_fee_catalog() -> QfResult<FeeCatalog> {
    let mut records = Vec::new();
    for exchange in [Exchange::Shanghai, Exchange::Shenzhen, Exchange::Beijing] {
        let products = match exchange {
            Exchange::Shanghai => vec![
                Product::MainBoardStock,
                Product::StarStock,
                Product::EquityEtf,
                Product::BondEtf,
                Product::MoneyEtf,
                Product::GoldEtf,
                Product::CommodityEtf,
                Product::CrossBorderEtf,
            ],
            Exchange::Shenzhen => vec![
                Product::MainBoardStock,
                Product::ChiNextStock,
                Product::EquityEtf,
                Product::BondEtf,
                Product::MoneyEtf,
                Product::GoldEtf,
                Product::CommodityEtf,
                Product::CrossBorderEtf,
            ],
            Exchange::Beijing => vec![Product::BeijingStock],
            Exchange::Unknown => unreachable!(),
        };
        for product in products {
            for side in [Side::Buy, Side::Sell] {
                for investor in [
                    InvestorKind::ResidentIndividual,
                    InvestorKind::ResidentEnterprise,
                ] {
                    let mut add = |kind,
                                   from: &str,
                                   through: &str,
                                   rate: Option<&str>,
                                   source: &str|
                     -> QfResult<()> {
                        records.push(FeeRuleRecord {
                            exchange,
                            product,
                            side,
                            investor,
                            kind,
                            effective: range(from, through)?,
                            origin: RuleOrigin::Official(source.into()),
                            fact: match rate {
                                Some(v) => FeeFact::Applicable {
                                    rate: v.parse()?,
                                    basis: source.into(),
                                },
                                None => FeeFact::Inapplicable {
                                    basis: source.into(),
                                },
                            },
                        });
                        Ok(())
                    };
                    if product.is_etf() || side == Side::Buy {
                        add(
                            FeeComponentKind::StampDuty,
                            "2022-07-01",
                            VERIFIED_THROUGH,
                            None,
                            "stamp-law-2022:article-3",
                        )?;
                    } else {
                        add(
                            FeeComponentKind::StampDuty,
                            "2022-07-01",
                            "2023-08-27",
                            Some("0.001"),
                            "stamp-law-2022:tax-table",
                        )?;
                        add(
                            FeeComponentKind::StampDuty,
                            "2023-08-28",
                            VERIFIED_THROUGH,
                            Some("0.0005"),
                            "stamp-cut-2023-39",
                        )?;
                    }
                    if product.is_stock() {
                        add(
                            FeeComponentKind::TransferFee,
                            "2022-04-28",
                            "2022-04-28",
                            Some(if exchange == Exchange::Beijing {
                                "0.000025"
                            } else {
                                "0.00002"
                            }),
                            "chinaclear-transfer-2022:previous-observation",
                        )?;
                        add(
                            FeeComponentKind::TransferFee,
                            "2022-04-29",
                            VERIFIED_THROUGH,
                            Some("0.00001"),
                            "chinaclear-transfer-2022",
                        )?;
                        add(
                            FeeComponentKind::HandlingFee,
                            "2023-08-18",
                            "2023-08-27",
                            Some(if exchange == Exchange::Beijing {
                                "0.00025"
                            } else {
                                "0.0000487"
                            }),
                            "csrc-handling-2023:previous-observation",
                        )?;
                        add(
                            FeeComponentKind::HandlingFee,
                            "2023-08-28",
                            VERIFIED_THROUGH,
                            Some(if exchange == Exchange::Beijing {
                                "0.000125"
                            } else {
                                "0.0000341"
                            }),
                            "csrc-handling-2023",
                        )?;
                    }
                    if exchange == Exchange::Shanghai && product.is_stock() {
                        // An observation date is not backdated to the month shown
                        // on a current tariff page. Historical gaps stay explicit.
                        add(
                            FeeComponentKind::RegulatoryFee,
                            VERIFIED_THROUGH,
                            VERIFIED_THROUGH,
                            Some("0.00002"),
                            "sse-taxfee:observed-2026-10-10",
                        )?;
                    }
                    if exchange == Exchange::Shanghai && product.is_etf() {
                        add(
                            FeeComponentKind::HandlingFee,
                            VERIFIED_THROUGH,
                            VERIFIED_THROUGH,
                            Some(if matches!(product, Product::BondEtf | Product::MoneyEtf) {
                                "0"
                            } else {
                                "0.00004"
                            }),
                            "sse-handling:observed-2026-10-10",
                        )?;
                    }
                }
            }
        }
    }
    FeeCatalog::new(records)
}

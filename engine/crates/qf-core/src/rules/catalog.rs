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

/// Verified auction profiles, including SZSE's 2023 edition activated on the
/// first registered main-board IPO listing date, rather than publication day.
/// Pending Beijing risk/after-hours activation is not
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
            let rule = TradingRule {
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
            };
            if exchange == Exchange::Shenzhen {
                // SZSE 2023 clauses 2.3.2, 3.1.5, 3.3.5-19, 4.5 and 10.9;
                // CSRC's first-main-board listing announcement: 2023-04-10.
                // The 2026 notice explicitly replaces this edition on July 6.
                let mut previous = rule.clone();
                previous.effective = range("2023-04-10", "2026-07-05")?;
                previous.origin = RuleOrigin::Official("szse-trading-2023".into());
                if product == Product::MainBoardStock {
                    previous.risk_limit_rate = "0.05".parse()?;
                }
                rules.push(previous);
            }
            rules.push(rule);
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
/// this check. The source's legal commencement and inspected scope determine
/// coverage, rather than the date on which a current web table was read.
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
                            if exchange == Exchange::Beijing {
                                "2022-04-28"
                            } else {
                                "2015-08-01"
                            },
                            "2022-04-28",
                            Some(if exchange == Exchange::Beijing {
                                "0.000025"
                            } else {
                                "0.00002"
                            }),
                            if exchange == Exchange::Beijing {
                                "chinaclear-transfer-2022:previous-observation"
                            } else {
                                "chinaclear-transfer-2015"
                            },
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
                            if exchange == Exchange::Beijing {
                                "2023-08-18"
                            } else {
                                "2015-08-01"
                            },
                            "2023-08-27",
                            Some(if exchange == Exchange::Beijing {
                                "0.00025"
                            } else {
                                "0.0000487"
                            }),
                            match exchange {
                                Exchange::Shanghai => "sse-handling-2015-67",
                                Exchange::Shenzhen => "szse-handling-2015",
                                _ => "csrc-handling-2023:previous-observation",
                            },
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
                    if exchange != Exchange::Beijing && product.is_stock() {
                        // The 2018 renewal explicitly supersedes 2016-14 and
                        // sets commencement for SH/SZ business regulatory fees.
                        // The 2018-2020 exemption concerns institution fees only.
                        add(
                            FeeComponentKind::RegulatoryFee,
                            "2018-01-01",
                            VERIFIED_THROUGH,
                            Some("0.00002"),
                            "ndrc-regulatory-2018-917",
                        )?;
                    }
                    if product.is_etf() {
                        // 财税[2015]20 expressly exempts investment funds;
                        // 2018-917 renews this closed SH/SZ charging program.
                        add(
                            FeeComponentKind::RegulatoryFee,
                            "2018-01-01",
                            VERIFIED_THROUGH,
                            None,
                            "mof-regulatory-2015-20:fund-exemption",
                        )?;
                        // The complete ChinaClear SH/SZ tariff's investor
                        // transfer-fee scopes cover stocks and specified
                        // exercises / ETF primary baskets, not secondary ETF
                        // units. 2015's notice forbids brokers inventing a
                        // transfer fee. This is a scoped applicability fact,
                        // not zero substituted for a missing tariff.
                        add(
                            FeeComponentKind::TransferFee,
                            if exchange == Exchange::Shanghai {
                                "2023-11-24"
                            } else {
                                "2025-05-01"
                            },
                            VERIFIED_THROUGH,
                            None,
                            if exchange == Exchange::Shanghai {
                                "chinaclear-tariffs-2023-2025:secondary-etf-outside-transfer-scope"
                            } else {
                                "chinaclear-tariff-2025:secondary-etf-outside-transfer-scope"
                            },
                        )?;
                        let exempt = matches!(product, Product::BondEtf | Product::MoneyEtf);
                        add(
                            FeeComponentKind::HandlingFee,
                            match (exchange, exempt) {
                                (Exchange::Shenzhen, true) => "2016-05-09",
                                _ => "2015-08-01",
                            },
                            "2021-07-18",
                            Some(if exempt {
                                "0"
                            } else if exchange == Exchange::Shanghai {
                                "0.000045"
                            } else {
                                "0.0000487"
                            }),
                            match (exchange, exempt) {
                                (Exchange::Shanghai, _) => "sse-handling-2015-67",
                                (Exchange::Shenzhen, true) => "szse-fund-2016-139:exemption",
                                _ => "szse-handling-2015",
                            },
                        )?;
                        add(
                            FeeComponentKind::HandlingFee,
                            "2021-07-19",
                            VERIFIED_THROUGH,
                            Some(if exempt { "0" } else { "0.00004" }),
                            match (exchange, exempt) {
                                (Exchange::Shanghai, _) => {
                                    "sse-fund-2021-49:continued-by-2021-95-2023-137"
                                }
                                (Exchange::Shenzhen, true) => {
                                    "szse-fund-2016-139:continued-exemption"
                                }
                                _ => "szse-fund-2021-655",
                            },
                        )?;
                    }
                    if exchange == Exchange::Beijing {
                        // The statutory regulatory-fee program expressly
                        // names SH/SZ and forbids expanding its scope. The
                        // complete Beijing tariff corroborates applicability
                        // for the inspected interval, not a guessed zero rate.
                        add(
                            FeeComponentKind::RegulatoryFee,
                            "2025-05-01",
                            VERIFIED_THROUGH,
                            None,
                            "ndrc-regulatory-2018-917:beijing-outside-scope",
                        )?;
                    }
                }
            }
        }
    }
    FeeCatalog::new(records)
}

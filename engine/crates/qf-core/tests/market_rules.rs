//! Published-rule assertions use a verified catalog and synthetic DAILY facts.
//! Named synthetic rule books are separate: they prove mechanics, not coverage.
use qf_core::orders::Side;
use qf_core::rules::CostOverrides;
use qf_core::rules::catalog::{verified_fee_catalog, verified_market_rules};
use qf_core::rules::date::{EffectiveRange, RuleDate};
use qf_core::rules::fees::{CommissionConfig, FeeScope, InvestorKind, OrderFeeAccumulator};
use qf_core::rules::market::*;
use qf_core::types::{
    ExactDecimal as D, Price, Quantity as Q, QuantityStep, SecurityKey, SessionKey,
};
use qf_core::{ErrorCode, QfResult};

fn d(v: &str) -> D {
    v.parse().unwrap()
}
fn p(v: &str) -> Price {
    v.parse().unwrap()
}
fn q(v: i64) -> Q {
    Q::new(v).unwrap()
}
fn date(v: &str) -> RuleDate {
    v.parse().unwrap()
}
fn instrument(exchange: Exchange, product: Product) -> Instrument {
    Instrument {
        security: SecurityKey::new("daily-fact-fixture").unwrap(),
        exchange,
        product,
    }
}
fn facts(inst: &Instrument) -> TradeDayFacts {
    let (rate, lower, upper) = match inst.product {
        Product::StarStock | Product::ChiNextStock => ("0.20", "8", "12"),
        Product::BeijingStock => ("0.30", "7", "13"),
        _ => ("0.10", "9", "11"),
    };
    TradeDayFacts {
        security: inst.security.clone(),
        date: date("2026-07-07"),
        session: SessionKey::new("fixture-session-2026-07-07").unwrap(),
        listing: Some(ListingWindow {
            first_trading_date: date("2020-01-06"),
            last_trading_date: None,
            verified_through: date("2026-10-10"),
            kind: ListingKind::Ipo,
            session_ordinal: 1_000,
        }),
        status: TradingStatus::Trading,
        risk: RiskState::Normal,
        price_limit: DailyPriceLimit::Limited {
            rate: d(rate),
            lower: p(lower),
            upper: p(upper),
        },
        fund_price_band: if inst.product.is_etf() {
            Some(FundPriceBand::TenPercent)
        } else {
            None
        },
        underlying_same_session: if inst.product == Product::CrossBorderEtf {
            Some(true)
        } else {
            None
        },
        delisting_session_ordinal: None,
    }
}
fn code<T: std::fmt::Debug>(result: QfResult<T>, expected: ErrorCode) {
    assert_eq!(result.unwrap_err().code, expected);
}

#[test]
fn official_markets_and_etf_subtypes_do_not_share_units_or_sellability() {
    let book = verified_market_rules().unwrap();
    for (exchange, product, min, step, tick, turnover) in [
        (
            Exchange::Shanghai,
            Product::MainBoardStock,
            100,
            100,
            "0.01",
            SellAvailability::NextSession,
        ),
        (
            Exchange::Shenzhen,
            Product::MainBoardStock,
            100,
            100,
            "0.01",
            SellAvailability::NextSession,
        ),
        (
            Exchange::Shanghai,
            Product::StarStock,
            200,
            1,
            "0.01",
            SellAvailability::NextSession,
        ),
        (
            Exchange::Shenzhen,
            Product::ChiNextStock,
            100,
            100,
            "0.01",
            SellAvailability::NextSession,
        ),
        (
            Exchange::Beijing,
            Product::BeijingStock,
            100,
            1,
            "0.01",
            SellAvailability::NextSession,
        ),
        (
            Exchange::Shanghai,
            Product::EquityEtf,
            100,
            100,
            "0.001",
            SellAvailability::NextSession,
        ),
        (
            Exchange::Shenzhen,
            Product::BondEtf,
            100,
            100,
            "0.001",
            SellAvailability::SameSession,
        ),
        (
            Exchange::Shanghai,
            Product::MoneyEtf,
            100,
            100,
            "0.001",
            SellAvailability::SameSession,
        ),
        (
            Exchange::Shenzhen,
            Product::GoldEtf,
            100,
            100,
            "0.001",
            SellAvailability::SameSession,
        ),
        (
            Exchange::Shanghai,
            Product::CommodityEtf,
            100,
            100,
            "0.001",
            SellAvailability::SameSession,
        ),
        (
            Exchange::Shenzhen,
            Product::CrossBorderEtf,
            100,
            100,
            "0.001",
            SellAvailability::SameSession,
        ),
    ] {
        let inst = instrument(exchange, product);
        let day = facts(&inst);
        let rule = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
        assert_eq!(
            (
                rule.rule().minimum_buy,
                rule.rule().buy_step.get(),
                rule.rule().price_tick
            ),
            (q(min), step, p(tick))
        );
        assert_eq!(rule.sell_availability().unwrap(), turnover);
        assert_eq!(rule.session(), &day.session);
        let pos = PositionAvailability {
            settled: q(100),
            bought_today: q(100),
            frozen: q(50),
        };
        assert_eq!(
            rule.sellable(&pos).unwrap(),
            q(if turnover == SellAvailability::SameSession {
                150
            } else {
                50
            })
        );
    }
    let inst = instrument(Exchange::Shanghai, Product::CrossBorderEtf);
    let mut day = facts(&inst);
    day.underlying_same_session = Some(false);
    assert_eq!(
        book.resolve(&inst, &day, &RuleUse::Market)
            .unwrap()
            .sell_availability()
            .unwrap(),
        SellAvailability::NextSession
    );
    day.underlying_same_session = None;
    let error = book.resolve(&inst, &day, &RuleUse::Market).unwrap_err();
    assert_eq!(error.code, ErrorCode::RuleUnavailable);
    assert_eq!(error.scope["field"], "underlying_same_session");
}

#[test]
fn quantity_orders_are_strict_targets_round_down_and_odd_lots_are_not_split() {
    let book = verified_market_rules().unwrap();
    for (exchange, product, legal_buy, bad_buy, maximum) in [
        (
            Exchange::Shanghai,
            Product::MainBoardStock,
            100,
            101,
            1_000_000,
        ),
        (Exchange::Shanghai, Product::StarStock, 201, 199, 100_000),
        (Exchange::Shenzhen, Product::ChiNextStock, 100, 101, 300_000),
        (Exchange::Beijing, Product::BeijingStock, 101, 99, 1_000_000),
    ] {
        let inst = instrument(exchange, product);
        let day = facts(&inst);
        let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
        r.validate_quantity(Side::Buy, q(legal_buy), Q::ZERO, false)
            .unwrap();
        code(
            r.validate_quantity(Side::Buy, q(bad_buy), Q::ZERO, false),
            ErrorCode::InvalidOrder,
        );
        code(
            r.validate_quantity(Side::Buy, q(maximum + 1), Q::ZERO, false),
            ErrorCode::InvalidOrder,
        );
        code(
            r.validate_quantity(Side::Sell, q(151), q(150), false),
            ErrorCode::InsufficientSellable,
        );
    }
    let inst = instrument(Exchange::Shanghai, Product::MainBoardStock);
    let day = facts(&inst);
    let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    assert_eq!(r.quantize_buy(q(199)), q(100));
    assert_eq!(r.quantize_buy(q(99)), Q::ZERO);
    for qty in [50, 100, 150, 250] {
        r.validate_quantity(Side::Sell, q(qty), q(250), false)
            .unwrap();
    }
    for qty in [25, 125, 225] {
        code(
            r.validate_quantity(Side::Sell, q(qty), q(250), false),
            ErrorCode::InvalidOrder,
        );
    }
    let inst = instrument(Exchange::Shanghai, Product::StarStock);
    let day = facts(&inst);
    let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    assert_eq!(r.quantize_buy(q(199)), Q::ZERO);
    assert_eq!(r.quantize_buy(q(201)), q(201));
    r.validate_quantity(Side::Sell, q(150), q(150), false)
        .unwrap();
    code(
        r.validate_quantity(Side::Sell, q(100), q(150), false),
        ErrorCode::InvalidOrder,
    );
    code(
        r.validate_quantity(Side::Buy, q(50_001), Q::ZERO, true),
        ErrorCode::InvalidOrder,
    );
}

#[test]
fn identity_unknown_halt_and_listing_edges_are_locatable_and_fail_closed() {
    let book = verified_market_rules().unwrap();
    for (exchange, product, expected) in [
        (Exchange::Shanghai, Product::Index, ErrorCode::InvalidOrder),
        (
            Exchange::Unknown,
            Product::MainBoardStock,
            ErrorCode::RuleUnavailable,
        ),
        (
            Exchange::Beijing,
            Product::EquityEtf,
            ErrorCode::RuleUnavailable,
        ),
        (
            Exchange::Shenzhen,
            Product::StarStock,
            ErrorCode::RuleUnavailable,
        ),
    ] {
        let inst = instrument(exchange, product);
        code(
            book.resolve(&inst, &facts(&inst), &RuleUse::Market),
            expected,
        );
    }
    let inst = instrument(Exchange::Shanghai, Product::MainBoardStock);
    let baseline = facts(&inst);
    let mut day = baseline.clone();
    day.status = TradingStatus::Halted;
    let halted = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    code(halted.validate_execution(p("10")), ErrorCode::InvalidOrder);
    halted
        .validate_limit_submission(Side::Buy, p("10"), AuctionPhase::TemporaryHaltCall, None)
        .unwrap();
    for field in [
        "status",
        "risk",
        "listing",
        "price_limit",
        "identity",
        "unverified",
    ] {
        let mut day = baseline.clone();
        match field {
            "status" => day.status = TradingStatus::Unknown,
            "risk" => day.risk = RiskState::Unknown,
            "listing" => day.listing = None,
            "price_limit" => day.price_limit = DailyPriceLimit::Unknown,
            "identity" => day.security = SecurityKey::new("other").unwrap(),
            _ => day.listing.as_mut().unwrap().verified_through = date("2026-07-06"),
        }
        let err = book.resolve(&inst, &day, &RuleUse::Market).unwrap_err();
        assert_eq!(err.code, ErrorCode::RuleUnavailable, "{field}");
        assert_eq!(err.scope["security"], inst.security.as_str());
        assert_eq!(err.scope["date"], "2026-07-07");
    }
    let mut day = baseline.clone();
    day.listing.as_mut().unwrap().first_trading_date = date("2026-07-08");
    code(
        book.resolve(&inst, &day, &RuleUse::Market),
        ErrorCode::InvalidOrder,
    );
    day = baseline.clone();
    day.listing.as_mut().unwrap().last_trading_date = Some(date("2026-07-06"));
    code(
        book.resolve(&inst, &day, &RuleUse::Market),
        ErrorCode::InvalidOrder,
    );
    day = baseline;
    day.listing.as_mut().unwrap().last_trading_date = Some(day.date.clone());
    book.resolve(&inst, &day, &RuleUse::Market).unwrap();
}

#[test]
fn official_date_coverage_and_named_fixture_changes_are_separate() {
    let book = verified_market_rules().unwrap();
    let inst = instrument(Exchange::Shanghai, Product::MainBoardStock);
    let mut day = facts(&inst);
    for value in ["2026-07-05", "2026-10-11", "2023-04-10"] {
        day.date = date(value);
        code(
            book.resolve(&inst, &day, &RuleUse::Market),
            ErrorCode::RuleUnavailable,
        );
    }
    day.date = date("2026-07-06");
    book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    let mut old = book
        .resolve(&inst, &day, &RuleUse::Market)
        .unwrap()
        .rule()
        .clone();
    old.origin = RuleOrigin::Synthetic("dated-mechanics".into());
    old.effective = EffectiveRange {
        from: date("2026-07-01"),
        through: date("2026-07-05"),
    };
    old.risk_limit_rate = d("0.05");
    let mut new = old.clone();
    new.effective.from = date("2026-07-06");
    new.effective.through = date("2026-07-10");
    new.risk_limit_rate = d("0.10");
    let synthetic = RuleBook::new(vec![old.clone(), new]).unwrap();
    for (value, rate) in [("2026-07-05", "0.05"), ("2026-07-06", "0.10")] {
        day.date = date(value);
        day.risk = RiskState::RiskWarning;
        day.price_limit = DailyPriceLimit::Limited {
            rate: d(rate),
            lower: p("7"),
            upper: p("13"),
        };
        synthetic
            .resolve(&inst, &day, &RuleUse::Synthetic("dated-mechanics".into()))
            .unwrap();
        code(
            synthetic.resolve(&inst, &day, &RuleUse::Market),
            ErrorCode::RuleUnavailable,
        );
        code(
            synthetic.resolve(&inst, &day, &RuleUse::Synthetic("other".into())),
            ErrorCode::RuleUnavailable,
        );
    }
    let mut overlap = old.clone();
    overlap.origin = RuleOrigin::Synthetic("dated-mechanics".into());
    code(
        RuleBook::new(vec![old, overlap]),
        ErrorCode::RuleUnavailable,
    );
}

#[test]
fn shenzhen_2023_activation_and_2026_risk_change_use_official_editions() {
    let book = verified_market_rules().unwrap();
    let inst = instrument(Exchange::Shenzhen, Product::MainBoardStock);
    let mut day = facts(&inst);
    for value in ["2023-02-17", "2023-04-07"] {
        day.date = date(value);
        code(
            book.resolve(&inst, &day, &RuleUse::Market),
            ErrorCode::RuleUnavailable,
        );
    }
    day.date = date("2023-04-10");
    day.listing.as_mut().unwrap().first_trading_date = day.date.clone();
    day.listing.as_mut().unwrap().session_ordinal = 1;
    day.price_limit = DailyPriceLimit::NoLimit {
        reason: NoLimitReason::Ipo,
        session_ordinal: 1,
        basis: "explicit daily IPO fixture".into(),
    };
    let first = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    assert_eq!(
        first.rule().origin,
        RuleOrigin::Official("szse-trading-2023".into())
    );
    assert_eq!(first.rule().ipo_no_limit_sessions, 5);
    // July 3 and 6 are Friday / Monday around the legal July 6 transition.
    // These daily facts remain fixtures; no market calendar is generated here.
    day = facts(&inst);
    day.risk = RiskState::RiskWarning;
    for (value, rate, lower, upper, source) in [
        ("2026-07-03", "0.05", "9.5", "10.5", "szse-trading-2023"),
        ("2026-07-06", "0.10", "9", "11", "szse-trading-2026"),
    ] {
        day.date = date(value);
        day.price_limit = DailyPriceLimit::Limited {
            rate: d(rate),
            lower: p(lower),
            upper: p(upper),
        };
        let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
        assert_eq!(r.rule().origin, RuleOrigin::Official(source.into()));
        assert_eq!(r.rule().risk_limit_rate, d(rate));
        assert_eq!(
            r.rule().price_band(p("10"), d(rate)).unwrap(),
            (p(lower), p(upper))
        );
        day.price_limit = DailyPriceLimit::Limited {
            rate: d(if rate == "0.05" { "0.10" } else { "0.05" }),
            lower: p(lower),
            upper: p(upper),
        };
        code(
            book.resolve(&inst, &day, &RuleUse::Market),
            ErrorCode::RuleUnavailable,
        );
    }
    for product in [
        Product::ChiNextStock,
        Product::EquityEtf,
        Product::BondEtf,
        Product::MoneyEtf,
        Product::GoldEtf,
        Product::CommodityEtf,
        Product::CrossBorderEtf,
    ] {
        let inst = instrument(Exchange::Shenzhen, product);
        let mut day = facts(&inst);
        day.date = date("2023-04-10");
        let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
        assert_eq!(
            r.rule().origin,
            RuleOrigin::Official("szse-trading-2023".into())
        );
        assert_eq!(r.rule().session_template.closing_call(), Some((897, 900)));
    }
}

#[test]
fn official_market_and_fee_chain_accepts_a_trading_date_without_synthetic_rules() {
    let book = verified_market_rules().unwrap();
    let catalog = verified_fee_catalog().unwrap();
    for (exchange, stocks) in [
        (
            Exchange::Shanghai,
            vec![Product::MainBoardStock, Product::StarStock],
        ),
        (
            Exchange::Shenzhen,
            vec![Product::MainBoardStock, Product::ChiNextStock],
        ),
        (Exchange::Beijing, vec![Product::BeijingStock]),
    ] {
        let products: Vec<_> = if exchange == Exchange::Beijing {
            stocks
        } else {
            stocks
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
        };
        for product in products {
            let inst = instrument(exchange, product);
            let day = facts(&inst); // Tuesday 2026-07-07; all daily facts explicitly supplied.
            let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
            r.validate_quantity(Side::Sell, q(200), q(200), false)
                .unwrap();
            r.validate_execution(p("10")).unwrap();
            let scope = FeeScope {
                instrument: inst.clone(),
                side: Side::Sell,
                investor: InvestorKind::ResidentIndividual,
                origin: r.rule().origin.clone(),
            };
            let commission = CommissionConfig {
                commission_rate: d("0.0003"),
                minimum_commission: d("5"),
                currency: qf_core::types::market::Currency::CNY,
                settlement_scale: 2,
                rounding: qf_core::types::RoundingPolicy::HalfEven,
                included_components: vec![],
                basis: "explicit selected-account agreement (test input)".into(),
            };
            let fees = catalog
                .compose(
                    scope.clone(),
                    &EffectiveRange {
                        from: date("2026-07-06"),
                        through: date("2026-10-10"),
                    },
                    &commission,
                    &CostOverrides::default(),
                    &RuleUse::Market,
                )
                .unwrap();
            assert!(fees.config().synthetic_model.is_none());
            let mut a = OrderFeeAccumulator::new("market-and-fee-chain", fees).unwrap();
            // Amount = 200 * 10; commission minimum 5; mandatory rates are official.
            let expected = if product == Product::BeijingStock {
                "6.27"
            } else if product.is_stock() {
                "6.13"
            } else if matches!(product, Product::BondEtf | Product::MoneyEtf) {
                "5"
            } else {
                "5.08"
            };
            assert_eq!(
                a.apply_fill(&scope, &day.date, d("2000")).unwrap().total,
                d(expected)
            );
        }
    }
}

#[test]
fn tick_grid_price_bands_and_etf_twenty_percent_require_dated_facts() {
    let book = verified_market_rules().unwrap();
    let inst = instrument(Exchange::Shanghai, Product::MainBoardStock);
    let mut day = facts(&inst);
    day.price_limit = DailyPriceLimit::Limited {
        rate: d("0.10"),
        lower: p("9"),
        upper: p("11"),
    };
    let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    for value in ["9", "11"] {
        r.validate_execution(p(value)).unwrap();
    }
    for value in ["8.99", "11.01", "10.001"] {
        code(r.validate_execution(p(value)), ErrorCode::InvalidOrder);
    }
    assert_eq!(
        r.rule().price_band(p("10.05"), d("0.10")).unwrap(),
        (p("9.05"), p("11.06"))
    );
    assert_eq!(
        r.rule().price_band(p("0.01"), d("0.10")).unwrap(),
        (p("0.01"), p("0.02"))
    );
    code(
        r.rule().price_band(p("10.005"), d("0.10")),
        ErrorCode::RuleUnavailable,
    );
    let inst = instrument(Exchange::Shanghai, Product::EquityEtf);
    let mut day = facts(&inst);
    day.fund_price_band = Some(FundPriceBand::TwentyPercent);
    code(
        book.resolve(&inst, &day, &RuleUse::Market),
        ErrorCode::RuleUnavailable,
    );
    day.price_limit = DailyPriceLimit::Limited {
        rate: d("0.20"),
        lower: p("8"),
        upper: p("12"),
    };
    book.resolve(&inst, &day, &RuleUse::Market)
        .unwrap()
        .validate_execution(p("10.001"))
        .unwrap();
    let bond = instrument(Exchange::Shanghai, Product::BondEtf);
    day.security = bond.security.clone();
    code(
        book.resolve(&bond, &day, &RuleUse::Market),
        ErrorCode::RuleUnavailable,
    );
    let inst = instrument(Exchange::Beijing, Product::BeijingStock);
    let day = facts(&inst);
    code(
        book.resolve(&inst, &day, &RuleUse::Market)
            .unwrap()
            .rule()
            .price_band(p("10"), d("0.30")),
        ErrorCode::RuleUnavailable,
    );
}

#[test]
fn ipo_exemptions_count_actual_sessions_and_call_ranges_differ_by_market() {
    let book = verified_market_rules().unwrap();
    for (exchange, product, last_exempt) in [
        (Exchange::Shanghai, Product::MainBoardStock, 5),
        (Exchange::Shenzhen, Product::ChiNextStock, 5),
        (Exchange::Beijing, Product::BeijingStock, 1),
    ] {
        let inst = instrument(exchange, product);
        let mut day = facts(&inst);
        day.listing.as_mut().unwrap().first_trading_date = date("2026-07-06");
        day.listing.as_mut().unwrap().session_ordinal = last_exempt;
        if last_exempt == 1 {
            day.date = date("2026-07-06");
        } else {
            day.date = date("2026-07-10");
        }
        day.price_limit = DailyPriceLimit::NoLimit {
            reason: NoLimitReason::Ipo,
            session_ordinal: last_exempt,
            basis: "accepted daily IPO fixture".into(),
        };
        book.resolve(&inst, &day, &RuleUse::Market).unwrap();
        day.date = date(if last_exempt == 1 {
            "2026-07-07"
        } else {
            "2026-07-13"
        });
        day.listing.as_mut().unwrap().session_ordinal = last_exempt + 1;
        day.price_limit = DailyPriceLimit::NoLimit {
            reason: NoLimitReason::Ipo,
            session_ordinal: last_exempt + 1,
            basis: "fixture".into(),
        };
        code(
            book.resolve(&inst, &day, &RuleUse::Market),
            ErrorCode::RuleUnavailable,
        );
    }
    for (exchange, product) in [
        (Exchange::Shanghai, Product::MainBoardStock),
        (Exchange::Shenzhen, Product::MainBoardStock),
        (Exchange::Shanghai, Product::StarStock),
        (Exchange::Beijing, Product::BeijingStock),
    ] {
        let inst = instrument(exchange, product);
        let mut day = facts(&inst);
        day.date = date("2026-07-06");
        day.listing.as_mut().unwrap().first_trading_date = day.date.clone();
        day.listing.as_mut().unwrap().session_ordinal = 1;
        day.price_limit = DailyPriceLimit::NoLimit {
            reason: NoLimitReason::Ipo,
            session_ordinal: 1,
            basis: "accepted daily IPO fixture".into(),
        };
        let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
        if product == Product::MainBoardStock {
            r.validate_limit_submission(
                Side::Buy,
                p("90"),
                AuctionPhase::OpeningCall,
                Some(p("10")),
            )
            .unwrap();
            code(
                r.validate_limit_submission(
                    Side::Buy,
                    p("90.01"),
                    AuctionPhase::OpeningCall,
                    Some(p("10")),
                ),
                ErrorCode::InvalidOrder,
            );
            let result = r.validate_limit_submission(
                Side::Buy,
                p("4.99"),
                AuctionPhase::OpeningCall,
                Some(p("10")),
            );
            if exchange == Exchange::Shanghai {
                code(result, ErrorCode::InvalidOrder);
            } else {
                result.unwrap();
            }
            code(
                r.validate_limit_submission(
                    Side::Sell,
                    p("8.99"),
                    AuctionPhase::ClosingCall,
                    Some(p("10")),
                ),
                ErrorCode::InvalidOrder,
            );
        } else {
            r.validate_limit_submission(Side::Buy, p("1000"), AuctionPhase::OpeningCall, None)
                .unwrap();
        }
        code(
            r.validate_market_phase(AuctionPhase::OpeningCall),
            ErrorCode::InvalidOrder,
        );
        if exchange == Exchange::Shanghai {
            r.validate_market_phase(AuctionPhase::Continuous).unwrap();
        } else {
            code(
                r.validate_market_phase(AuctionPhase::Continuous),
                ErrorCode::InvalidOrder,
            );
        }
    }
}

#[test]
fn continuous_cages_include_tick_fallback_and_do_not_guess_beijing_rounding() {
    let book = verified_market_rules().unwrap();
    for (exchange, product, highest, next) in [
        (Exchange::Shanghai, Product::MainBoardStock, "1.10", "1.11"),
        (Exchange::Shenzhen, Product::ChiNextStock, "1.10", "1.11"),
        (Exchange::Shanghai, Product::StarStock, "1.02", "1.03"),
        (Exchange::Beijing, Product::BeijingStock, "1.10", "1.11"),
    ] {
        let inst = instrument(exchange, product);
        let day = facts(&inst);
        let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
        r.validate_continuous_limit(Side::Buy, p(highest), Some(p("1")))
            .unwrap();
        code(
            r.validate_continuous_limit(Side::Buy, p(next), Some(p("1"))),
            ErrorCode::InvalidOrder,
        );
        code(
            r.validate_continuous_limit(Side::Buy, p("1"), None),
            ErrorCode::RuleUnavailable,
        );
    }
    let inst = instrument(Exchange::Beijing, Product::BeijingStock);
    let day = facts(&inst);
    let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    r.validate_continuous_limit(Side::Buy, p("10.51"), Some(p("10.01")))
        .unwrap();
    code(
        r.validate_continuous_limit(Side::Buy, p("10.52"), Some(p("10.01"))),
        ErrorCode::RuleUnavailable,
    );
    code(
        r.validate_continuous_limit(Side::Buy, p("10.53"), Some(p("10.01"))),
        ErrorCode::InvalidOrder,
    );
}

#[test]
fn permissions_risk_aggregate_and_pending_beijing_risk_are_explicit() {
    let book = verified_market_rules().unwrap();
    let inst = instrument(Exchange::Shanghai, Product::MainBoardStock);
    let mut day = facts(&inst);
    day.risk = RiskState::RiskWarning;
    let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    let mut permissions = TradePermissions {
        buy: Some(true),
        sell: Some(true),
        product_buy: Some(true),
        risk_disclosure: Some(true),
        delisting_buy: None,
        risk_bought_or_open: Some(q(499_900)),
    };
    r.validate_permissions(Side::Buy, q(100), false, &permissions)
        .unwrap();
    code(
        r.validate_permissions(Side::Buy, q(200), false, &permissions),
        ErrorCode::InvalidOrder,
    );
    code(
        r.validate_permissions(Side::Sell, q(100), true, &permissions),
        ErrorCode::InvalidOrder,
    );
    permissions.product_buy = None;
    code(
        r.validate_permissions(Side::Buy, q(100), false, &permissions),
        ErrorCode::RuleUnavailable,
    );
    permissions.product_buy = Some(false);
    code(
        r.validate_permissions(Side::Buy, q(100), false, &permissions),
        ErrorCode::InvalidOrder,
    );
    // Buy-only suitability does not prevent an explicitly permitted sale.
    r.validate_permissions(Side::Sell, q(100), false, &permissions)
        .unwrap();
    let inst = instrument(Exchange::Beijing, Product::BeijingStock);
    let mut day = facts(&inst);
    day.risk = RiskState::RiskWarning;
    code(
        book.resolve(&inst, &day, &RuleUse::Market),
        ErrorCode::RuleUnavailable,
    );
    let inst = instrument(Exchange::Shanghai, Product::MainBoardStock);
    let mut day = facts(&inst);
    day.risk = RiskState::Delisting;
    day.delisting_session_ordinal = Some(1);
    day.price_limit = DailyPriceLimit::NoLimit {
        reason: NoLimitReason::DelistingFirstSession,
        session_ordinal: 1,
        basis: "daily delisting fixture".into(),
    };
    let r = book.resolve(&inst, &day, &RuleUse::Market).unwrap();
    permissions.product_buy = Some(true);
    code(
        r.validate_permissions(Side::Buy, q(100), false, &permissions),
        ErrorCode::RuleUnavailable,
    );
    permissions.delisting_buy = Some(false);
    code(
        r.validate_permissions(Side::Buy, q(100), false, &permissions),
        ErrorCode::InvalidOrder,
    );
}

#[test]
fn synthetic_t0_integer_rules_cannot_be_selected_as_official_market_rules() {
    let real = verified_market_rules().unwrap();
    let inst = instrument(Exchange::Shanghai, Product::MainBoardStock);
    let day = facts(&inst);
    let mut fixture = real
        .resolve(&inst, &day, &RuleUse::Market)
        .unwrap()
        .rule()
        .clone();
    fixture.origin = RuleOrigin::Synthetic("integer-t0".into());
    fixture.minimum_buy = q(1);
    fixture.minimum_sell = q(1);
    fixture.buy_step = QuantityStep::new(1).unwrap();
    fixture.sell_step = QuantityStep::new(1).unwrap();
    fixture.sell_availability = SellAvailability::SameSession;
    let book = RuleBook::new(vec![fixture]).unwrap();
    code(
        book.resolve(&inst, &day, &RuleUse::Market),
        ErrorCode::RuleUnavailable,
    );
    let r = book
        .resolve(&inst, &day, &RuleUse::Synthetic("integer-t0".into()))
        .unwrap();
    r.validate_quantity(Side::Buy, q(3), Q::ZERO, false)
        .unwrap();
    assert_eq!(
        r.sellable(&PositionAvailability {
            settled: Q::ZERO,
            bought_today: q(3),
            frozen: Q::ZERO
        })
        .unwrap(),
        q(3)
    );
    assert_eq!(
        r.rule().session_template.continuous_windows(),
        &[(570, 690), (780, 897)]
    );
    assert_eq!(r.rule().session_template.closing_call(), Some((897, 900)));
}

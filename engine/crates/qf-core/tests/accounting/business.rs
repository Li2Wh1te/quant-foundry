use super::support::*;
use qf_core::ErrorCode;

fn official_terms(
    row: &qf_core::clock::CalendarSession,
    product: qf_core::rules::market::Product,
) -> AccountTerms {
    use qf_core::rules::CostOverrides;
    use qf_core::rules::catalog::{verified_fee_catalog, verified_market_rules};
    use qf_core::rules::date::EffectiveRange;
    use qf_core::rules::fees::{CommissionConfig, FeeScope, InvestorKind};
    use qf_core::rules::market::{Exchange, Instrument, RuleOrigin, RuleUse};
    let instrument = Instrument {
        security: sec("A"),
        exchange: Exchange::Shanghai,
        product,
    };
    let mut day = facts(
        row,
        &instrument,
        qf_core::rules::market::TradingStatus::Trading,
    );
    day.price_limit = qf_core::rules::market::DailyPriceLimit::Limited {
        rate: d("0.1"),
        lower: p("9"),
        upper: p("11"),
    };
    let commission = CommissionConfig {
        commission_rate: d("0.0003"),
        minimum_commission: d("5"),
        currency: qf_core::types::market::Currency::CNY,
        settlement_scale: 2,
        rounding: qf_core::types::RoundingPolicy::HalfEven,
        included_components: vec![
            FeeComponentKind::TransferFee,
            FeeComponentKind::RegulatoryFee,
            FeeComponentKind::HandlingFee,
        ],
        basis: "explicit isolated saved-account inclusive commission agreement".into(),
    };
    let fees = verified_fee_catalog().unwrap();
    let interval = EffectiveRange {
        from: date("2026-07-07"),
        through: date("2026-07-09"),
    };
    let scope = |side| FeeScope {
        instrument: instrument.clone(),
        side,
        investor: InvestorKind::ResidentIndividual,
        origin: RuleOrigin::Official("verified-fee-catalog".into()),
    };
    let buy = fees
        .compose(
            scope(Side::Buy),
            &interval,
            &commission,
            &CostOverrides::default(),
            &RuleUse::Market,
        )
        .unwrap();
    let sell = fees
        .compose(
            scope(Side::Sell),
            &interval,
            &commission,
            &CostOverrides::default(),
            &RuleUse::Market,
        )
        .unwrap();
    // D02 official catalogs + explicitly supplied isolated current-state facts.
    // This does NOT assert production facts/cash-usability mappings are enabled.
    AccountTerms::resolve(
        instrument,
        &verified_market_rules().unwrap(),
        day,
        buy,
        sell,
        SaleProceedsRule {
            timing: SaleProceedsTiming::Immediate,
            basis: "isolated supplied sale-cash fact; not production evidence".into(),
            origin: RuleOrigin::Official("isolated-current-cash-fact".into()),
        },
        RuleUse::Market,
    )
    .unwrap()
}
#[test]
fn actual_d02_stock_and_bond_etf_rules_drive_account_units_sellability_and_inclusive_fees() {
    use qf_core::rules::market::{Product, RuleUse};
    let r: Vec<_> = ["2026-07-07", "2026-07-08", "2026-07-09"]
        .iter()
        .enumerate()
        .map(|(i, d)| row(i, d))
        .collect();
    for (product, same_day, profit) in [
        (Product::MainBoardStock, false, "89.45"),
        (Product::BondEtf, true, "90"),
    ] {
        let mut a = Account::new(
            d("10000"),
            r.clone(),
            RuleUse::Market,
            AccountLimits::default(),
        )
        .unwrap();
        a.install_terms(official_terms(&r[0], product)).unwrap();
        a.settle(&r[0].session.key).unwrap();
        code(
            a.reserve_order(request(&r[0], "bad-unit", Side::Buy, 1, "10", at(&r[0], 1))),
            ErrorCode::InvalidOrder,
        );
        a.reserve_order(request(&r[0], "buy", Side::Buy, 100, "10", at(&r[0], 1)))
            .unwrap();
        let buy = execute(
            &mut a,
            fill("buy", "buy-fill", Side::Buy, 100, "10", at(&r[0], 2)),
        );
        assert_eq!(buy.fee, d("5"));
        if !same_day {
            code(
                a.reserve_order(request(&r[0], "early", Side::Sell, 100, "11", at(&r[0], 3))),
                ErrorCode::InsufficientSellable,
            );
            a.settle(&r[1].session.key).unwrap();
            a.install_terms(official_terms(&r[1], product)).unwrap();
        }
        let row = if same_day { &r[0] } else { &r[1] };
        a.reserve_order(request(row, "sell", Side::Sell, 100, "11", at(row, 3)))
            .unwrap();
        let sold = execute(
            &mut a,
            fill("sell", "sell-fill", Side::Sell, 100, "11", at(row, 4)),
        );
        assert_eq!(sold.fee, d(if same_day { "5" } else { "5.55" }));
        assert_eq!(a.totals().realized_pnl, d(profit));
        assert_eq!(
            a.value(at(row, 4)).unwrap().cash,
            d("10000").checked_add(d(profit)).unwrap()
        );
        assert!(a.value(at(row, 4)).unwrap().positions.is_empty());
    }
}
use qf_core::accounting::*;
use qf_core::orders::{Side, TimeInForce};
use qf_core::rules::market::SellAvailability as S;
use qf_core::rules::{DatedFeeComponent, FeeComponentKind, FeeFact};
use qf_core::types::QuantityStep;

#[test]
fn contract_cash_conservation_uses_total_cost_and_exact_raw_equity() {
    let oracle = oracle("cash_conservation");
    let r = rows();
    let mut a = account(
        oracle["initial_cash"].as_str().unwrap(),
        &r,
        S::SameSession,
        "1",
    );
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    trade(&mut a, &r[0], "sell", Side::Sell, 40, "12", 3);
    let v = a.value(at(&r[0], 4)).unwrap();
    let e = &oracle["expected"];
    assert_eq!(v.cash, decimal(&e["cash"]));
    assert_eq!(v.total_value, Some(decimal(&e["equity"])));
    assert_eq!(v.cash, v.available_cash.checked_add(v.frozen_cash).unwrap());
    assert_eq!(
        v.positions[&sec("A")].quantity.get(),
        e["quantity"].as_i64().unwrap()
    );
    assert_eq!(
        v.positions[&sec("A")].cost_basis_total,
        decimal(&e["remaining_cost"])
    );
    assert_eq!(a.totals().realized_pnl, decimal(&e["realized_pnl"]));
    assert_eq!(
        v.cash
            .checked_add(v.receivables)
            .unwrap()
            .checked_add(v.positions[&sec("A")].market_value.unwrap())
            .unwrap(),
        v.total_value.unwrap()
    );
}
#[test]
fn repeating_average_is_display_only_and_final_sale_releases_one_yuan() {
    let e = oracle("repeating_average_cost")["expected"].clone();
    let r = rows();
    let mut a = account("2", &r, S::SameSession, "0.25");
    // Independent 3*0.25 + 0.25 = 1; no decimal 1/3 input or old engine.
    trade(&mut a, &r[0], "buy", Side::Buy, 3, "0.25", 1);
    let v = a.value(at(&r[0], 2)).unwrap();
    assert_eq!(v.positions[&sec("A")].cost_basis_total, d("1"));
    assert_eq!(
        v.positions[&sec("A")].average_cost,
        decimal(&e["display_average_cost"])
    );
    trade(&mut a, &r[0], "sell", Side::Sell, 3, "1", 3);
    let v = a.value(at(&r[0], 4)).unwrap();
    assert!(v.positions.is_empty());
    assert_eq!(v.cash, d("3.75"));
    // 3 - 0.25 - 1 = 1.75 proves the released cost was exactly 1.
    assert_eq!(a.totals().realized_pnl, d("1.75"));
}
#[test]
fn partial_sales_release_rounded_rational_then_all_remaining_cost() {
    let r = rows();
    let mut a = account("2", &r, S::SameSession, "0.25");
    trade(&mut a, &r[0], "buy", Side::Buy, 3, "0.25", 1);
    a.reserve_order(request(&r[0], "sell", Side::Sell, 3, "1", at(&r[0], 3)))
        .unwrap();
    execute(&mut a, fill("sell", "s1", Side::Sell, 1, "1", at(&r[0], 4)));
    assert_eq!(
        a.value(at(&r[0], 4)).unwrap().positions[&sec("A")].cost_basis_total,
        d("0.666666666667")
    );
    execute(&mut a, fill("sell", "s2", Side::Sell, 1, "1", at(&r[0], 5)));
    assert_eq!(
        a.value(at(&r[0], 5)).unwrap().positions[&sec("A")].cost_basis_total,
        d("0.333333333333")
    );
    execute(&mut a, fill("sell", "s3", Side::Sell, 1, "1", at(&r[0], 6)));
    assert_eq!(a.totals().realized_pnl, d("1.75"));
    assert!(a.value(at(&r[0], 6)).unwrap().positions.is_empty());
}
#[test]
fn fees_midpoints_are_assessed_with_half_even_and_settlement_policy() {
    let e = oracle("half_even_midpoints");
    let r = rows();
    for (rate, expected) in e["input"]
        .as_array()
        .unwrap()
        .iter()
        .zip(e["expected"].as_array().unwrap())
    {
        let mut a = account("10", &r, S::SameSession, "0");
        a.install_terms(terms(
            &r[0],
            "A",
            S::SameSession,
            rate.as_str().unwrap(),
            "0",
        ))
        .unwrap();
        let f = trade(&mut a, &r[0], "half-even", Side::Buy, 1, "1", 1);
        assert_eq!(f.fee, decimal(expected));
        assert_eq!(
            a.value(at(&r[0], 2)).unwrap().cash,
            d("9").checked_sub(decimal(expected)).unwrap()
        );
    }
}
#[test]
fn partial_fill_cancel_charges_one_minimum_and_release_is_idempotent() {
    let r = rows();
    let mut a = account("10000", &r, S::SameSession, "5");
    a.install_terms(terms(&r[0], "A", S::SameSession, "0.001", "5"))
        .unwrap();
    a.reserve_order(request(&r[0], "buy", Side::Buy, 100, "10", at(&r[0], 1)))
        .unwrap();
    let first = execute(&mut a, fill("buy", "b1", Side::Buy, 10, "10", at(&r[0], 2)));
    let second = execute(&mut a, fill("buy", "b2", Side::Buy, 30, "10", at(&r[0], 3)));
    assert_eq!((first.fee, second.fee), (d("5"), d("0")));
    assert_eq!(a.reservation("buy").unwrap().frozen_cash, d("600"));
    a.release("buy").unwrap();
    a.release("buy").unwrap();
    let v = a.value(at(&r[0], 3)).unwrap();
    assert_eq!(v.cash, d("9595"));
    assert_eq!(v.frozen_cash, d("0"));
    assert_eq!(v.positions[&sec("A")].cost_basis_total, d("405"));
    assert_eq!(a.totals().trading_fees, d("5"));
}
#[test]
fn cross_day_order_keeps_minimum_and_accrues_actual_dated_rates_once() {
    let r = rows();
    let components = vec![
        DatedFeeComponent {
            kind: FeeComponentKind::HandlingFee,
            effective_from: "2026-01-09".into(),
            effective_through: "2026-01-09".into(),
            included_in_commission: false,
            fact: FeeFact::Applicable {
                rate: d("0.01"),
                basis: MODEL.into(),
            },
        },
        DatedFeeComponent {
            kind: FeeComponentKind::HandlingFee,
            effective_from: "2026-01-12".into(),
            effective_through: "2026-12-31".into(),
            included_in_commission: false,
            fact: FeeFact::Applicable {
                rate: d("0.02"),
                basis: MODEL.into(),
            },
        },
    ];
    let mut a = account("10000", &r, S::NextSession, "5");
    a.install_terms(terms_with(
        &r[0],
        "A",
        S::NextSession,
        "0.001",
        "5",
        qf_core::rules::market::TradingStatus::Trading,
        SaleProceedsTiming::Immediate,
        components.clone(),
    ))
    .unwrap();
    a.reserve_order(request(&r[0], "gtc", Side::Buy, 100, "10", at(&r[0], 1)))
        .unwrap();
    let first = execute(&mut a, fill("gtc", "g1", Side::Buy, 10, "10", at(&r[0], 2)));
    a.settle(&r[1].session.key).unwrap();
    a.install_terms(terms_with(
        &r[1],
        "A",
        S::NextSession,
        "0.001",
        "5",
        qf_core::rules::market::TradingStatus::Trading,
        SaleProceedsTiming::Immediate,
        components,
    ))
    .unwrap();
    a.recheck_order("gtc").unwrap();
    let second = execute(&mut a, fill("gtc", "g2", Side::Buy, 30, "10", at(&r[1], 1)));
    // Commission 5 once; 100*1% + 300*2% = 7. Total fees = 12.
    assert_eq!((first.fee, second.fee), (d("6"), d("6")));
    assert_eq!(a.reservation("gtc").unwrap().fee_paid, d("12"));
    a.release("gtc").unwrap();
    assert_eq!(a.value(at(&r[1], 1)).unwrap().cash, d("9588"));
    assert_eq!(
        a.value(at(&r[1], 1)).unwrap().positions[&sec("A")].cost_basis_total,
        d("412")
    );
    assert_eq!(a.sellable_quantity(&sec("A")).unwrap(), q(10));
}
#[test]
fn t_plus_one_does_not_freeze_synthetic_t_zero_and_cross_day_sell_order_is_retained() {
    let r = rows();
    for turnover in [S::SameSession, S::NextSession] {
        let mut a = account("2000", &r, turnover, "0");
        trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
        if turnover == S::NextSession {
            code(
                a.reserve_order(request(&r[0], "early", Side::Sell, 1, "10", at(&r[0], 3))),
                ErrorCode::InsufficientSellable,
            );
        } else {
            a.reserve_order(request(&r[0], "sell", Side::Sell, 40, "10", at(&r[0], 3)))
                .unwrap();
        }
        a.settle(&r[1].session.key).unwrap();
        a.install_terms(terms(&r[1], "A", turnover, "0", "0"))
            .unwrap();
        if turnover == S::NextSession {
            a.reserve_order(request(&r[1], "sell", Side::Sell, 40, "10", at(&r[1], 1)))
                .unwrap();
        }
        assert_eq!(a.sellable_quantity(&sec("A")).unwrap(), q(60));
        a.settle(&r[1].session.key).unwrap(); // idempotent, no double release.
        execute(
            &mut a,
            fill("sell", "sold", Side::Sell, 20, "10", at(&r[1], 2)),
        );
        a.release("sell").unwrap();
        assert_eq!(a.sellable_quantity(&sec("A")).unwrap(), q(80));
    }
}
#[test]
fn unfilled_sale_cannot_finance_buy() {
    assert_eq!(
        oracle("unfilled_sale_not_cash")["expected"],
        "INSUFFICIENT_CASH"
    );
    let r = rows();
    let mut a = account("1000", &r, S::SameSession, "0");
    a.install_terms(terms(&r[0], "B", S::SameSession, "0", "0"))
        .unwrap();
    trade(&mut a, &r[0], "buy-A", Side::Buy, 100, "10", 1);
    a.reserve_order(request(
        &r[0],
        "sell-A",
        Side::Sell,
        100,
        "10",
        at(&r[0], 3),
    ))
    .unwrap();
    let mut buy = request(&r[0], "buy-B", Side::Buy, 1, "900", at(&r[0], 4));
    buy.security = sec("B");
    code(a.reserve_order(buy), ErrorCode::InsufficientCash);
    assert_eq!(a.value(at(&r[0], 4)).unwrap().available_cash, d("0"));
    assert_eq!(a.held_quantity(&sec("A")), q(100));
}
#[test]
fn price_gap_affordability_uses_own_reserve_and_checked_integer_units() {
    let r = rows();
    let mut a = account("100", &r, S::SameSession, "1");
    a.reserve_order(request(&r[0], "buy", Side::Buy, 20, "10", at(&r[0], 1)))
        .unwrap();
    assert_eq!(
        a.max_affordable("buy", q(20), p("12"), QuantityStep::new(1).unwrap())
            .unwrap(),
        q(8)
    );
    assert_eq!(
        a.max_affordable("buy", q(20), p("12"), QuantityStep::new(3).unwrap())
            .unwrap(),
        q(6)
    );
    let before = a.reservation("buy").unwrap();
    code(
        a.quote_fill(&fill("buy", "too-large", Side::Buy, 9, "12", at(&r[0], 2))),
        ErrorCode::InsufficientCash,
    );
    assert_eq!(a.reservation("buy").unwrap(), before);
    execute(
        &mut a,
        fill("buy", "gap-fill", Side::Buy, 8, "12", at(&r[0], 2)),
    );
    let v = a.value(at(&r[0], 2)).unwrap();
    assert_eq!(
        (v.cash, v.available_cash, v.frozen_cash),
        (d("3"), d("0"), d("3"))
    );
    a.release("buy").unwrap();
    assert_eq!(a.available_cash().unwrap(), d("3"));
}
#[test]
fn explicit_delayed_sale_cash_is_blocked_until_next_session() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    a.install_terms(terms_with(
        &r[0],
        "A",
        S::SameSession,
        "0",
        "0",
        qf_core::rules::market::TradingStatus::Trading,
        SaleProceedsTiming::NextSession,
        vec![],
    ))
    .unwrap();
    trade(&mut a, &r[0], "sell", Side::Sell, 40, "12", 3);
    let v = a.value(at(&r[0], 4)).unwrap();
    assert_eq!(
        (v.cash, v.available_cash, v.frozen_cash),
        (d("1480"), d("1000"), d("480"))
    );
    code(
        a.reserve_order(request(&r[0], "unpaid", Side::Buy, 1, "1100", at(&r[0], 5))),
        ErrorCode::InsufficientCash,
    );
    a.settle(&r[1].session.key).unwrap();
    assert_eq!(a.available_cash().unwrap(), d("1480"));
}
#[test]
fn day_release_and_next_session_day_reserve_do_not_share_expiry() {
    let r = rows();
    let mut a = account("1000", &r, S::NextSession, "1");
    let mut current = request(&r[0], "current", Side::Buy, 10, "10", at(&r[0], 1));
    current.tif = TimeInForce::Day;
    a.reserve_order(current).unwrap();
    a.release("current").unwrap();
    let mut after = request(&r[0], "after", Side::Buy, 10, "10", r[0].after_close_ns);
    after.tif = TimeInForce::Day;
    after.effective_session = r[1].session.key.clone();
    a.reserve_order(after).unwrap();
    a.settle(&r[1].session.key).unwrap();
    a.install_terms(terms(&r[1], "A", S::NextSession, "0", "1"))
        .unwrap();
    execute(
        &mut a,
        fill("after", "next", Side::Buy, 4, "10", at(&r[1], 1)),
    );
    assert_eq!(a.reservation("after").unwrap().remaining, q(6));
    a.release("after").unwrap();
    assert_eq!(a.frozen_cash().unwrap(), d("0"));
    assert_eq!(a.totals().trading_fees, d("1"));
}

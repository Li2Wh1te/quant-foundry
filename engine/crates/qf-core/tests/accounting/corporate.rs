use super::support::*;
use qf_core::ErrorCode;
use qf_core::accounting::*;
use qf_core::orders::Side;
use qf_core::rules::fees::InvestorKind;
use qf_core::rules::market::SellAvailability as S;
use qf_core::types::RoundingPolicy;

#[test]
fn overdue_close_payment_cannot_hide_in_receivables_after_position_is_fully_sold() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    let mut action = dividend(&r, CashDividendTax::SyntheticFlat { rate: d("0") });
    if let CorporateActionKind::CashDividend { payment, .. } = &mut action.kind {
        *payment = boundary(&r[1], ActionPhase::SessionClose);
    }
    a.add_corporate_action(action, r[0].before_open_ns).unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    trade(&mut a, &r[1], "sell", Side::Sell, 100, "9.8", 1);
    let stats = a.totals().clone();
    assert_eq!(a.held_quantity(&sec("A")), q(0));
    code(
        a.check_session_end(&r[1].session.key),
        ErrorCode::RuleUnavailable,
    );
    code(a.value(r[1].after_close_ns), ErrorCode::RuleUnavailable);
    assert_eq!(a.totals(), &stats);
    // Catching a read error did not consume or lose the registered right.
    close(&mut a, &r[1]);
    a.check_session_end(&r[1].session.key).unwrap();
    let v = a.value(r[1].after_close_ns).unwrap();
    assert_eq!(v.cash, d("2000"));
    assert_eq!(v.receivables, d("0"));
    assert_eq!(v.total_value, Some(d("2000")));
    assert_eq!(a.totals(), &stats);
}

#[test]
fn dated_dividend_tax_adjustments_settle_in_cents_with_one_cumulative_liability() {
    let r: Vec<_> = ["2015-01-12", "2015-01-13", "2015-01-14"]
        .iter()
        .enumerate()
        .map(|(i, date)| row(i, date))
        .collect();
    let mut a = account("2000", &r, S::SameSession, "0");
    let mut action = dividend(
        &r,
        CashDividendTax::DatedStock {
            investor: InvestorKind::ResidentIndividual,
            restricted_stock: false,
        },
    );
    if let CorporateActionKind::CashDividend { per_share, .. } = &mut action.kind {
        *per_share = d("0.1");
    }
    action.ex_raw_mark = Some(raw(&r[1], "9.9", r[1].before_open_ns));
    a.add_corporate_action(action, r[0].before_open_ns).unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 3, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    // Gross .30, initial 5% = .015 -> .02; net payable .28.
    assert_eq!(a.value(r[1].before_open_ns).unwrap().receivables, d("0.28"));
    assert_eq!(a.totals().dividend_tax, d("0.02"));
    // Cumulative short-holding liability .02/.04/.06, less allocated initial
    // .02/3, 2*.02/3, .02. Round cumulative adjustment to cents, then subtract
    // already settled adjustment: additional charges .01/.02/.01.
    for (i, expected_tax) in ["0.03", "0.05", "0.06"].iter().enumerate() {
        trade(
            &mut a,
            &r[1],
            &format!("sale-{i}"),
            Side::Sell,
            1,
            "9.9",
            1 + i as i64 * 2,
        );
        let v = a.value(at(&r[1], 2 + i as i64 * 2)).unwrap();
        assert_eq!(a.totals().dividend_tax, d(expected_tax));
        assert_eq!(v.cash.round(2, RoundingPolicy::HalfEven).unwrap(), v.cash);
    }
    close(&mut a, &r[1]);
    open(&mut a, &r[2], S::SameSession, "0");
    assert_eq!(a.value(r[2].before_open_ns).unwrap().cash, d("1999.94"));
    assert_eq!(a.totals().realized_pnl, d("-0.06"));
}

#[test]
fn dividend_contract_moves_receivable_to_cash_without_second_income() {
    let r = rows();
    let mut a = account("2000", &r, S::NextSession, "0");
    a.add_corporate_action(
        dividend(&r, CashDividendTax::SyntheticFlat { rate: d("0") }),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    assert_eq!(a.value(r[0].session.close_ns).unwrap().receivables, d("0"));
    open(&mut a, &r[1], S::NextSession, "0");
    let v = a.value(r[1].before_open_ns).unwrap();
    let oracle = oracle("cash_dividend");
    let expected = &oracle["expected_before_payment"];
    assert_eq!(v.receivables, decimal(&expected["receivable"]));
    assert_eq!(
        v.positions[&sec("A")].market_value,
        Some(decimal(&expected["market_value"]))
    );
    assert_eq!(
        v.total_value.unwrap().checked_sub(d("1000")).unwrap(),
        decimal(&expected["equity"])
    );
    close(&mut a, &r[1]);
    let before = a.totals().clone();
    open(&mut a, &r[2], S::NextSession, "0");
    let v = a.value(r[2].before_open_ns).unwrap();
    assert_eq!(v.cash, d("1020"));
    assert_eq!(v.receivables, d("0"));
    assert_eq!(v.total_value, Some(d("2000")));
    assert_eq!(a.totals(), &before);
    assert!(
        a.advance_corporate_actions(&boundary(&r[2], ActionPhase::BeforeOpen))
            .unwrap()
            .effects
            .is_empty()
    );
    assert_eq!(a.value(r[2].before_open_ns).unwrap().cash, d("1020"));
}
#[test]
fn registration_keeps_sold_entitlement_and_excludes_subsequent_purchases() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    a.add_corporate_action(
        dividend(&r, CashDividendTax::SyntheticFlat { rate: d("0") }),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "original", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    trade(&mut a, &r[1], "sold", Side::Sell, 100, "9.8", 1);
    trade(&mut a, &r[1], "later", Side::Buy, 100, "9.8", 3);
    assert_eq!(a.value(at(&r[1], 4)).unwrap().receivables, d("20"));
    close(&mut a, &r[1]);
    open(&mut a, &r[2], S::SameSession, "0");
    assert_eq!(a.value(r[2].before_open_ns).unwrap().cash, d("1020"));
    assert_eq!(a.totals().dividend_income, d("20"));
}
#[test]
fn entitlement_policy_is_explicit_and_frozen_sell_units_are_still_held() {
    let r = rows();
    let mut settled = account("2000", &r, S::NextSession, "0");
    let mut action = dividend(&r, CashDividendTax::SyntheticFlat { rate: d("0") });
    action.entitlement = EntitlementRule::SettledOnly;
    settled
        .add_corporate_action(action, r[0].before_open_ns)
        .unwrap();
    trade(&mut settled, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut settled, &r[0]);
    open(&mut settled, &r[1], S::NextSession, "0");
    assert_eq!(
        settled.value(r[1].before_open_ns).unwrap().receivables,
        d("0")
    );
    let mut frozen = account("2000", &r, S::SameSession, "0");
    frozen
        .add_corporate_action(
            dividend(&r, CashDividendTax::SyntheticFlat { rate: d("0") }),
            r[0].before_open_ns,
        )
        .unwrap();
    trade(&mut frozen, &r[0], "buy", Side::Buy, 100, "10", 1);
    frozen
        .reserve_order(request(
            &r[0],
            "sell-gtc",
            Side::Sell,
            100,
            "10",
            at(&r[0], 3),
        ))
        .unwrap();
    close(&mut frozen, &r[0]);
    let report = open(&mut frozen, &r[1], S::SameSession, "0");
    assert_eq!(report.cancelled_order_ids, vec!["sell-gtc"]);
    assert_eq!(
        frozen.value(r[1].before_open_ns).unwrap().receivables,
        d("20")
    );
    assert_eq!(frozen.sellable_quantity(&sec("A")).unwrap(), q(100));
}
#[test]
fn split_contract_preserves_total_cost_and_separately_releases_listed_units() {
    let r = rows();
    let mut a = account("2000", &r, S::NextSession, "0");
    a.add_corporate_action(
        split(
            &r,
            ShareRatio {
                numerator: 2,
                denominator: 1,
            },
            2,
        ),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::NextSession, "0");
    let v = a.value(r[1].before_open_ns).unwrap();
    let expected = &oracle("split")["expected"];
    assert_eq!(
        v.positions[&sec("A")].quantity.get(),
        expected["quantity"].as_i64().unwrap()
    );
    assert_eq!(v.positions[&sec("A")].cost_basis_total, d("1000"));
    assert_eq!(
        v.positions[&sec("A")].average_cost,
        decimal(&expected["price"])
    );
    assert_eq!(
        v.positions[&sec("A")].market_value,
        Some(decimal(&expected["market_value"]))
    );
    assert_eq!(v.positions[&sec("A")].sellable, q(0));
    code(
        a.reserve_order(request(&r[1], "early", Side::Sell, 1, "5", at(&r[1], 1))),
        ErrorCode::InsufficientSellable,
    );
    close(&mut a, &r[1]);
    open(&mut a, &r[2], S::NextSession, "0");
    assert_eq!(a.sellable_quantity(&sec("A")).unwrap(), q(200));
    trade(&mut a, &r[2], "sell", Side::Sell, 200, "5", 1);
    assert_eq!(a.totals().realized_pnl, d("0"));
    assert_eq!(a.value(at(&r[2], 2)).unwrap().cash, d("2000"));
}
#[test]
fn share_distribution_is_not_sellable_before_listing_and_never_mints_cost() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    let mut action = split(
        &r,
        ShareRatio {
            numerator: 2,
            denominator: 1,
        },
        2,
    );
    action.kind = CorporateActionKind::ShareDistribution {
        additional_ratio: ShareRatio {
            numerator: 1,
            denominator: 1,
        },
        sellable_session: r[2].session.key.clone(),
        acquisition: ShareAcquisitionDate::InheritRecordLots,
        tax: share_tax(),
    };
    a.add_corporate_action(action, r[0].before_open_ns).unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    let v = a.value(r[1].before_open_ns).unwrap();
    assert_eq!(v.positions[&sec("A")].quantity, q(200));
    assert_eq!(v.positions[&sec("A")].sellable, q(100));
    assert_eq!(v.positions[&sec("A")].cost_basis_total, d("1000"));
    assert_eq!(v.cash, d("1000"));
    trade(&mut a, &r[1], "sell-old", Side::Sell, 100, "5", 1);
    assert_eq!(a.sellable_quantity(&sec("A")).unwrap(), q(0));
    close(&mut a, &r[1]);
    open(&mut a, &r[2], S::SameSession, "0");
    trade(&mut a, &r[2], "sell-new", Side::Sell, 100, "5", 1);
    assert_eq!(a.totals().realized_pnl, d("0"));
    assert_eq!(a.value(at(&r[2], 2)).unwrap().cash, d("2000"));
}
#[test]
fn registered_bonus_is_kept_when_original_shares_are_sold_before_ex() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    let mut action = split(
        &r,
        ShareRatio {
            numerator: 2,
            denominator: 1,
        },
        3,
    );
    action.ex = boundary(&r[2], ActionPhase::BeforeOpen);
    action.ex_raw_mark = Some(raw(&r[2], "5", r[2].before_open_ns));
    action.kind = CorporateActionKind::ShareDistribution {
        additional_ratio: ShareRatio {
            numerator: 1,
            denominator: 1,
        },
        sellable_session: r[3].session.key.clone(),
        acquisition: ShareAcquisitionDate::ExDate,
        tax: share_tax(),
    };
    a.add_corporate_action(action, r[0].before_open_ns).unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    trade(&mut a, &r[1], "sold", Side::Sell, 100, "10", 1);
    close(&mut a, &r[1]);
    open(&mut a, &r[2], S::SameSession, "0");
    let v = a.value(r[2].before_open_ns).unwrap();
    assert_eq!(v.positions[&sec("A")].quantity, q(100));
    assert_eq!(v.positions[&sec("A")].cost_basis_total, d("0"));
    assert_eq!(v.positions[&sec("A")].sellable, q(0));
}
#[test]
fn actual_d02_fifo_tax_is_deducted_once_across_disposals_and_payment() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    a.add_corporate_action(
        dividend(
            &r,
            CashDividendTax::DatedStock {
                investor: InvestorKind::ResidentIndividual,
                restricted_stock: false,
            },
        ),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    trade(&mut a, &r[1], "short", Side::Sell, 20, "9.8", 1);
    assert_eq!(a.totals().dividend_tax, d("0.8")); // 20*.2*.20
    close(&mut a, &r[1]);
    open(&mut a, &r[2], S::SameSession, "0");
    assert_eq!(a.totals().dividend_income, d("20"));
    assert_eq!(a.totals().dividend_tax, d("0.8"));
    trade(&mut a, &r[2], "over-month", Side::Sell, 80, "9.8", 1);
    assert_eq!(a.totals().dividend_tax, d("2.4")); // .8 + 80*.2*.10
    assert_eq!(a.totals().realized_pnl, d("-2.4"));
    assert_eq!(a.value(at(&r[2], 2)).unwrap().cash, d("1997.6"));
}
#[test]
fn old_d02_initial_withholding_is_not_recharged_at_payment_or_final_sale() {
    let r: Vec<_> = ["2015-09-07", "2015-09-09", "2016-09-08"]
        .iter()
        .enumerate()
        .map(|(i, d)| row(i, d))
        .collect();
    let mut a = account("2000", &r, S::SameSession, "0");
    a.add_corporate_action(
        dividend(
            &r,
            CashDividendTax::DatedStock {
                investor: InvestorKind::ResidentIndividual,
                restricted_stock: false,
            },
        ),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    assert_eq!(a.value(r[1].before_open_ns).unwrap().receivables, d("19"));
    assert_eq!(a.totals().dividend_tax, d("1"));
    trade(&mut a, &r[1], "short", Side::Sell, 50, "9.8", 1);
    assert_eq!(a.totals().dividend_tax, d("2.5")); // initial1 + (10*.20 - .5)
    close(&mut a, &r[1]);
    open(&mut a, &r[2], S::SameSession, "0");
    trade(&mut a, &r[2], "long", Side::Sell, 50, "9.8", 1);
    assert_eq!(a.totals().dividend_tax, d("2.5"));
    assert_eq!(a.value(at(&r[2], 2)).unwrap().cash, d("1997.5"));
}
#[test]
fn prior_cash_dividend_claim_survives_split_without_doubling_tax_base() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    a.add_corporate_action(
        dividend(
            &r,
            CashDividendTax::DatedStock {
                investor: InvestorKind::ResidentIndividual,
                restricted_stock: false,
            },
        ),
        r[0].before_open_ns,
    )
    .unwrap();
    let mut s = split(
        &r,
        ShareRatio {
            numerator: 2,
            denominator: 1,
        },
        1,
    );
    s.ex_raw_mark = Some(raw(&r[1], "4.9", r[1].before_open_ns));
    a.add_corporate_action(s, r[0].before_open_ns).unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    assert_eq!(a.value(r[1].before_open_ns).unwrap().receivables, d("20"));
    assert_eq!(
        a.value(r[1].before_open_ns).unwrap().total_value,
        Some(d("2000"))
    );
    trade(&mut a, &r[1], "sell", Side::Sell, 200, "4.9", 1);
    assert_eq!(a.totals().dividend_tax, d("4")); // original 100*.2*.20
    close(&mut a, &r[1]);
    open(&mut a, &r[2], S::SameSession, "0");
    assert_eq!(a.value(r[2].before_open_ns).unwrap().cash, d("1996"));
}
#[test]
fn missing_holding_band_refuses_atomically_then_accepted_provider_band_can_apply() {
    let r: Vec<_> = ["2026-01-31", "2026-02-28", "2026-03-02"]
        .iter()
        .enumerate()
        .map(|(i, d)| row(i, d))
        .collect();
    let mut a = account("2000", &r, S::SameSession, "0");
    a.add_corporate_action(
        dividend(
            &r,
            CashDividendTax::DatedStock {
                investor: InvestorKind::ResidentIndividual,
                restricted_stock: false,
            },
        ),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    let before = a.value(r[1].before_open_ns).unwrap();
    code(
        a.reserve_order(request(&r[1], "sell", Side::Sell, 10, "9.8", at(&r[1], 1))),
        ErrorCode::RuleUnavailable,
    );
    assert_eq!(a.value(r[1].before_open_ns).unwrap(), before);
    assert_eq!(a.active_reservation_count(), 0);
    let mut t = terms(&r[1], "A", S::SameSession, "0", "0");
    t.holding_bands.insert(
        r[0].date.clone(),
        qf_core::rules::dividends::HoldingBand::UpToOneMonth,
    );
    a.install_terms(t).unwrap();
    trade(&mut a, &r[1], "sell", Side::Sell, 10, "9.8", 1);
    assert_eq!(a.totals().dividend_tax, d("0.4"));
}
#[test]
fn rights_do_not_subscribe_and_unknown_necessary_terms_are_rejected() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    let mut action = split(
        &r,
        ShareRatio {
            numerator: 2,
            denominator: 1,
        },
        1,
    );
    action.action_id = "rights".into();
    action.kind = CorporateActionKind::RightsIssue {
        offered_ratio: ShareRatio {
            numerator: 1,
            denominator: 10,
        },
        subscription_price: p("4"),
        tradable_rights: None,
        lapse_compensation: Some(d("0")),
    };
    code(
        a.add_corporate_action(action.clone(), r[0].before_open_ns),
        ErrorCode::RuleUnavailable,
    );
    action.kind = CorporateActionKind::RightsIssue {
        offered_ratio: ShareRatio {
            numerator: 1,
            denominator: 10,
        },
        subscription_price: p("4"),
        tradable_rights: Some(false),
        lapse_compensation: Some(d("0")),
    };
    a.add_corporate_action(action, r[0].before_open_ns).unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    let report = open(&mut a, &r[1], S::SameSession, "0");
    assert!(
        report
            .effects
            .iter()
            .any(|e| e.kind == CorporateEffectKind::RightsNotParticipated
                && e.policy.contains("no external funds"))
    );
    let v = a.value(r[1].before_open_ns).unwrap();
    assert_eq!(v.cash, d("1000"));
    assert_eq!(v.positions[&sec("A")].quantity, q(100));
}
#[test]
fn necessary_share_tax_and_dated_dividend_tax_gaps_do_not_become_zero() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    let mut s = split(
        &r,
        ShareRatio {
            numerator: 2,
            denominator: 1,
        },
        1,
    );
    s.kind = CorporateActionKind::Split {
        ratio: ShareRatio {
            numerator: 2,
            denominator: 1,
        },
        sellable_session: r[1].session.key.clone(),
        tax: None,
    };
    code(
        a.add_corporate_action(s, r[0].before_open_ns),
        ErrorCode::RuleUnavailable,
    );
    a.add_corporate_action(
        dividend(
            &r,
            CashDividendTax::DatedStock {
                investor: InvestorKind::ResidentIndividual,
                restricted_stock: true,
            },
        ),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    let before = a.value(at(&r[0], 2)).unwrap();
    code(
        a.advance_corporate_actions(&boundary(&r[0], ActionPhase::SessionClose)),
        ErrorCode::RuleUnavailable,
    );
    assert_eq!(a.value(at(&r[0], 2)).unwrap(), before);
    assert_eq!(a.totals().dividend_income, d("0"));
    // No after-the-fact registration with guessed prior holdings.
    a.settle(&r[1].session.key).unwrap();
    a.install_terms(terms(&r[1], "A", S::SameSession, "0", "0"))
        .unwrap();
    code(
        a.advance_corporate_actions(&boundary(&r[1], ActionPhase::BeforeOpen)),
        ErrorCode::RuleUnavailable,
    );
}
#[test]
fn fractional_consolidation_needs_facts_and_rolls_back_cancellation() {
    let r = rows();
    let mut a = account("20", &r, S::SameSession, "0");
    a.add_corporate_action(
        split(
            &r,
            ShareRatio {
                numerator: 1,
                denominator: 2,
            },
            1,
        ),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 3, "1", 1);
    a.reserve_order(request(&r[0], "sell", Side::Sell, 1, "1", at(&r[0], 3)))
        .unwrap();
    close(&mut a, &r[0]);
    a.settle(&r[1].session.key).unwrap();
    a.install_terms(terms(&r[1], "A", S::SameSession, "0", "0"))
        .unwrap();
    code(
        a.advance_corporate_actions(&boundary(&r[1], ActionPhase::BeforeOpen)),
        ErrorCode::RuleUnavailable,
    );
    assert!(a.reservation("sell").is_some());
    assert_eq!(a.held_quantity(&sec("A")), q(3));
}

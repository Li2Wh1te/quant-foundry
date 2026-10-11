use super::support::*;
use qf_core::ErrorCode;
use qf_core::accounting::*;
use qf_core::orders::Side;
use qf_core::rules::market::{SellAvailability as S, TradingStatus};
use qf_core::types::keys::ChannelKey;
use qf_core::types::market::{Currency, MarketUnits, QuantityUnit};
use qf_core::types::{EventIdentity, ExactDecimal, MarketEvent, QuoteTick, Sequence};

#[test]
fn wrong_fee_and_replayed_fill_do_not_mutate_the_authoritative_account() {
    let r = rows();
    let mut a = account("100", &r, S::SameSession, "5");
    a.reserve_order(request(&r[0], "buy", Side::Buy, 5, "10", at(&r[0], 1)))
        .unwrap();
    let candidate = fill("buy", "first", Side::Buy, 1, "10", at(&r[0], 2));
    let before = a.value(at(&r[0], 1)).unwrap();
    let reserve = a.reservation("buy").unwrap();
    code(a.apply_fill(&candidate), ErrorCode::InvalidContract);
    assert_eq!(a.value(at(&r[0], 1)).unwrap(), before);
    assert_eq!(a.reservation("buy").unwrap(), reserve);
    let assessed = a.assess_fill(&candidate).unwrap();
    assert_eq!(assessed.fee, d("5"));
    let (_, prepared) = a.preview(|_| Ok(())).unwrap();
    assert_eq!(a.assess_fill(&candidate).unwrap(), assessed);
    assert_eq!(a.value(at(&r[0], 1)).unwrap(), before);
    assert_eq!(a.reservation("buy").unwrap(), reserve);
    assert_eq!(a.totals().trading_fees, d("0"));
    a.commit(prepared).unwrap(); // assessment did not advance account revision.
    a.apply_fill(&assessed).unwrap();
    let after = a.value(at(&r[0], 2)).unwrap();
    code(a.apply_fill(&assessed), ErrorCode::InvalidContract);
    assert_eq!(a.value(at(&r[0], 2)).unwrap(), after);
    a.release("buy").unwrap();
    code(a.apply_fill(&assessed), ErrorCode::InvalidContract);
}
#[test]
fn target_candidate_failure_retains_original_and_ordinary_reservations() {
    let r = rows();
    let mut a = account("100", &r, S::SameSession, "1");
    a.install_terms(terms(&r[0], "B", S::SameSession, "0", "1"))
        .unwrap();
    a.reserve_order(request(
        &r[0],
        "old-target",
        Side::Buy,
        5,
        "10",
        at(&r[0], 1),
    ))
    .unwrap();
    let mut ordinary = request(&r[0], "ordinary", Side::Buy, 3, "10", at(&r[0], 1));
    ordinary.security = sec("B");
    a.reserve_order(ordinary).unwrap();
    let before = a.value(at(&r[0], 1)).unwrap();
    code(
        a.transact(|candidate| {
            candidate.release("old-target")?;
            candidate.reserve_order(request(
                &r[0],
                "replacement",
                Side::Buy,
                1,
                "200",
                at(&r[0], 2),
            ))
        }),
        ErrorCode::InsufficientCash,
    );
    assert_eq!(a.value(at(&r[0], 1)).unwrap(), before);
    assert_eq!(a.reservation("old-target").unwrap().frozen_cash, d("51"));
    assert_eq!(a.reservation("ordinary").unwrap().frozen_cash, d("31"));
    a.transact(|candidate| {
        candidate.release("old-target")?;
        candidate.reserve_order(request(
            &r[0],
            "replacement",
            Side::Buy,
            6,
            "10",
            at(&r[0], 2),
        ))
    })
    .unwrap();
    assert!(a.reservation("old-target").is_none());
    assert_eq!(a.reservation("replacement").unwrap().frozen_cash, d("61"));
    assert_eq!(a.reservation("ordinary").unwrap().frozen_cash, d("31"));
    assert_eq!(a.available_cash().unwrap(), d("8"));
}
#[test]
fn stale_or_cross_account_prepared_state_is_rejected() {
    let r = rows();
    let mut a = account("100", &r, S::SameSession, "0");
    let mut b = account("1000", &r, S::SameSession, "0");
    let (_, prepared) = a
        .preview(|c| {
            c.reserve_order(request(
                &r[0],
                "candidate",
                Side::Buy,
                1,
                "10",
                at(&r[0], 1),
            ))
        })
        .unwrap();
    code(b.commit(prepared), ErrorCode::InvalidContract);
    assert_eq!(b.available_cash().unwrap(), d("1000"));
    let (_, prepared) = a
        .preview(|c| {
            c.reserve_order(request(
                &r[0],
                "candidate",
                Side::Buy,
                1,
                "10",
                at(&r[0], 1),
            ))
        })
        .unwrap();
    a.reserve_order(request(&r[0], "live", Side::Buy, 1, "10", at(&r[0], 1)))
        .unwrap();
    code(a.commit(prepared), ErrorCode::InvalidContract);
    assert!(a.reservation("candidate").is_none());
    assert!(a.reservation("live").is_some());
}
#[test]
fn extreme_decimal_cash_overflow_leaves_fill_and_fees_uncommitted() {
    let r = rows();
    let mut a = account("79228162514264337593543950335", &r, S::SameSession, "0");
    trade(&mut a, &r[0], "buy", Side::Buy, 1, "1", 1);
    a.reserve_order(request(&r[0], "sell", Side::Sell, 1, "2", at(&r[0], 3)))
        .unwrap();
    let before = a.value(at(&r[0], 3)).unwrap();
    let stats = a.totals().clone();
    let reserved = a.reservation("sell").unwrap();
    code(
        a.quote_fill(&fill("sell", "overflow", Side::Sell, 1, "2", at(&r[0], 4))),
        ErrorCode::NumericRangeUnsupported,
    );
    assert_eq!(a.value(at(&r[0], 3)).unwrap(), before);
    assert_eq!(a.totals(), &stats);
    assert_eq!(a.reservation("sell").unwrap(), reserved);
}
#[test]
fn extreme_quantity_overflow_does_not_spend_cash_or_advance_fee_accumulator() {
    let r = rows();
    let mut a = account("79228162514264337593543950335", &r, S::SameSession, "0");
    trade(&mut a, &r[0], "huge", Side::Buy, i64::MAX, "1", 1);
    a.reserve_order(request(&r[0], "extra", Side::Buy, 1, "1", at(&r[0], 3)))
        .unwrap();
    let before = a.value(at(&r[0], 3)).unwrap();
    code(
        a.quote_fill(&fill("extra", "overflow", Side::Buy, 1, "1", at(&r[0], 4))),
        ErrorCode::NumericRangeUnsupported,
    );
    assert_eq!(a.value(at(&r[0], 3)).unwrap(), before);
    assert_eq!(a.reservation("extra").unwrap().cumulative_notional, d("0"));
}
#[test]
fn corporate_batch_overflow_rolls_back_earlier_dividend_and_order_cancellation() {
    let r = rows();
    let mut a = account("100000000000000000000", &r, S::SameSession, "0");
    a.add_corporate_action(
        dividend(&r, CashDividendTax::SyntheticFlat { rate: d("0") }),
        r[0].before_open_ns,
    )
    .unwrap();
    a.add_corporate_action(
        split(
            &r,
            ShareRatio {
                numerator: 2,
                denominator: 1,
            },
            1,
        ),
        r[0].before_open_ns,
    )
    .unwrap();
    trade(&mut a, &r[0], "huge", Side::Buy, i64::MAX, "1", 1);
    a.reserve_order(request(&r[0], "sell", Side::Sell, 1, "1", at(&r[0], 3)))
        .unwrap();
    close(&mut a, &r[0]);
    a.settle(&r[1].session.key).unwrap();
    a.install_terms(terms(&r[1], "A", S::SameSession, "0", "0"))
        .unwrap();
    let before = a.value(r[1].before_open_ns).unwrap();
    let totals = a.totals().clone();
    let reservation = a.reservation("sell").unwrap();
    code(
        a.advance_corporate_actions(&boundary(&r[1], ActionPhase::BeforeOpen)),
        ErrorCode::NumericRangeUnsupported,
    );
    assert_eq!(a.value(r[1].before_open_ns).unwrap(), before);
    assert_eq!(a.totals(), &totals);
    assert_eq!(a.reservation("sell").unwrap(), reservation);
    assert_eq!(before.receivables, d("0"));
}
#[test]
fn known_halt_stale_value_and_unknown_missing_value_are_distinct() {
    let r = rows();
    let mut a = account("2000", &r, S::NextSession, "0");
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    a.settle(&r[1].session.key).unwrap();
    a.install_terms(terms(&r[1], "A", S::NextSession, "0", "0"))
        .unwrap();
    let missing = a.valuation(r[1].session.close_ns).unwrap();
    assert_eq!(missing.account.total_value, None);
    assert_eq!(missing.account.positions[&sec("A")].market_value, None);
    assert_eq!(
        missing.valuation[&sec("A")].reason,
        ValuationReason::UnknownMissingPrice
    );
    a.install_terms(terms_with(
        &r[1],
        "A",
        S::NextSession,
        "0",
        "0",
        TradingStatus::Halted,
        SaleProceedsTiming::Immediate,
        vec![],
    ))
    .unwrap();
    let halted = a.valuation(r[1].session.close_ns).unwrap();
    assert_eq!(halted.account.total_value, Some(d("2000")));
    assert_eq!(
        halted.valuation[&sec("A")].reason,
        ValuationReason::KnownHaltLastRaw
    );
    assert!(halted.valuation[&sec("A")].is_stale);
    assert_eq!(
        halted.valuation[&sec("A")].price_time_ns,
        Some(at(&r[0], 2))
    );
    code(
        a.reserve_order(request(&r[1], "halted", Side::Buy, 1, "10", at(&r[1], 1))),
        ErrorCode::InvalidOrder,
    );
}
#[test]
fn research_adjustment_and_quote_midpoint_cannot_create_raw_value() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    let mut adjusted = raw(&r[0], "100", at(&r[0], 3));
    adjusted.basis = PriceBasis::ResearchAdjusted;
    code(
        a.set_raw_mark(sec("A"), adjusted, at(&r[0], 3)),
        ErrorCode::InvalidContract,
    );
    a.settle(&r[1].session.key).unwrap();
    a.install_terms(terms(&r[1], "A", S::SameSession, "0", "0"))
        .unwrap();
    let quote = MarketEvent::QuoteTick(QuoteTick {
        security: sec("A"),
        session: r[1].session.key.clone(),
        identity: EventIdentity {
            source_session: r[1].session.key.clone(),
            channel: ChannelKey::new("quote").unwrap(),
            sequence: Some(Sequence::new(1)),
            stable_input_sequence: Sequence::new(1),
        },
        time_ns: at(&r[1], 1),
        bid: Some(p("9")),
        ask: Some(p("11")),
        bid_quantity: Some(q(100)),
        ask_quantity: Some(q(100)),
        units: MarketUnits {
            price_currency: Currency::CNY,
            quantity_unit: QuantityUnit::Shares,
        },
    });
    a.mark(&quote).unwrap();
    assert_eq!(a.value(at(&r[1], 1)).unwrap().total_value, None);
    mark(&mut a, &r[1], "10", at(&r[1], 2));
    assert_eq!(a.value(at(&r[1], 2)).unwrap().total_value, Some(d("2000")));
}
#[test]
fn ex_with_no_raw_price_is_uncomputable_and_pre_ex_raw_cannot_be_reused() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    let mut action = dividend(&r, CashDividendTax::SyntheticFlat { rate: d("0") });
    action.ex_raw_mark = None;
    a.add_corporate_action(action, r[0].before_open_ns).unwrap();
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    close(&mut a, &r[0]);
    open(&mut a, &r[1], S::SameSession, "0");
    assert_eq!(a.value(r[1].before_open_ns).unwrap().total_value, None);
    code(
        a.set_raw_mark(
            sec("A"),
            raw(&r[0], "10", at(&r[0], 2)),
            r[1].before_open_ns,
        ),
        ErrorCode::InvalidContract,
    );
    mark(&mut a, &r[1], "9.8", at(&r[1], 1));
    assert_eq!(a.value(at(&r[1], 1)).unwrap().total_value, Some(d("2000")));
}
#[test]
fn terminal_positions_remain_open_and_valuation_overflow_is_explicit() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    let v = a.close_view(&r[0].session.key).unwrap();
    assert_eq!(v.account.positions[&sec("A")].quantity, q(100));
    assert_eq!(v.account.total_value, Some(d("2000")));
    mark(&mut a, &r[0], "79228162514264337593543950335", at(&r[0], 3));
    code(
        a.terminal_view(at(&r[0], 3)),
        ErrorCode::NumericRangeUnsupported,
    );
    assert_eq!(a.held_quantity(&sec("A")), q(100));
}
#[test]
fn repeated_same_day_fills_coalesce_and_no_completed_order_history_grows() {
    let r = rows();
    let mut a = Account::new(
        d("2000"),
        r.clone(),
        usage(),
        AccountLimits {
            max_lots: 1,
            ..AccountLimits::default()
        },
    )
    .unwrap();
    a.install_terms(terms(&r[0], "A", S::SameSession, "0", "0"))
        .unwrap();
    a.settle(&r[0].session.key).unwrap();
    a.reserve_order(request(&r[0], "many", Side::Buy, 1000, "1", at(&r[0], 1)))
        .unwrap();
    for n in 0..1000 {
        execute(
            &mut a,
            fill(
                "many",
                &format!("f{n}"),
                Side::Buy,
                1,
                "1",
                at(&r[0], 2 + n),
            ),
        );
    }
    assert_eq!(a.held_quantity(&sec("A")), q(1000));
    assert_eq!(a.active_reservation_count(), 0);
    a.settle(&r[1].session.key).unwrap();
    a.install_terms(terms(&r[1], "A", S::SameSession, "0", "0"))
        .unwrap();
    a.reserve_order(request(&r[1], "new-date", Side::Buy, 1, "1", at(&r[1], 1)))
        .unwrap();
    code(
        a.quote_fill(&fill(
            "new-date",
            "resource",
            Side::Buy,
            1,
            "1",
            at(&r[1], 2),
        )),
        ErrorCode::ResourceLimit,
    );
    assert_eq!(a.held_quantity(&sec("A")), q(1000));
    assert_eq!(a.reservation("new-date").unwrap().fee_paid, d("0"));
}
#[test]
fn calendar_boundaries_and_decimal_inputs_are_not_silently_repaired() {
    struct UnwiredPort;
    impl AccountPort for UnwiredPort {
        fn validate_and_reserve(
            &mut self,
            _: &qf_core::orders::OrderIntent,
        ) -> qf_core::QfResult<qf_core::orders::OrderResult> {
            unreachable!()
        }
        fn apply_fill(&mut self, _: &qf_core::matching::Fill) -> qf_core::QfResult<()> {
            unreachable!()
        }
        fn release(&mut self, _: &str) -> qf_core::QfResult<()> {
            unreachable!()
        }
        fn settle(&mut self, _: &qf_core::types::SessionKey) -> qf_core::QfResult<()> {
            unreachable!()
        }
        fn value(&self, _: qf_core::types::Nanoseconds) -> qf_core::QfResult<AccountView> {
            unreachable!()
        }
    }
    // A source-compatible older adapter cannot silently omit the new real
    // closing postcondition, even when the strategy never queries its account.
    code(
        UnwiredPort.check_session_end(&key("S1")),
        ErrorCode::CapabilityUnavailable,
    );
    let r = rows();
    let mut a = account("2000", &r, S::NextSession, "0");
    code(a.settle(&r[2].session.key), ErrorCode::RuleUnavailable);
    code(a.settle(&key("unknown")), ErrorCode::RuleUnavailable);
    code(
        Account::new(d("0"), r.clone(), usage(), AccountLimits::default()),
        ErrorCode::InvalidRunConfig,
    );
    code(
        Account::new(d("-1"), r, usage(), AccountLimits::default()),
        ErrorCode::InvalidRunConfig,
    );
    for text in [
        "NaN",
        "Infinity",
        "0.00000000000000000000000000001",
        "79228162514264337593543950336",
    ] {
        assert!(text.parse::<ExactDecimal>().is_err());
    }
}
#[test]
fn accepted_run_commission_cannot_change_but_new_date_facts_can() {
    let r = rows();
    let mut a = account("1000", &r, S::NextSession, "5");
    a.reserve_order(request(&r[0], "gtc", Side::Buy, 10, "10", at(&r[0], 1)))
        .unwrap();
    let before = a.reservation("gtc").unwrap();
    code(
        a.install_terms(terms(&r[0], "A", S::NextSession, "0", "9")),
        ErrorCode::RuleUnavailable,
    );
    assert_eq!(a.reservation("gtc").unwrap(), before);
    a.settle(&r[1].session.key).unwrap();
    code(
        a.install_terms(terms(&r[1], "A", S::NextSession, "0.01", "5")),
        ErrorCode::RuleUnavailable,
    );
    a.install_terms(terms(&r[1], "A", S::NextSession, "0", "5"))
        .unwrap();
    let f = execute(
        &mut a,
        fill("gtc", "next", Side::Buy, 10, "10", at(&r[1], 1)),
    );
    assert_eq!(f.fee, d("5"));
    assert_eq!(a.totals().trading_fees, d("5"));
}
#[test]
fn mismatched_raw_session_and_future_account_view_do_not_masquerade_as_fresh() {
    let r = rows();
    let mut a = account("2000", &r, S::SameSession, "0");
    trade(&mut a, &r[0], "buy", Side::Buy, 100, "10", 1);
    let mut forged = raw(&r[0], "10", at(&r[0], 3));
    forged.session = r[1].session.key.clone();
    code(
        a.set_raw_mark(sec("A"), forged, at(&r[0], 3)),
        ErrorCode::InvalidContract,
    );
    code(a.value(r[1].before_open_ns), ErrorCode::RuleUnavailable);
    code(a.value(at(&r[0], 1)), ErrorCode::LookaheadForbidden);
    assert_eq!(a.value(at(&r[0], 2)).unwrap().total_value, Some(d("2000")));
}

use super::support::*;

#[test]
fn target_one_hundred_times_has_one_effective_delta_then_partial_cancel_keeps_real_fill() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    let now = time(row, 570);
    let first = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(100)),
        1,
        now,
        TimeInForce::Gtc,
    );
    assert!(first.accepted && !first.unchanged);
    let id = first.order_id.unwrap();
    for sequence in 2..=100 {
        let result = submit(
            &mut m,
            "A",
            IntentValue::TargetQuantity(q(100)),
            sequence,
            now,
            TimeInForce::Gtc,
        );
        assert!(result.accepted && result.unchanged && result.order_id.is_none());
        assert_eq!(result.effective_quantity, Some(q(100)));
    }
    assert_eq!(m.open_orders().len(), 1);
    assert_eq!(m.take_order_changes().len(), 1);
    assert_eq!(m.value(now).unwrap().frozen_cash, d("1001"));
    let event = tick(row, "A", 1, time(row, 580), "10");
    execute(&mut m, &id, 30, &event, "10");
    assert_eq!(m.account().held_quantity(&sec("A")), q(30));
    assert_eq!(m.value(event.key().time_ns).unwrap().cash, d("9699"));
    let repeated = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(100)),
        101,
        event.key().time_ns,
        TimeInForce::Gtc,
    );
    assert!(repeated.unchanged);
    assert_eq!(m.get_order(&id).unwrap().remaining().unwrap(), q(70));
    let at = command(row, "A", 102, event.key().time_ns);
    let cancelled = m.cancel_at(&id, &at).unwrap();
    assert!(cancelled.accepted && !cancelled.unchanged);
    assert_eq!(m.get_order(&id).unwrap().filled_quantity, q(30));
    assert_eq!(m.get_order(&id).unwrap().updated_at, Some(Box::new(at)));
    let after = m.value(event.key().time_ns).unwrap();
    assert_eq!(after.cash, d("9699"));
    assert_eq!(after.frozen_cash, d("0"));
    assert_eq!(after.positions[&sec("A")].cost_basis_total, d("301"));
    assert!(m.cancel_order(&id).unwrap().unchanged);
    assert_eq!(m.value(event.key().time_ns).unwrap(), after);
    assert_eq!(m.account().totals().trading_fees, d("1"));
}

#[test]
fn ordinary_orders_contribute_to_target_and_are_never_cancelled_by_replacement() {
    let row = &rows()[0];
    let now = time(row, 570);
    let mut m = manager("10000", Facts::default());
    let ordinary = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(30)),
        1,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let target = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(100)),
        2,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    assert_eq!(m.get_order(&target).unwrap().quantity, q(70));
    let replace = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(60)),
        3,
        now,
        TimeInForce::Gtc,
    );
    let replacement = replace.order_id.unwrap();
    assert_eq!(m.get_order(&replacement).unwrap().quantity, q(30));
    assert_eq!(m.get_order(&target).unwrap().status, OrderStatus::Cancelled);
    assert_eq!(m.get_order(&ordinary).unwrap().status, OrderStatus::Open);
    let changes = m.take_order_changes();
    assert_eq!(
        changes.iter().map(|o| o.status).collect::<Vec<_>>(),
        vec![
            OrderStatus::Open,
            OrderStatus::Open,
            OrderStatus::Cancelled,
            OrderStatus::Open
        ]
    );
    assert_eq!(m.value(now).unwrap().frozen_cash, d("602"));
    assert!(
        submit(
            &mut m,
            "A",
            IntentValue::TargetQuantity(q(60)),
            4,
            now,
            TimeInForce::Gtc
        )
        .unchanged
    );
}

#[test]
fn replacement_insufficient_cash_reports_reachable_target_and_preserves_old_reservation() {
    let row = &rows()[0];
    let now = time(row, 570);
    let mut m = manager("1001", Facts::default());
    let id = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(100)),
        1,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let before = m.value(now).unwrap();
    let order = m.get_order(&id).unwrap();
    let reserved = m.account().reservation(&id).unwrap();
    let result = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(200)),
        2,
        now,
        TimeInForce::Gtc,
    );
    assert!(!result.accepted);
    assert_eq!(result.reason_code, Some(ErrorCode::InsufficientCash));
    assert_eq!(result.requested_quantity, Some(q(200)));
    assert_eq!(result.effective_quantity, Some(q(100)));
    assert_eq!(m.value(now).unwrap(), before);
    assert_eq!(m.get_order(&id).unwrap(), order);
    assert_eq!(m.account().reservation(&id).unwrap(), reserved);
    assert_eq!(m.take_order_changes().len(), 1);
}

#[test]
fn monetary_target_after_partial_ordinary_and_target_fills_keeps_legal_existing_remainder() {
    let row = &rows()[0];
    for value in [
        IntentValue::TargetValue(d("3010")),
        IntentValue::TargetPercent(d("0.3")),
    ] {
        let mut m = manager(
            "10050",
            Facts {
                unit: 100,
                minimum: 100,
                ..Facts::default()
            },
        );
        let ordinary = submit(
            &mut m,
            "A",
            IntentValue::Quantity(q(100)),
            1,
            time(row, 570),
            TimeInForce::Gtc,
        )
        .order_id
        .unwrap();
        let target = submit(
            &mut m,
            "A",
            value.clone(),
            2,
            time(row, 570),
            TimeInForce::Gtc,
        )
        .order_id
        .unwrap();
        assert_eq!(m.get_order(&target).unwrap().quantity, q(200));
        execute(
            &mut m,
            &ordinary,
            30,
            &tick(row, "A", 1, time(row, 580), "10"),
            "10",
        );
        let event = tick(row, "A", 2, time(row, 590), "10");
        execute(&mut m, &target, 40, &event, "10");
        // Hand oracle: held 70 + ordinary 70 + target 160 = 300.
        // Cash 10050 - (30*10+1) - (40*10+1) = 9348;
        // equity 10048 keeps floor(equity*0.3/10) at 301.
        let before = m.value(event.key().time_ns).unwrap();
        assert_eq!(before.cash, d("9348"));
        assert_eq!(before.total_value, Some(d("10048")));
        assert_eq!(before.frozen_cash, d("2300"));
        let target_before = m.get_order(&target).unwrap();
        let ordinary_before = m.get_order(&ordinary).unwrap();
        let reservation_before = m.account().reservation(&target).unwrap();
        m.take_order_changes();
        for sequence in 3..=102 {
            let repeated = submit(
                &mut m,
                "A",
                value.clone(),
                sequence,
                event.key().time_ns,
                TimeInForce::Gtc,
            );
            assert!(repeated.accepted && repeated.unchanged && repeated.order_id.is_none());
            assert_eq!(repeated.requested_quantity, Some(q(301)));
            assert_eq!(repeated.effective_quantity, Some(q(300)));
        }
        assert_eq!(m.get_order(&target).unwrap(), target_before);
        assert_eq!(m.get_order(&ordinary).unwrap(), ordinary_before);
        assert_eq!(
            m.account().reservation(&target).unwrap(),
            reservation_before
        );
        assert_eq!(m.value(event.key().time_ns).unwrap(), before);
        assert!(m.take_order_changes().is_empty());
        assert_eq!(m.open_orders().len(), 2);

        // Explicit ordinary cancellation changes the projected basis; the
        // old target must then be adjusted, not mistaken for the same goal.
        m.cancel_at(&ordinary, &command(row, "A", 103, event.key().time_ns))
            .unwrap();
        let adjusted = submit(
            &mut m,
            "A",
            value,
            104,
            event.key().time_ns,
            TimeInForce::Gtc,
        );
        assert!(adjusted.accepted && !adjusted.unchanged);
        assert_eq!(adjusted.effective_quantity, Some(q(270)));
        let replacement = m.get_order(&adjusted.order_id.unwrap()).unwrap();
        assert_eq!(replacement.quantity, q(200));
        assert_eq!(m.get_order(&target).unwrap().status, OrderStatus::Cancelled);
        assert_eq!(m.get_order(&target).unwrap().filled_quantity, q(40));
        assert_eq!(m.value(event.key().time_ns).unwrap().cash, d("9348"));
        assert_eq!(m.account().held_quantity(&sec("A")), q(70));
        assert_eq!(m.account().totals().trading_fees, d("2"));
    }
}

#[test]
fn reverse_target_t_plus_failure_keeps_buy_and_later_sell_only_releases_unfilled_part() {
    let calendar = rows();
    let row = &calendar[0];
    let mut m = manager(
        "10000",
        Facts {
            turnover: SellAvailability::NextSession,
            ..Facts::default()
        },
    );
    let id = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(100)),
        1,
        time(row, 570),
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let event = tick(row, "A", 1, time(row, 580), "10");
    execute(&mut m, &id, 30, &event, "10");
    let before = m.value(event.key().time_ns).unwrap();
    let rejected = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(0)),
        2,
        event.key().time_ns,
        TimeInForce::Gtc,
    );
    assert!(!rejected.accepted);
    assert_eq!(rejected.reason_code, Some(ErrorCode::InsufficientSellable));
    assert_eq!(m.value(event.key().time_ns).unwrap(), before);
    assert_eq!(
        m.get_order(&id).unwrap().status,
        OrderStatus::PartiallyFilled
    );
    m.session_end(row).unwrap();
    m.check_session_end(&row.session.key).unwrap();
    m.expire_day(&row.session.key).unwrap();
    m.settle(&calendar[1].session.key).unwrap();
    m.session_start(&calendar[1]).unwrap();
    let reversed = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(0)),
        3,
        calendar[1].before_open_ns,
        TimeInForce::Gtc,
    );
    assert!(reversed.accepted);
    let sell = m.get_order(&reversed.order_id.unwrap()).unwrap();
    assert_eq!((sell.side, sell.quantity), (Side::Sell, q(30)));
    assert_eq!(m.get_order(&id).unwrap().status, OrderStatus::Cancelled);
    assert_eq!(m.account().held_quantity(&sec("A")), q(30));
    assert_eq!(m.value(calendar[1].before_open_ns).unwrap().cash, d("9699"));
}

#[test]
fn pending_sale_is_not_cash_for_another_symbol() {
    let row = &rows()[0];
    let mut m = manager("1001", Facts::default());
    let id = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(100)),
        1,
        time(row, 570),
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let event = tick(row, "A", 1, time(row, 580), "10");
    execute(&mut m, &id, 100, &event, "10");
    assert_eq!(m.value(event.key().time_ns).unwrap().available_cash, d("0"));
    let sale = submit(
        &mut m,
        "A",
        IntentValue::TargetQuantity(q(0)),
        2,
        event.key().time_ns,
        TimeInForce::Gtc,
    );
    assert!(sale.accepted);
    let buy = submit(
        &mut m,
        "B",
        IntentValue::Quantity(q(1)),
        3,
        event.key().time_ns,
        TimeInForce::Gtc,
    );
    assert!(!buy.accepted);
    assert_eq!(buy.reason_code, Some(ErrorCode::InsufficientCash));
    assert_eq!(buy.effective_quantity, Some(q(0)));
    assert_eq!(m.value(event.key().time_ns).unwrap().cash, d("0"));
    assert_eq!(m.account().held_quantity(&sec("A")), q(100));
}

#[test]
fn monetary_and_percent_targets_quantize_original_rational_and_repeat_one_hundred_times() {
    let row = &rows()[0];
    let now = time(row, 570);
    for value in [
        IntentValue::TargetValue(d("1009.99")),
        IntentValue::TargetPercent(d("0.100999")),
    ] {
        let mut m = manager(
            "10000",
            Facts {
                unit: 100,
                minimum: 100,
                ..Facts::default()
            },
        );
        let first = submit(&mut m, "A", value.clone(), 1, now, TimeInForce::Gtc);
        assert!(first.accepted);
        assert_eq!(first.requested_quantity, Some(q(100)));
        assert_eq!(first.effective_quantity, Some(q(100)));
        for n in 2..=100 {
            assert!(submit(&mut m, "A", value.clone(), n, now, TimeInForce::Gtc).unchanged);
        }
        assert_eq!(m.open_orders().len(), 1);
        assert_eq!(m.take_order_changes().len(), 1);
    }
    let mut m = manager(
        "10000",
        Facts {
            unit: 100,
            minimum: 100,
            ..Facts::default()
        },
    );
    let value = submit(
        &mut m,
        "A",
        IntentValue::Value(d("1999.99")),
        1,
        now,
        TimeInForce::Gtc,
    );
    assert_eq!(
        (value.requested_quantity, value.effective_quantity),
        (Some(q(199)), Some(q(100)))
    );
    let illegal = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(199)),
        2,
        now,
        TimeInForce::Gtc,
    );
    assert!(!illegal.accepted);
    assert_eq!(illegal.reason_code, Some(ErrorCode::InvalidOrder));
    assert_eq!(m.open_orders().len(), 1);
    // Independent decimal integer boundary: 3 * 0.666... / 1 is strictly <2.
    assert_eq!(
        ExactDecimal::legal_value_quantity(
            d("3"),
            d("0.6666666666666666666666666666"),
            d("1"),
            QuantityStep::new(1).unwrap()
        )
        .unwrap(),
        q(1)
    );
    assert_eq!(
        ExactDecimal::legal_value_quantity(
            d("3"),
            d("0.6666666666666666666666666667"),
            d("1"),
            QuantityStep::new(1).unwrap()
        )
        .unwrap(),
        q(2)
    );
}

#[test]
fn odd_lot_value_sale_and_zero_target_are_legal_without_selling_frozen_shares() {
    for clear in [false, true] {
        let row = &rows()[0];
        let mut m = manager(
            "10000",
            Facts {
                unit: 100,
                minimum: 100,
                ..Facts::default()
            },
        );
        let id = submit(
            &mut m,
            "A",
            IntentValue::Quantity(q(200)),
            1,
            time(row, 570),
            TimeInForce::Gtc,
        )
        .order_id
        .unwrap();
        let event = tick(row, "A", 1, time(row, 580), "10");
        execute(&mut m, &id, 105, &event, "10");
        m.cancel_at(&id, &command(row, "A", 2, event.key().time_ns))
            .unwrap();
        let target = if clear {
            IntentValue::TargetQuantity(q(0))
        } else {
            IntentValue::TargetValue(d("950"))
        };
        let result = submit(
            &mut m,
            "A",
            target,
            3,
            event.key().time_ns,
            TimeInForce::Gtc,
        );
        assert!(result.accepted);
        let order = m.get_order(&result.order_id.unwrap()).unwrap();
        assert_eq!(
            (order.side, order.quantity),
            (Side::Sell, q(if clear { 105 } else { 5 }))
        );
        assert_eq!(
            result.effective_quantity,
            Some(q(if clear { 0 } else { 100 }))
        );
    }
}

#[test]
fn price_gap_allowance_is_explicit_fees_are_cumulative_and_overdraw_batch_is_atomic() {
    let row = &rows()[0];
    let mut m = manager("2001", Facts::default());
    let id = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(200)),
        1,
        time(row, 570),
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let event = tick(row, "A", 1, time(row, 580), "12");
    let allowance = m
        .allowance(&id, q(200), p("12"), QuantityStep::new(1).unwrap())
        .unwrap();
    assert_eq!(allowance.effective_quantity, q(166));
    assert_eq!(allowance.reason_code, Some(ErrorCode::InsufficientCash));
    let before = m.value(time(row, 570)).unwrap();
    let old = m.get_order(&id).unwrap();
    let claim = m.claim_match(&event, std::slice::from_ref(&old)).unwrap();
    let bad = fill_outcome(&m, &event, &old, 200, "12");
    code(m.commit_match(claim, bad), ErrorCode::InsufficientCash);
    assert_eq!(m.value(time(row, 570)).unwrap(), before);
    assert_eq!(m.get_order(&id).unwrap(), old);
    let outcome = execute(&mut m, &id, 166, &event, "12");
    assert_eq!(
        outcome.orders[0].reason_code,
        Some(ErrorCode::InsufficientCash)
    );
    assert!(outcome.orders[0].message.contains("34"));
    assert_eq!(outcome.fills[0].fee, d("1"));
    assert_eq!(m.value(event.key().time_ns).unwrap().cash, d("8"));
    let later = tick(row, "A", 2, time(row, 590), "0.2");
    let outcome = execute(&mut m, &id, 34, &later, "0.2");
    assert_eq!(outcome.fills[0].fee, d("0"));
    assert_eq!(m.value(later.key().time_ns).unwrap().cash, d("1.2"));
    assert_eq!(m.get_order(&id).unwrap().status, OrderStatus::Filled);
    assert!(m.cancel_order(&id).unwrap().unchanged);
}

#[test]
fn claims_are_single_use_revision_bound_cross_run_safe_and_use_full_same_ns_identity() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    let id = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(100)),
        1,
        time(row, 570),
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let first = tick(row, "A", 1, time(row, 580), "10");
    let second = tick(row, "A", 2, time(row, 580), "10");
    let order = m.get_order(&id).unwrap();
    let stale = m.claim_match(&first, std::slice::from_ref(&order)).unwrap();
    let cross = m.claim_match(&first, std::slice::from_ref(&order)).unwrap();
    let mut other = manager("10000", Facts::default());
    code(
        other.commit_match(cross, fill_outcome(&m, &first, &order, 10, "10")),
        ErrorCode::StaleClaim,
    );
    execute(&mut m, &id, 10, &first, "10");
    code(
        m.commit_match(stale, fill_outcome(&m, &first, &order, 10, "10")),
        ErrorCode::StaleClaim,
    );
    code(m.claim_match(&first, &[]), ErrorCode::StaleClaim);
    // New command after q1 cannot go back to q1, but q2 at the SAME ns is later.
    let new = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(5)),
        2,
        first.key().time_ns,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    execute(&mut m, &new, 5, &second, "10");
    assert_eq!(m.account().held_quantity(&sec("A")), q(15));
    assert_eq!(m.account().totals().trading_fees, d("2"));
    assert_eq!(
        m.get_order(&new).unwrap().eligible_after_event,
        Some(first.key())
    );
}

#[test]
fn claim_after_a_cancel_and_unknown_order_or_forged_trade_ids_cannot_fill() {
    let row = &rows()[0];
    let now = time(row, 570);
    let mut m = manager("10000", Facts::default());
    let id = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(100)),
        1,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let event = tick(row, "A", 1, time(row, 580), "10");
    let order = m.get_order(&id).unwrap();
    let claim = m.claim_match(&event, std::slice::from_ref(&order)).unwrap();
    let outcome = fill_outcome(&m, &event, &order, 10, "10");
    m.cancel_at(&id, &command(row, "A", 2, now)).unwrap();
    code(m.commit_match(claim, outcome), ErrorCode::StaleClaim);
    assert_eq!(m.value(now).unwrap().cash, d("10000"));
    assert_eq!(m.account().held_quantity(&sec("A")), q(0));
    let id = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(100)),
        3,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let order = m.get_order(&id).unwrap();
    let claim = m.claim_match(&event, std::slice::from_ref(&order)).unwrap();
    let mut outcome = fill_outcome(&m, &event, &order, 10, "10");
    outcome.fills[0].trade_id = "unknown-id".into();
    code(m.commit_match(claim, outcome), ErrorCode::InvalidContract);
    let mut unknown = order;
    unknown.order_id = "no-such-order".into();
    code(m.claim_match(&event, &[unknown]), ErrorCode::StaleClaim);
}

#[test]
fn matcher_state_regression_identity_changes_and_late_batch_failure_leave_account_untouched() {
    let row = &rows()[0];
    let now = time(row, 570);
    let mut m = manager("2002", Facts::default());
    let first = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(100)),
        1,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let second = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(100)),
        2,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let event = tick(row, "A", 1, time(row, 580), "20");
    let active = m.active_orders(&sec("A"), 100).unwrap();
    let before = m.value(now).unwrap();
    let mut outcome = fill_outcome(&m, &event, &active[0], 50, "20");
    let mut second_outcome = fill_outcome(&m, &event, &active[1], 100, "20");
    second_outcome.fills[0].trade_id = m.trade_id(1).unwrap();
    outcome.fills.extend(second_outcome.fills);
    outcome.orders.extend(second_outcome.orders);
    let claim = m.claim_match(&event, &active).unwrap();
    code(m.commit_match(claim, outcome), ErrorCode::InsufficientCash);
    assert_eq!(m.value(now).unwrap(), before);
    assert_eq!(m.get_order(&first).unwrap().filled_quantity, q(0));
    assert_eq!(m.get_order(&second).unwrap().filled_quantity, q(0));
    for status in [
        OrderStatus::Filled,
        OrderStatus::Accepted,
        OrderStatus::PartiallyFilled,
    ] {
        let mut changed = active[0].clone();
        changed.status = status;
        let claim = m.claim_match(&event, &active).unwrap();
        code(
            m.commit_match(
                claim,
                MatchOutcome {
                    fills: vec![],
                    orders: vec![changed],
                },
            ),
            ErrorCode::InvalidContract,
        );
    }
    let mut changed = active[0].clone();
    changed.limit_price = Some(p("1"));
    changed.status = OrderStatus::Cancelled;
    changed.reason_code = Some(ErrorCode::Cancelled);
    changed.message = "malicious transition".into();
    let claim = m.claim_match(&event, &active).unwrap();
    code(
        m.commit_match(
            claim,
            MatchOutcome {
                fills: vec![],
                orders: vec![changed],
            },
        ),
        ErrorCode::InvalidContract,
    );
}

#[test]
fn after_close_day_is_next_session_and_gtc_corporate_action_reports_real_cancellations() {
    let calendar = rows();
    let mut m = manager("10000", Facts::default());
    m.add_corporate_action(
        oracle::dividend(&calendar, CashDividendTax::SyntheticFlat { rate: d("0") }),
        calendar[0].before_open_ns,
    )
    .unwrap();
    let old = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(20)),
        1,
        time(&calendar[0], 570),
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    m.session_end(&calendar[0]).unwrap();
    m.check_session_end(&calendar[0].session.key).unwrap();
    assert!(m.expire_day(&calendar[0].session.key).unwrap().is_empty());
    let day = submit(
        &mut m,
        "B",
        IntentValue::Quantity(q(10)),
        2,
        calendar[0].after_close_ns,
        TimeInForce::Day,
    )
    .order_id
    .unwrap();
    assert_eq!(
        m.get_order(&day).unwrap().effective_session,
        calendar[1].session.key
    );
    assert!(m.expire_day(&calendar[0].session.key).unwrap().is_empty());
    m.take_order_changes();
    m.take_corporate_effects();
    m.settle(&calendar[1].session.key).unwrap();
    let cancelled = m.session_start(&calendar[1]).unwrap();
    assert_eq!(cancelled.len(), 1);
    assert_eq!(cancelled[0].order_id, old);
    assert_eq!(cancelled[0].status, OrderStatus::Cancelled);
    assert_eq!(m.get_order(&day).unwrap().status, OrderStatus::Open);
    assert_eq!(
        m.value(calendar[1].before_open_ns).unwrap().frozen_cash,
        d("101")
    );
    assert!(
        m.take_corporate_effects()
            .iter()
            .any(|e| e.effect.kind == CorporateEffectKind::ExDividend)
    );
    m.session_end(&calendar[1]).unwrap();
    let expired = m.expire_day(&calendar[1].session.key).unwrap();
    assert_eq!(expired.len(), 1);
    assert_eq!(expired[0].order_id, day);
    assert_eq!(expired[0].effective_session, calendar[1].session.key);
    assert!(m.cancel_order(&day).unwrap().unchanged);
    assert_eq!(
        m.value(calendar[1].after_close_ns).unwrap().frozen_cash,
        d("0")
    );
}

#[test]
fn index_unknown_permissions_invalid_decimal_range_and_zero_noop_are_explicit() {
    let row = &rows()[0];
    let now = time(row, 570);
    let mut facts = Facts::default();
    facts.symbols.push("INDEX".into());
    let mut m = manager("10000", facts);
    m.set_raw_mark(
        sec("INDEX"),
        raw(row, "10", row.before_open_ns),
        row.before_open_ns,
    )
    .unwrap();
    let index = submit(
        &mut m,
        "INDEX",
        IntentValue::Quantity(q(100)),
        1,
        now,
        TimeInForce::Day,
    );
    assert_eq!(index.reason_code, Some(ErrorCode::InvalidOrder));
    assert!(!index.accepted);
    let zero = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(0)),
        2,
        now,
        TimeInForce::Day,
    );
    assert!(zero.accepted && zero.unchanged && zero.order_id.is_none());
    assert!(m.open_orders().is_empty());
    let huge = submit(
        &mut m,
        "A",
        IntentValue::Value(d("79228162514264337593543950335")),
        3,
        now,
        TimeInForce::Day,
    );
    assert_eq!(huge.reason_code, Some(ErrorCode::NumericRangeUnsupported));
    let mut m = manager(
        "10000",
        Facts {
            permission: None,
            ..Facts::default()
        },
    );
    assert_eq!(
        submit(
            &mut m,
            "A",
            IntentValue::Quantity(q(10)),
            1,
            now,
            TimeInForce::Day
        )
        .reason_code,
        Some(ErrorCode::RuleUnavailable)
    );
    let invalid = submit(
        &mut m,
        "A",
        IntentValue::TargetPercent(d("1.01")),
        2,
        now,
        TimeInForce::Day,
    );
    assert_eq!(invalid.reason_code, Some(ErrorCode::InvalidOrder));
}

#[test]
fn command_sequence_stable_order_priority_notifications_and_terminal_cache_are_bounded() {
    let row = &rows()[0];
    let now = time(row, 570);
    let mut m = manager_with_limits(
        "10000",
        Facts::default(),
        OrderLimits {
            max_active: 2,
            terminal_cache: 1,
            max_changes: 2,
        },
    );
    let b = submit(
        &mut m,
        "B",
        IntentValue::Quantity(q(1)),
        1,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let a = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(1)),
        2,
        now,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    assert_eq!(
        m.open_orders()
            .iter()
            .map(|o| &o.order_id)
            .collect::<Vec<_>>(),
        vec![&b, &a]
    );
    let intent = OrderIntent {
        security: sec("A"),
        side: Side::Buy,
        value: IntentValue::Quantity(q(1)),
        style: OrderStyle::Market,
        tif: TimeInForce::Gtc,
        submitted_at: command(row, "A", 3, now),
    };
    code(m.validate_and_reserve(&intent), ErrorCode::ResourceLimit);
    assert_eq!(m.open_orders().len(), 2);
    m.take_order_changes();
    m.cancel_at(&b, &command(row, "B", 3, now)).unwrap();
    m.take_order_changes();
    m.cancel_at(&a, &command(row, "A", 4, now)).unwrap();
    assert!(m.cancel_order(&a).unwrap().unchanged);
    code(m.get_order(&b), ErrorCode::InvalidOrder);
    assert!(m.cancel_order(&b).unwrap().unchanged);
    assert_eq!(m.value(now).unwrap().cash, d("10000"));
    assert_eq!(m.value(now).unwrap().frozen_cash, d("0"));
}

#[test]
fn unchanged_tick_emits_no_order_or_success_ledger() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(100)),
        1,
        time(row, 570),
        TimeInForce::Gtc,
    );
    m.take_order_changes();
    for n in 1..=100 {
        empty(&mut m, &tick(row, "A", n, time(row, 580), "10"));
    }
    assert!(m.take_order_changes().is_empty());
    assert_eq!(m.open_orders().len(), 1);
    assert_eq!(m.account().totals().trading_fees, d("0"));
}

#[test]
fn unissued_ids_cannot_be_confirmed_by_another_runs_terminal_history() {
    use std::cell::Cell;
    use std::rc::Rc;
    struct WrongRunHistory {
        order: Order,
        calls: Rc<Cell<usize>>,
    }
    impl OrderFacts for WrongRunHistory {
        fn session_terms(&mut self, _row: &CalendarSession) -> QfResult<Vec<AccountTerms>> {
            panic!("no lifecycle work is needed to reject an unissued order")
        }
        fn admission(
            &self,
            _security: &SecurityKey,
            _side: Side,
            _row: &CalendarSession,
            _now: Nanoseconds,
        ) -> QfResult<AdmissionFacts> {
            panic!("no admission work is needed to reject an unissued order")
        }
        fn terminal_order(&self, _id: &str) -> QfResult<Order> {
            self.calls.set(self.calls.get() + 1);
            Ok(self.order.clone())
        }
    }
    let row = &rows()[0];
    let mut source = manager("10000", Facts::default());
    let id = submit(
        &mut source,
        "A",
        IntentValue::Quantity(q(1)),
        1,
        time(row, 570),
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    source
        .cancel_at(&id, &command(row, "A", 2, time(row, 570)))
        .unwrap();
    let calls = Rc::new(Cell::new(0));
    let account = Account::new(d("10000"), rows(), usage(), AccountLimits::default()).unwrap();
    let mut other = OrderManager::new(
        account,
        WrongRunHistory {
            order: source.get_order(&id).unwrap(),
            calls: calls.clone(),
        },
        vec![sec("A")],
        OrderLimits::default(),
    )
    .unwrap();
    // The same ordinal may exist in another run's persisted results. It was
    // never accepted here, so a provider cannot turn it into a successful cancel.
    code(other.get_order(&id), ErrorCode::InvalidOrder);
    code(other.cancel_order(&id), ErrorCode::InvalidOrder);
    assert_eq!(calls.get(), 0);
    assert!(other.open_orders().is_empty());
    assert_eq!(other.value(row.before_open_ns).unwrap().cash, d("10000"));
}

#[test]
fn order_wire_reads_pre_d06_dto_and_preserves_full_change_key_and_corporate_effect() {
    let mut legacy = serde_json::json!({
        "order_id": "qf-order-00000000000000000001", "security": "A", "side": "buy",
        "quantity": 100, "filled_quantity": 30, "status": "partially_filled",
        "submitted_ns": "1767225600000000123", "limit_price": null, "tif": "gtc",
        "effective_session": "S1", "eligible_interval_start": "1767225600000000123",
        "eligible_after_event": null, "reason_code": null, "message": "pre-D06 DTO"
    });
    let old: Order = serde_json::from_value(legacy.clone()).unwrap();
    assert_eq!(old.updated_at, None);
    assert_eq!(old.submitted_ns, ns(1767225600000000123));
    let key = serde_json::json!({
        "time_ns": "1767225600000000123", "phase": "callback", "security": "A",
        "identity": {"source_session": "S1", "channel": "strategy_commands",
                     "sequence": "2", "stable_input_sequence": "2"}
    });
    legacy["updated_at"] = key.clone();
    let current: Order = serde_json::from_value(legacy.clone()).unwrap();
    assert_eq!(serde_json::to_value(current).unwrap(), legacy);
    legacy["quantity"] = serde_json::json!(true);
    assert!(serde_json::from_value::<Order>(legacy).is_err());

    let effect = serde_json::json!({
        "kind": "corporate_action", "record": {
            "time_ns": "1767225600000000123", "session": "S1", "effect": {
                "action_id": "dividend-A", "security": "A", "kind": "paid_dividend",
                "quantity": 100, "cash_delta": "20", "receivable_delta": "-20",
                "tax": "0", "policy": "synthetic fixture"
            }
        }
    });
    let record: qf_core::results::ResultRecord = serde_json::from_value(effect.clone()).unwrap();
    assert_eq!(serde_json::to_value(record).unwrap(), effect);
}

#[test]
fn limit_style_does_not_change_target_market_value_and_invalid_fill_cannot_cross_limit() {
    let row = &rows()[0];
    let now = time(row, 570);
    let mut m = manager("10000", Facts::default());
    let mut intent = OrderIntent {
        security: sec("A"),
        side: Side::Buy,
        value: IntentValue::TargetValue(d("1000")),
        style: OrderStyle::Limit { price: p("8") },
        tif: TimeInForce::Gtc,
        submitted_at: command(row, "A", 1, now),
    };
    let result = m.validate_and_reserve(&intent).unwrap();
    assert!(result.accepted);
    assert_eq!(result.effective_quantity, Some(q(100)));
    let id = result.order_id.unwrap();
    assert_eq!(m.value(now).unwrap().frozen_cash, d("801"));
    let event = tick(row, "A", 1, time(row, 580), "9");
    let order = m.get_order(&id).unwrap();
    let before = m.value(now).unwrap();
    let claim = m.claim_match(&event, std::slice::from_ref(&order)).unwrap();
    code(
        m.commit_match(claim, fill_outcome(&m, &event, &order, 100, "9")),
        ErrorCode::InvalidOrder,
    );
    assert_eq!(m.value(now).unwrap(), before);
    assert_eq!(m.get_order(&id).unwrap(), order);
    intent.style = OrderStyle::Limit { price: p("8.001") };
    intent.submitted_at = command(row, "A", 2, now);
    assert_eq!(
        m.validate_and_reserve(&intent).unwrap().reason_code,
        Some(ErrorCode::InvalidOrder)
    );
    assert_eq!(m.value(now).unwrap(), before);
    assert_eq!(m.get_order(&id).unwrap(), order);
}

#[test]
fn opening_call_market_is_rejected_and_missing_new_day_rule_cancels_gtc_with_reason() {
    let calendar = rows();
    let mut facts = Facts::default();
    facts.missing.insert("S2:A".into());
    let mut m = manager("10000", facts);
    let refused = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(10)),
        1,
        time(&calendar[0], 555),
        TimeInForce::Day,
    );
    assert!(!refused.accepted);
    assert_eq!(refused.reason_code, Some(ErrorCode::InvalidOrder));
    assert!(m.open_orders().is_empty());
    let id = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(10)),
        2,
        time(&calendar[0], 570),
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    m.session_end(&calendar[0]).unwrap();
    m.expire_day(&calendar[0].session.key).unwrap();
    m.take_corporate_effects();
    m.settle(&calendar[1].session.key).unwrap();
    let changes = m.session_start(&calendar[1]).unwrap();
    assert_eq!(changes.len(), 1);
    assert_eq!(changes[0].order_id, id);
    assert_eq!(changes[0].reason_code, Some(ErrorCode::RuleUnavailable));
    assert_eq!(
        m.value(calendar[1].before_open_ns).unwrap().frozen_cash,
        d("0")
    );
}

#[test]
fn dated_risk_buy_cap_counts_real_fills_and_open_orders_even_after_same_day_sale() {
    let row = &rows()[0];
    let mut m = manager(
        "10000",
        Facts {
            risk: RiskState::RiskWarning,
            risk_cap: Some(q(15)),
            ..Facts::default()
        },
    );
    let id = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(10)),
        1,
        time(row, 570),
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    let event = tick(row, "A", 1, time(row, 580), "10");
    execute(&mut m, &id, 6, &event, "10");
    let refused = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(10)),
        2,
        event.key().time_ns,
        TimeInForce::Gtc,
    );
    assert!(!refused.accepted);
    assert_eq!(refused.reason_code, Some(ErrorCode::InvalidOrder));
    let extra = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(5)),
        3,
        event.key().time_ns,
        TimeInForce::Gtc,
    )
    .order_id
    .unwrap();
    m.cancel_at(&id, &command(row, "A", 4, event.key().time_ns))
        .unwrap();
    m.cancel_at(&extra, &command(row, "A", 5, event.key().time_ns))
        .unwrap();
    let sale = m
        .validate_and_reserve(&OrderIntent {
            security: sec("A"),
            side: Side::Sell,
            value: IntentValue::Quantity(q(6)),
            style: OrderStyle::Market,
            tif: TimeInForce::Gtc,
            submitted_at: command(row, "A", 6, event.key().time_ns),
        })
        .unwrap()
        .order_id
        .unwrap();
    let later = tick(row, "A", 2, time(row, 590), "10");
    execute(&mut m, &sale, 6, &later, "10");
    assert_eq!(m.account().held_quantity(&sec("A")), q(0));
    let refused = submit(
        &mut m,
        "A",
        IntentValue::Quantity(q(10)),
        7,
        later.key().time_ns,
        TimeInForce::Gtc,
    );
    assert!(!refused.accepted);
    assert_eq!(refused.reason_code, Some(ErrorCode::InvalidOrder));
}

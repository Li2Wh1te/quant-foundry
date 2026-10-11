use super::support::*;

#[test]
fn repeat_snapshot_cannot_refill_and_new_order_after_first_tick_shares_depletion() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    let mut matcher = ticks(ExecutionModel::QuoteTickV1, "1", "0");
    let first = buy(&mut m, 60, 1, row.before_open_ns);
    let q1 = quote(row, 1, Some("9.99"), Some("10"), Some(100), Some(100));
    let out = tick_commit(&mut m, &mut matcher, &q1, &rules(row, "A"));
    assert_eq!(out.fills[0].quantity, q(60));
    let second = buy(&mut m, 60, 2, q1.key().time_ns);
    let q2 = quote(row, 2, Some("9.99"), Some("10"), Some(100), Some(100));
    let out = tick_commit(&mut m, &mut matcher, &q2, &rules(row, "A"));
    assert_eq!(out.fills[0].quantity, q(40));
    assert_eq!(
        m.get_order(&first.order_id).unwrap().status,
        OrderStatus::Filled
    );
    assert_eq!(
        m.get_order(&second.order_id).unwrap().status,
        OrderStatus::PartiallyFilled
    );
    let q3 = quote(row, 3, Some("9.99"), Some("10"), Some(100), Some(100));
    assert!(
        tick_commit(&mut m, &mut matcher, &q3, &rules(row, "A"))
            .fills
            .is_empty()
    );
    let view = m.account().value(q3.key().time_ns).unwrap();
    assert_eq!(view.positions[&sec("A")].quantity, q(100));
    assert_eq!(view.cash, d("8998")); // two orders, one minimum fee each
}

#[test]
fn both_sides_share_independently_and_opposite_price_quantity_changes_do_not_refill() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    seed(&mut m, 100);
    let mut matcher = ticks(ExecutionModel::QuoteTickV1, "1", "0");
    let now = time(row, 572);
    buy(&mut m, 160, 2, now);
    submit_at(&mut m, "A", Side::Sell, 100, None, TimeInForce::Gtc, 3, now);
    let mut q1 = quote(row, 2, Some("9.99"), Some("10.01"), Some(30), Some(100));
    if let MarketEvent::QuoteTick(q) = &mut q1 {
        q.time_ns = now;
    }
    let out = tick_commit(&mut m, &mut matcher, &q1, &rules(row, "A"));
    assert_eq!(
        out.fills
            .iter()
            .map(|f| (f.side, f.quantity))
            .collect::<Vec<_>>(),
        vec![(Side::Buy, q(100)), (Side::Sell, q(30))]
    );
    let mut q2 = quote(row, 3, Some("9.98"), Some("10.01"), Some(20), Some(100));
    if let MarketEvent::QuoteTick(q) = &mut q2 {
        q.time_ns = now;
    }
    let out = tick_commit(&mut m, &mut matcher, &q2, &rules(row, "A"));
    assert_eq!(out.fills.len(), 1);
    assert_eq!(out.fills[0].side, Side::Sell);
    assert_eq!(out.fills[0].quantity, q(20));
    let mut q3 = quote(row, 4, Some("9.98"), Some("10.02"), Some(20), Some(20));
    if let MarketEvent::QuoteTick(q) = &mut q3 {
        q.time_ns = now;
    }
    let out = tick_commit(&mut m, &mut matcher, &q3, &rules(row, "A"));
    assert_eq!(out.fills.len(), 1);
    assert_eq!(out.fills[0].side, Side::Buy);
    assert_eq!(out.fills[0].quantity, q(20));
}

#[test]
fn quote_increase_is_only_floor_of_added_participation_and_decrease_clips() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    let mut matcher = ticks(ExecutionModel::QuoteTickV1, "0.5", "0");
    buy(&mut m, 30, 1, row.before_open_ns);
    assert_eq!(
        tick_commit(
            &mut m,
            &mut matcher,
            &quote(row, 1, None, Some("10"), None, Some(100)),
            &rules(row, "A")
        )
        .fills[0]
            .quantity,
        q(30)
    );
    // 20 left is clipped to 5 by size 10, then +floor((13-10)*0.5)=1.
    assert!(
        tick_commit(
            &mut m,
            &mut matcher,
            &quote(row, 2, None, Some("10"), None, Some(10)),
            &rules(row, "A")
        )
        .fills
        .is_empty()
    );
    buy(&mut m, 60, 2, print(row, 2, "10", 0).key().time_ns);
    let out = tick_commit(
        &mut m,
        &mut matcher,
        &quote(row, 3, None, Some("10"), None, Some(13)),
        &rules(row, "A"),
    );
    assert_eq!(out.fills[0].quantity, q(6));
    let out = tick_commit(
        &mut m,
        &mut matcher,
        &quote(row, 4, None, Some("10"), None, Some(14)),
        &rules(row, "A"),
    );
    assert!(out.fills.is_empty()); // sub-share added participation is not rounded up
}

#[test]
fn missing_side_quantity_never_matches_or_resets_depletion_and_zero_is_observed() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    let mut matcher = ticks(ExecutionModel::QuoteTickV1, "1", "0");
    buy(&mut m, 200, 1, row.before_open_ns);
    let events = [
        quote(row, 1, Some("9"), Some("10"), Some(100), None),
        quote(row, 2, Some("9"), Some("10"), Some(100), Some(100)),
        quote(row, 3, Some("9"), Some("10"), Some(100), None),
        quote(row, 4, None, None, None, None),
        quote(row, 5, Some("9"), Some("10"), Some(100), Some(100)),
        quote(row, 6, Some("9"), Some("10"), Some(100), Some(0)),
        quote(row, 7, Some("9"), Some("10"), Some(100), Some(20)),
    ];
    let fills: Vec<i64> = events
        .iter()
        .map(|e| {
            tick_commit(&mut m, &mut matcher, e, &rules(row, "A"))
                .fills
                .iter()
                .map(|f| f.quantity.get())
                .sum()
        })
        .collect();
    assert_eq!(fills, vec![0, 100, 0, 0, 0, 0, 20]);
}

#[test]
fn print_budget_shared_between_orders_and_new_same_value_print_is_real_liquidity() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    let mut matcher = ticks(ExecutionModel::TradeTickV1, "1", "0");
    buy(&mut m, 60, 1, row.before_open_ns);
    buy(&mut m, 60, 2, row.before_open_ns);
    let out = tick_commit(
        &mut m,
        &mut matcher,
        &print(row, 1, "10", 100),
        &rules(row, "A"),
    );
    assert_eq!(
        out.fills
            .iter()
            .map(|f| f.quantity.get())
            .collect::<Vec<_>>(),
        vec![60, 40]
    );
    let out = tick_commit(
        &mut m,
        &mut matcher,
        &print(row, 2, "10", 100),
        &rules(row, "A"),
    );
    assert_eq!(out.fills[0].quantity, q(20));
    assert_eq!(out.fills[0].fee, d("0"));
    assert_eq!(
        m.account()
            .value(out.fills[0].execution_time_ns)
            .unwrap()
            .cash,
        d("8798")
    );
}

#[test]
fn merged_stream_uses_only_selected_basis_and_print_does_not_refresh_quote() {
    let row = &rows()[0];
    for model in [ExecutionModel::TradeTickV1, ExecutionModel::QuoteTickV1] {
        let mut m = manager("10000", Facts::default());
        let mut matcher = ticks(model, "1", "0");
        buy(&mut m, 500, 1, row.before_open_ns);
        let events = [
            quote(row, 1, None, Some("10"), None, Some(100)),
            print(row, 2, "10", 200),
            quote(row, 3, None, Some("10"), None, Some(100)),
        ];
        let fill_quantities: Vec<i64> = events
            .iter()
            .map(|e| {
                tick_commit(&mut m, &mut matcher, e, &rules(row, "A"))
                    .fills
                    .iter()
                    .map(|f| f.quantity.get())
                    .sum()
            })
            .collect();
        assert_eq!(
            fill_quantities,
            if model == ExecutionModel::TradeTickV1 {
                vec![0, 200, 0]
            } else {
                vec![100, 0, 0]
            }
        );
    }
}

#[test]
fn real_cumulative_account_budget_at_gap_prices_and_minimum_commission() {
    let row = &rows()[0];
    let mut m = manager("203", Facts::default());
    let mut matcher = ticks(ExecutionModel::TradeTickV1, "1", "0");
    buy(&mut m, 10, 1, row.before_open_ns);
    buy(&mut m, 10, 2, row.before_open_ns);
    let out = tick_commit(
        &mut m,
        &mut matcher,
        &print(row, 1, "20", 100),
        &rules(row, "A"),
    );
    assert_eq!(
        out.fills
            .iter()
            .map(|f| f.quantity.get())
            .collect::<Vec<_>>(),
        vec![5, 5]
    );
    let view = m.account().value(out.fills[0].execution_time_ns).unwrap();
    assert_eq!(view.cash, d("1"));
    assert_eq!(view.positions[&sec("A")].quantity, q(10));
}

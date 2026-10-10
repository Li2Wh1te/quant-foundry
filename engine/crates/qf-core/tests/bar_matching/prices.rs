use super::support::*;

#[test]
fn independent_price_table_uses_real_orders_accounts_and_one_rational_tick_rounding() {
    for case in oracles()["price_cases"].as_array().unwrap() {
        let row = &rows()[0];
        let mut m = manager("10000", Facts::default());
        let side = if case["side"] == "buy" {
            Side::Buy
        } else {
            seed(&mut m, 20);
            Side::Sell
        };
        let start = if side == Side::Buy { 570 } else { 571 };
        let now = if side == Side::Buy {
            row.before_open_ns
        } else {
            time(row, start)
        };
        let order = submit_at(
            &mut m,
            "A",
            side,
            10,
            case["limit"].as_str(),
            TimeInForce::Gtc,
            2,
            now,
        );
        let prices = std::array::from_fn(|i| case["ohlc"][i].as_str().unwrap());
        let event = bar(row, "A", start, start + 1, 2, prices, Some(100));
        let mut facts = day_facts(row, "A", TradingStatus::Trading);
        if let DailyPriceLimit::Limited { lower, .. } = &mut facts.price_limit {
            *lower = p(case["tick"].as_str().unwrap());
        }
        let rules = rules_with(row, "A", case["tick"].as_str().unwrap(), facts).unwrap();
        let outcome = commit(
            &mut m,
            &mut matcher("1", case["bps"].as_str().unwrap()),
            &event,
            &rules,
        );
        match case["expected"].as_str() {
            Some(expected) => {
                assert_eq!(outcome.fills.len(), 1, "{case}");
                let fill = &outcome.fills[0];
                assert_eq!(fill.price, p(expected), "{case}");
                assert_eq!(fill.execution_time_ns, time(row, start + 1));
                assert_eq!(fill.fee, d("1"));
                assert_eq!(
                    m.get_order(&order.order_id).unwrap().status,
                    OrderStatus::Filled
                );
            }
            None => {
                assert!(outcome.fills.is_empty(), "{case}");
                assert!(outcome.orders[0].message.contains("未触及"));
            }
        }
    }
}

#[test]
fn interval_qualification_refuses_started_bar_even_with_later_event_identity() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    let order = submit_at(
        &mut m,
        "A",
        Side::Buy,
        10,
        None,
        TimeInForce::Gtc,
        1,
        time(row, 600),
    );
    let mut matcher = matcher("1", "0");
    let event = bar(row, "A", 570, 630, 1, ["10", "12", "9", "10"], Some(100));
    let outcome = matcher
        .consume_with_budget(&event, std::slice::from_ref(&order), &rules(row, "A"), &m)
        .unwrap();
    assert!(outcome.fills.is_empty() && outcome.orders.is_empty());
    let event = bar(row, "A", 630, 690, 2, ["11", "12", "9", "11"], Some(100));
    let result = commit(&mut m, &mut matcher, &event, &rules(row, "A"));
    assert_eq!(result.fills[0].price, p("11"));
    assert_eq!(result.fills[0].execution_time_ns, time(row, 690));
    // A callback at the prior close is allowed into a Bar that starts exactly
    // then; an eligibility boundary later than that start is still excluded.
    let mut changed = order;
    changed.submitted_ns = time(row, 630);
    changed.eligible_interval_start = Some(time(row, 631));
    let outcome = super::support::matcher("1", "0")
        .consume_with_budget(&event, &[changed], &rules(row, "A"), &m)
        .unwrap();
    assert!(outcome.fills.is_empty());
}

#[test]
fn missing_prices_invalid_ohlc_off_grid_and_out_of_band_are_rejected() {
    let row = &rows()[0];
    let m = manager("10000", Facts::default());
    let event = bar(row, "A", 570, 571, 1, ["10", "12", "9", "10"], Some(100));
    let mut json = serde_json::to_value(&event).unwrap();
    json["event"].as_object_mut().unwrap().remove("open");
    assert!(serde_json::from_value::<MarketEvent>(json).is_err());
    for prices in [["10", "9", "8", "10"], ["10.001", "12", "9", "10"]] {
        let event = bar(row, "A", 570, 571, 1, prices, Some(100));
        assert!(
            matcher("1", "0")
                .consume_with_budget(&event, &[], &rules(row, "A"), &m)
                .is_err()
        );
    }
    let mut facts = day_facts(row, "A", TradingStatus::Trading);
    facts.price_limit = DailyPriceLimit::Limited {
        rate: d("0.1"),
        lower: p("9"),
        upper: p("11"),
    };
    code(
        matcher("1", "0").consume_with_budget(
            &event,
            &[],
            &rules_with(row, "A", "0.01", facts).unwrap(),
            &m,
        ),
        ErrorCode::InvalidOrder,
    );
}

#[test]
fn normalized_config_is_the_only_parameter_entry_and_tick_source_is_refused() {
    let config = config(Frequency::Hour, "0.33", "17", "10000");
    let mut matcher = BarMatcher::from_run(&config).unwrap();
    let description = matcher.model_description().unwrap();
    assert_eq!(description.participation_rate, config.participation_rate);
    assert_eq!(description.slippage_bps, config.slippage_bps);
    assert_eq!(description.frequency, config.frequency);
    let row = &rows()[0];
    let m = manager("10000", Facts::default());
    let event = bar(row, "A", 570, 630, 1, ["10", "12", "9", "10"], Some(100));
    code(
        matcher.consume(&event, &[], &rules(row, "A")),
        ErrorCode::CapabilityUnavailable,
    );
    code(
        matcher.consume_with_budget(
            &tick(row, "A", 1, time(row, 570), "10"),
            &[],
            &rules(row, "A"),
            &m,
        ),
        ErrorCode::InvalidContract,
    );
}

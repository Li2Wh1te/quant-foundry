use super::support::*;
use qf_core::matching::FillAllowance;

#[test]
fn two_orders_share_one_floor_volume_budget_and_real_account_fees() {
    let expected = &oracles()["shared_volume"];
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    for (i, qty) in expected["orders"].as_array().unwrap().iter().enumerate() {
        submit_at(
            &mut m,
            "A",
            Side::Buy,
            qty.as_i64().unwrap(),
            None,
            TimeInForce::Gtc,
            i as u64 + 1,
            row.before_open_ns,
        );
    }
    let event = bar(row, "A", 570, 571, 1, ["10", "12", "9", "10"], Some(101));
    let result = commit(&mut m, &mut matcher("0.5", "0"), &event, &rules(row, "A"));
    assert_eq!(
        result
            .fills
            .iter()
            .map(|f| f.quantity.get())
            .collect::<Vec<_>>(),
        vec![30, 20]
    );
    assert_eq!(
        result.fills.iter().map(|f| f.fee).collect::<Vec<_>>(),
        vec![d("1"), d("1")]
    );
    assert_eq!(
        m.value(time(row, 571)).unwrap().cash,
        d(expected["cash"].as_str().unwrap())
    );
    assert_eq!(m.account().held_quantity(&sec("A")), q(50));
    assert_eq!(m.open_orders()[0].remaining().unwrap(), q(10));
}

#[test]
fn cumulative_gap_candidates_use_prior_fills_instead_of_reusing_cash() {
    let expected = &oracles()["gap_budget"];
    let row = &rows()[0];
    let mut m = manager(expected["initial_cash"].as_str().unwrap(), Facts::default());
    for seq in 1..=2 {
        submit_at(
            &mut m,
            "A",
            Side::Buy,
            10,
            None,
            TimeInForce::Gtc,
            seq,
            row.before_open_ns,
        );
    }
    let event = bar(row, "A", 570, 571, 1, ["20", "21", "19", "20"], Some(100));
    let result = commit(&mut m, &mut matcher("1", "0"), &event, &rules(row, "A"));
    assert_eq!(
        result.fills.iter().map(|f| f.quantity).collect::<Vec<_>>(),
        vec![q(10), q(7)]
    );
    assert_eq!(m.account().held_quantity(&sec("A")), q(17));
    assert_eq!(m.value(time(row, 571)).unwrap().cash, d("8"));
    assert_eq!(m.account().totals().trading_fees, d("2"));
    assert_eq!(
        m.open_orders()[0].reason_code,
        Some(ErrorCode::InsufficientCash)
    );
}

#[test]
fn partial_fills_charge_minimum_once_and_accepted_lots_can_fill_individual_shares() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    submit_at(
        &mut m,
        "A",
        Side::Buy,
        5,
        None,
        TimeInForce::Gtc,
        1,
        row.before_open_ns,
    );
    let mut matcher = matcher("0.1", "0");
    let mut fills = vec![];
    for seq in 1..=3 {
        fills.extend(
            commit(
                &mut m,
                &mut matcher,
                &bar(
                    row,
                    "A",
                    569 + seq,
                    570 + seq,
                    seq as u64,
                    ["10", "12", "9", "10"],
                    Some(20),
                ),
                &rules(row, "A"),
            )
            .fills,
        );
    }
    assert_eq!(
        fills.iter().map(|f| f.quantity).collect::<Vec<_>>(),
        vec![q(2), q(2), q(1)]
    );
    assert_eq!(
        fills.iter().map(|f| f.fee).collect::<Vec<_>>(),
        vec![d("1"), d("0"), d("0")]
    );
    assert_eq!(
        m.value(time(row, 573)).unwrap().cash,
        d(oracles()["partial_minimum"]["cash"].as_str().unwrap())
    );

    let mut m = manager(
        "10000",
        Facts {
            unit: 100,
            minimum: 100,
            ..Facts::default()
        },
    );
    submit_at(
        &mut m,
        "A",
        Side::Buy,
        200,
        None,
        TimeInForce::Gtc,
        1,
        row.before_open_ns,
    );
    let mut matcher = super::support::matcher("1", "0");
    let first = commit(
        &mut m,
        &mut matcher,
        &bar(row, "A", 570, 571, 1, ["10", "12", "9", "10"], Some(137)),
        &rules(row, "A"),
    );
    let last = commit(
        &mut m,
        &mut matcher,
        &bar(row, "A", 571, 572, 2, ["10", "12", "9", "10"], Some(100)),
        &rules(row, "A"),
    );
    assert_eq!(first.fills[0].quantity, q(137));
    assert_eq!(last.fills[0].quantity, q(63));
    assert_eq!(last.fills[0].fee, d("0"));
    assert!(m.open_orders().is_empty());
}

#[test]
fn buy_and_sell_orders_share_volume_in_real_stable_order_priority() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    seed(&mut m, 10);
    submit_at(
        &mut m,
        "A",
        Side::Sell,
        6,
        None,
        TimeInForce::Gtc,
        2,
        time(row, 571),
    );
    submit_at(
        &mut m,
        "A",
        Side::Buy,
        6,
        None,
        TimeInForce::Gtc,
        3,
        time(row, 571),
    );
    let result = commit(
        &mut m,
        &mut matcher("1", "0"),
        &bar(row, "A", 571, 572, 2, ["10", "12", "9", "10"], Some(8)),
        &rules(row, "A"),
    );
    assert_eq!(
        result
            .fills
            .iter()
            .map(|f| (f.side, f.quantity))
            .collect::<Vec<_>>(),
        vec![(Side::Sell, q(6)), (Side::Buy, q(2))]
    );
    assert_eq!(
        m.value(time(row, 572)).unwrap().cash,
        d(oracles()["mixed_sides"]["cash"].as_str().unwrap())
    );
    assert_eq!(m.account().held_quantity(&sec("A")), q(6));
    assert_eq!(m.account().totals().trading_fees, d("3"));
}

#[test]
fn missing_volume_rules_status_and_band_do_not_become_liquidity() {
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
        row.before_open_ns,
    );
    let mut matcher = matcher("1", "0");
    let missing = bar(row, "A", 570, 571, 1, ["10", "12", "9", "10"], None);
    code(
        matcher.consume_with_budget(&missing, std::slice::from_ref(&order), &rules(row, "A"), &m),
        ErrorCode::RuleUnavailable,
    );
    assert_eq!(m.account().totals().trading_fees, d("0"));
    assert_eq!(m.get_order(&order.order_id).unwrap(), order);
    for status in [TradingStatus::Trading, TradingStatus::Unknown] {
        let mut facts = day_facts(row, "A", status);
        if status == TradingStatus::Trading {
            facts.price_limit = DailyPriceLimit::Unknown;
        }
        code(
            rules_with(row, "A", "0.01", facts),
            ErrorCode::RuleUnavailable,
        );
    }
    code(
        BarRules::new(
            &RuleBook::new(vec![]).unwrap(),
            Facts::instrument("A"),
            day_facts(row, "A", TradingStatus::Trading),
            usage(),
        ),
        ErrorCode::RuleUnavailable,
    );
    let result = commit(
        &mut m,
        &mut matcher,
        &bar(row, "A", 570, 571, 1, ["10", "12", "9", "10"], Some(100)),
        &rules(row, "A"),
    );
    assert_eq!(result.fills[0].quantity, q(10)); // Failed candidate did not consume the interval.
}

#[test]
fn zero_volume_and_halt_keep_orders_and_repeated_reasons_emit_no_ledger() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    submit_at(
        &mut m,
        "A",
        Side::Buy,
        10,
        None,
        TimeInForce::Gtc,
        1,
        row.before_open_ns,
    );
    let mut matcher = matcher("1", "0");
    for seq in 1..=2 {
        let result = commit(
            &mut m,
            &mut matcher,
            &bar(
                row,
                "A",
                569 + seq,
                570 + seq,
                seq as u64,
                ["10", "12", "9", "10"],
                Some(0),
            ),
            &rules(row, "A"),
        );
        assert!(result.fills.is_empty());
        assert_eq!(result.orders.len(), if seq == 1 { 1 } else { 0 });
    }
    let halt = rules_with(row, "A", "0.01", day_facts(row, "A", TradingStatus::Halted)).unwrap();
    let result = commit(
        &mut m,
        &mut matcher,
        &bar(row, "A", 572, 573, 3, ["10", "12", "9", "10"], Some(100)),
        &halt,
    );
    assert!(result.fills.is_empty());
    assert!(result.orders[0].message.contains("停牌"));
    assert_eq!(m.value(time(row, 573)).unwrap().cash, d("10000"));
}

#[test]
fn partial_order_reason_updates_do_not_panic_or_recharge_fees() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    submit_at(
        &mut m,
        "A",
        Side::Buy,
        10,
        Some("10"),
        TimeInForce::Gtc,
        1,
        row.before_open_ns,
    );
    let mut matcher = matcher("1", "0");
    let first = commit(
        &mut m,
        &mut matcher,
        &bar(row, "A", 570, 571, 1, ["10", "12", "9", "10"], Some(2)),
        &rules(row, "A"),
    );
    assert_eq!(first.fills[0].quantity, q(2));
    let second = commit(
        &mut m,
        &mut matcher,
        &bar(
            row,
            "A",
            571,
            572,
            2,
            ["11", "12", "10.01", "11"],
            Some(100),
        ),
        &rules(row, "A"),
    );
    assert!(second.fills.is_empty());
    assert_eq!(second.orders[0].status, OrderStatus::PartiallyFilled);
    assert!(second.orders[0].message.contains("未触及"));
    assert_eq!(m.account().totals().trading_fees, d("1"));
    assert_eq!(m.open_orders()[0].remaining().unwrap(), q(8));
}

#[test]
fn price_limit_directions_and_one_price_bars_have_no_queue_guarantee() {
    let row = &rows()[0];
    for (side, price, expected) in [
        (Side::Buy, "11", 0),
        (Side::Sell, "11", 10),
        (Side::Buy, "9", 10),
        (Side::Sell, "9", 0),
    ] {
        let mut m = manager("10000", Facts::default());
        let (start, now) = if side == Side::Sell {
            seed(&mut m, 10);
            (571, time(row, 571))
        } else {
            (570, row.before_open_ns)
        };
        submit_at(&mut m, "A", side, 10, None, TimeInForce::Gtc, 2, now);
        let mut facts = day_facts(row, "A", TradingStatus::Trading);
        facts.price_limit = DailyPriceLimit::Limited {
            rate: d("0.1"),
            lower: p("9"),
            upper: p("11"),
        };
        let result = commit(
            &mut m,
            &mut matcher("1", "0"),
            &bar(row, "A", start, start + 1, 2, [price; 4], Some(100)),
            &rules_with(row, "A", "0.01", facts).unwrap(),
        );
        assert_eq!(
            result.fills.iter().map(|f| f.quantity.get()).sum::<i64>(),
            expected
        );
        if expected == 0 {
            assert!(result.orders[0].message.contains("队列"));
        }
    }
    let mut m = manager("10000", Facts::default());
    submit_at(
        &mut m,
        "A",
        Side::Buy,
        10,
        None,
        TimeInForce::Gtc,
        1,
        row.before_open_ns,
    );
    let mut facts = day_facts(row, "A", TradingStatus::Trading);
    facts.price_limit = DailyPriceLimit::Limited {
        rate: d("0.1"),
        lower: p("9"),
        upper: p("11"),
    };
    let rules = rules_with(row, "A", "0.01", facts).unwrap();
    let result = commit(
        &mut m,
        &mut matcher("1", "1000"),
        &bar(row, "A", 570, 571, 1, ["10", "11", "9", "10"], Some(100)),
        &rules,
    );
    assert!(result.fills.is_empty()); // Adverse tick/cap cannot bypass the limit queue.
}

#[test]
fn participation_uses_original_rational_and_duplicate_bars_cannot_refresh_volume() {
    let row = &rows()[0];
    let mut m = manager("10000", Facts::default());
    submit_at(
        &mut m,
        "A",
        Side::Buy,
        10,
        None,
        TimeInForce::Gtc,
        1,
        row.before_open_ns,
    );
    let mut matcher = matcher("0.6666666666666666666666666666", "0");
    let event = bar(row, "A", 570, 571, 1, ["10", "12", "9", "10"], Some(3));
    assert_eq!(
        commit(&mut m, &mut matcher, &event, &rules(row, "A")).fills[0].quantity,
        q(1)
    );
    let duplicate = bar(row, "A", 570, 571, 2, ["10", "12", "9", "10"], Some(3));
    code(
        matcher.consume_with_budget(&duplicate, &m.open_orders(), &rules(row, "A"), &m),
        ErrorCode::InvalidContract,
    );
    assert_eq!(m.account().held_quantity(&sec("A")), q(1));
}

struct FailLater<'a>(&'a Manager);
impl FillBudget for FailLater<'_> {
    fn allowance(
        &self,
        id: &str,
        qty: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        self.0.allowance(id, qty, price, step)
    }
    fn allowance_after(
        &self,
        prior: &[Fill],
        id: &str,
        qty: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        if !prior.is_empty() {
            return Err(QfError::new(
                ErrorCode::RuleUnavailable,
                "synthetic_late_failure",
                "合成后序候选拒绝",
            ));
        }
        self.0.allowance_after(prior, id, qty, price, step)
    }
    fn trade_id(&self, offset: usize) -> QfResult<String> {
        self.0.trade_id(offset)
    }
}

#[test]
fn late_candidate_or_commit_failure_leaves_all_orders_cash_and_fee_accumulators_unchanged() {
    let row = &rows()[0];
    let mut m = manager("350", Facts::default());
    for seq in 1..=2 {
        submit_at(
            &mut m,
            "A",
            Side::Buy,
            10,
            None,
            TimeInForce::Gtc,
            seq,
            row.before_open_ns,
        );
    }
    let before = m.value(row.before_open_ns).unwrap();
    let orders = m.open_orders();
    let event = bar(row, "A", 570, 571, 1, ["20", "21", "19", "20"], Some(100));
    let mut matcher = matcher("1", "0");
    code(
        matcher.consume_with_budget(&event, &orders, &rules(row, "A"), &FailLater(&m)),
        ErrorCode::RuleUnavailable,
    );
    assert_eq!(m.value(row.before_open_ns).unwrap(), before);
    assert_eq!(m.open_orders(), orders);
    assert_eq!(m.account().totals().trading_fees, d("0"));
    let claim = m.claim_match(&event, &orders).unwrap();
    let mut outcome = matcher
        .consume_with_budget(&event, &orders, &rules(row, "A"), &m)
        .unwrap();
    outcome.fills[1].quantity = q(8);
    outcome.orders[1].filled_quantity = q(8);
    code(m.commit_match(claim, outcome), ErrorCode::InsufficientCash);
    assert_eq!(m.value(row.before_open_ns).unwrap(), before);
    assert_eq!(m.open_orders(), orders);
    assert_eq!(m.account().totals().trading_fees, d("0"));
    assert_eq!(m.trade_id(0).unwrap(), "qf-trade-00000000000000000001");
}

// The matcher does not infer stock ticks/lots for ETFs. This explicit dated
// fixture exercises the real D02/D06/D09 path with an ETF milli-CNY price tick.
#[derive(Clone)]
struct EtfFacts;
impl EtfFacts {
    fn instrument() -> Instrument {
        Instrument {
            security: sec("A"),
            exchange: Exchange::Shanghai,
            product: Product::BondEtf,
        }
    }
    fn book(row: &CalendarSession) -> RuleBook {
        let stock = book("0.01");
        let instrument = Facts::instrument("A");
        let facts = day_facts(row, "A", TradingStatus::Trading);
        let mut rule = stock
            .resolve(&instrument, &facts, &usage())
            .unwrap()
            .rule()
            .clone();
        rule.product = Product::BondEtf;
        rule.price_tick = p("0.001");
        rule.minimum_buy = q(100);
        rule.buy_step = QuantityStep::new(100).unwrap();
        RuleBook::new(vec![rule]).unwrap()
    }
    fn facts(row: &CalendarSession) -> TradeDayFacts {
        oracle::facts(row, &Self::instrument(), TradingStatus::Trading)
    }
    fn terms(row: &CalendarSession) -> AccountTerms {
        let instrument = Self::instrument();
        let scope = |side| qf_core::rules::fees::FeeScope {
            instrument: instrument.clone(),
            side,
            investor: qf_core::rules::fees::InvestorKind::ResidentIndividual,
            origin: origin(),
        };
        AccountTerms::resolve(
            instrument.clone(),
            &Self::book(row),
            Self::facts(row),
            oracle::fee(scope(Side::Buy), "0", "1", vec![]),
            oracle::fee(scope(Side::Sell), "0", "1", vec![]),
            SaleProceedsRule {
                timing: SaleProceedsTiming::Immediate,
                basis: "synthetic explicit ETF sale cash".into(),
                origin: origin(),
            },
            usage(),
        )
        .unwrap()
    }
}
impl OrderFacts for EtfFacts {
    fn session_terms(&mut self, row: &CalendarSession) -> QfResult<Vec<AccountTerms>> {
        Ok(vec![Self::terms(row)])
    }
    fn admission(
        &self,
        _security: &SecurityKey,
        _side: Side,
        _row: &CalendarSession,
        _now: Nanoseconds,
    ) -> QfResult<AdmissionFacts> {
        Ok(AdmissionFacts {
            instrument: Self::instrument(),
            submission_reference: None,
            permissions: TradePermissions {
                buy: Some(true),
                sell: Some(true),
                product_buy: Some(true),
                risk_disclosure: Some(true),
                delisting_buy: Some(true),
                risk_bought_or_open: Some(q(0)),
            },
        })
    }
}
#[test]
fn etf_uses_its_dated_tick_and_admission_lot_with_real_integer_partial_fill() {
    let calendar = rows();
    let row = &calendar[0];
    let account = Account::new(
        d("1000"),
        calendar.clone(),
        usage(),
        AccountLimits::default(),
    )
    .unwrap();
    let mut manager =
        OrderManager::new(account, EtfFacts, vec![sec("A")], OrderLimits::default()).unwrap();
    manager.settle(&row.session.key).unwrap();
    manager.session_start(row).unwrap();
    manager
        .set_raw_mark(
            sec("A"),
            raw(row, "1.234", row.before_open_ns),
            row.before_open_ns,
        )
        .unwrap();
    let submitted = manager
        .validate_and_reserve(&OrderIntent {
            security: sec("A"),
            side: Side::Buy,
            value: IntentValue::Quantity(q(100)),
            style: OrderStyle::Market,
            tif: TimeInForce::Gtc,
            submitted_at: command(row, "A", 1, row.before_open_ns),
        })
        .unwrap();
    assert!(submitted.accepted);
    let event = bar(
        row,
        "A",
        570,
        571,
        1,
        ["1.234", "1.236", "1.232", "1.234"],
        Some(150),
    );
    let orders = manager.open_orders();
    let claim = manager.claim_match(&event, &orders).unwrap();
    let rules = BarRules::new(
        &EtfFacts::book(row),
        EtfFacts::instrument(),
        EtfFacts::facts(row),
        usage(),
    )
    .unwrap();
    let outcome = matcher("0.1", "0")
        .consume_with_budget(&event, &orders, &rules, &manager)
        .unwrap();
    let result = manager.commit_match(claim, outcome).unwrap();
    manager.mark(&event).unwrap();
    assert_eq!(result.fills[0].quantity, q(15));
    assert_eq!(result.fills[0].price, p("1.234"));
    assert_eq!(result.fills[0].fee, d("1"));
    assert_eq!(manager.value(time(row, 571)).unwrap().cash, d("980.49"));
    assert_eq!(manager.open_orders()[0].remaining().unwrap(), q(85));
}

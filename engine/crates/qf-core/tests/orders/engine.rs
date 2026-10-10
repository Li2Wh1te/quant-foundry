// D03 driver + real D06/D09/D02. These explicit fill plans are ONLY isolated
// seam inputs, not a D07/D08 matcher or production market data integration.
use super::support::*;
#[path = "../time/support.rs"]
mod isolated;
use qf_core::clock::{Cadence, CallbackId, ScheduleTime, SessionCalendar};
use qf_core::engine::*;
use qf_core::matching::{FillBudget, Matcher};
use qf_core::results::ResultRecord;
use qf_core::run::{Frequency, RunOutcome};
use std::cell::Cell;
use std::rc::Rc;

struct Plans {
    cap: i64,
    seen: Vec<(EventKey, Vec<String>)>,
}
impl Matcher for Plans {
    type Rules = &'static str;
    fn consume(
        &mut self,
        _event: &MarketEvent,
        _orders: &[Order],
        _rules: &Self::Rules,
    ) -> QfResult<MatchOutcome> {
        panic!("real D06 budget seam must be passed by D03")
    }
    fn consume_with_budget(
        &mut self,
        event: &MarketEvent,
        orders: &[Order],
        _rules: &Self::Rules,
        budget: &dyn FillBudget,
    ) -> QfResult<MatchOutcome> {
        self.seen.push((
            event.key(),
            orders.iter().map(|o| o.order_id.clone()).collect(),
        ));
        let price = match event {
            MarketEvent::Bar(b) => b.open,
            MarketEvent::TradeTick(t) => t.price,
            MarketEvent::QuoteTick(q) => q.ask.unwrap(),
        };
        let mut outcome = MatchOutcome {
            fills: vec![],
            orders: vec![],
        };
        for order in orders {
            let requested = order.remaining()?.min(q(self.cap));
            if requested == q(0) {
                continue;
            }
            let allowance = budget.allowance_after(
                &outcome.fills,
                &order.order_id,
                requested,
                price,
                QuantityStep::new(1)?,
            )?;
            if allowance.effective_quantity == q(0) {
                continue;
            }
            outcome.fills.push(Fill {
                trade_id: budget.trade_id(outcome.fills.len())?,
                order_id: order.order_id.clone(),
                security: order.security.clone(),
                side: order.side,
                quantity: allowance.effective_quantity,
                price,
                fee: d("0"),
                execution_time_ns: event.key().time_ns,
            });
            let mut changed = order.clone();
            changed.filled_quantity = changed
                .filled_quantity
                .checked_add(allowance.effective_quantity)?;
            changed.status = if changed.filled_quantity == changed.quantity {
                OrderStatus::Filled
            } else {
                OrderStatus::PartiallyFilled
            };
            changed.reason_code = allowance.reason_code;
            changed.message = allowance.message;
            outcome.orders.push(changed);
        }
        Ok(outcome)
    }
}
struct Harness {
    execution: Manager,
    matcher: Plans,
    host: isolated::Host,
    writer: isolated::Writer,
    data: isolated::Data,
    control: isolated::Control,
}
impl Harness {
    fn new(cash: &str) -> Self {
        Self::with_limits(cash, OrderLimits::default())
    }
    fn with_limits(cash: &str, limits: OrderLimits) -> Self {
        let facts = Facts::default();
        let calendar = rows();
        let mut account =
            Account::new(d(cash), calendar.clone(), usage(), AccountLimits::default()).unwrap();
        for symbol in ["A", "B"] {
            account
                .install_terms(facts.terms(&calendar[0], symbol).unwrap())
                .unwrap();
            account
                .set_raw_mark(
                    sec(symbol),
                    raw(&calendar[0], "10", calendar[0].before_open_ns),
                    calendar[0].before_open_ns,
                )
                .unwrap();
        }
        let execution =
            OrderManager::new(account, facts, vec![sec("A"), sec("B")], limits).unwrap();
        Self {
            execution,
            matcher: Plans {
                cap: i64::MAX,
                seen: vec![],
            },
            host: isolated::Host::default(),
            writer: isolated::Writer::default(),
            data: isolated::Data::default(),
            control: isolated::Control::default(),
        }
    }
    fn run(
        &mut self,
        frequency: Frequency,
        count: usize,
        events: Vec<MarketEvent>,
        chunk: usize,
    ) -> EngineReport {
        let calendar = SessionCalendar::new(
            MODEL.into(),
            rows()[..count].to_vec(),
            Some(rows()[count].session.key.clone()),
        )
        .unwrap();
        Engine::new(
            isolated::config(frequency),
            calendar,
            vec![isolated::Source::new(events, chunk)],
        )
        .unwrap()
        .run(EnginePorts {
            data: &mut self.data,
            execution: &mut self.execution,
            matcher: &mut self.matcher,
            rules: &mut isolated::Rules,
            strategy: &mut self.host,
            control: &mut self.control,
            results: &mut self.writer,
        })
    }
}
fn intent(symbol: &str, value: IntentValue, tif: TimeInForce) -> OrderIntent {
    OrderIntent {
        security: sec(symbol),
        side: Side::Buy,
        value,
        style: OrderStyle::Market,
        tif,
        submitted_at: command(&rows()[0], symbol, 1, rows()[0].before_open_ns),
    }
}
fn trades(h: &Harness) -> Vec<&Fill> {
    h.writer
        .records
        .iter()
        .filter_map(|record| match record {
            ResultRecord::Trade(fill) => Some(fill),
            _ => None,
        })
        .collect()
}

#[test]
fn actual_driver_bar_barrier_and_chunk_boundaries_produce_identical_orders_account_and_results() {
    let mut baseline = None;
    for chunk in [1, 2, 7] {
        let calendar = rows();
        let mut h = Harness::new("10000");
        h.host.initialize = Some(Box::new(|_, commands, _| {
            assert!(
                commands
                    .submit(&intent(
                        "A",
                        IntentValue::TargetQuantity(q(10)),
                        TimeInForce::Gtc
                    ))?
                    .accepted
            );
            Ok(())
        }));
        let mut calls = 0;
        h.host.bars = Some(Box::new(move |boundary, view, commands| {
            assert_eq!(boundary.securities, vec![sec("A"), sec("B")]);
            calls += 1;
            if calls == 1 {
                assert_eq!(view.account()?.positions[&sec("A")].quantity, q(10));
                assert!(
                    commands
                        .submit(&intent(
                            "B",
                            IntentValue::TargetQuantity(q(10)),
                            TimeInForce::Gtc
                        ))?
                        .accepted
                );
            }
            Ok(())
        }));
        let events = calendar[..2]
            .iter()
            .flat_map(|row| [isolated::daily(row, "B"), isolated::daily(row, "A")])
            .collect();
        let report = h.run(Frequency::Day, 2, events, chunk);
        assert!(
            matches!(report.outcome, RunOutcome::Succeeded { .. }),
            "{:?}",
            report.outcome
        );
        let fills = trades(&h);
        assert_eq!(fills.len(), 2);
        assert_eq!(fills[0].security, sec("A"));
        assert_eq!(fills[1].security, sec("B"));
        assert_eq!(fills[1].execution_time_ns, calendar[1].session.close_ns);
        assert_eq!(h.host.bar_boundaries.len(), 2);
        let actual = (
            h.writer.records,
            h.execution.value(calendar[1].after_close_ns).unwrap(),
        );
        if let Some(expected) = &baseline {
            assert_eq!(expected, &actual);
        } else {
            baseline = Some(actual);
        }
    }
}

#[test]
fn actual_driver_close_day_and_target_replacement_send_all_notifications_without_recursion() {
    let calendar = rows();
    let mut h = Harness::new("10000");
    h.matcher.cap = 0;
    h.host.initialize = Some(Box::new(|_, commands, _| {
        commands.submit(&intent(
            "A",
            IntentValue::TargetQuantity(q(10)),
            TimeInForce::Gtc,
        ))?;
        commands.submit(&intent(
            "A",
            IntentValue::TargetQuantity(q(20)),
            TimeInForce::Gtc,
        ))?;
        Ok(())
    }));
    let depth = Rc::new(Cell::new(0));
    let max_depth = Rc::new(Cell::new(0));
    let d1 = depth.clone();
    let d2 = max_depth.clone();
    let mut cancels = 0;
    h.host.notice = Some(Box::new(move |notice, _, commands| {
        d1.set(d1.get() + 1);
        d2.set(d2.get().max(d1.get()));
        if let Notification::Order(order) = notice
            && order.status == OrderStatus::Cancelled
        {
            cancels += 1;
            if cancels == 1 {
                commands.submit(&intent("B", IntentValue::Quantity(q(1)), TimeInForce::Gtc))?;
            }
        }
        d1.set(d1.get() - 1);
        Ok(())
    }));
    let after_count = Rc::new(Cell::new(0));
    let count = after_count.clone();
    h.host.after = Some(Box::new(move |_, commands| {
        if count.get() == 0 {
            commands.submit(&intent("B", IntentValue::Quantity(q(5)), TimeInForce::Day))?;
        }
        count.set(count.get() + 1);
        Ok(())
    }));
    let report = h.run(Frequency::Day, 2, vec![], 1);
    assert!(
        matches!(report.outcome, RunOutcome::Succeeded { .. }),
        "{:?}",
        report.outcome
    );
    assert_eq!(max_depth.get(), 1);
    let notices: Vec<_> = h
        .host
        .notices
        .iter()
        .filter_map(|n| match n {
            Notification::Order(o) => Some(o),
            _ => None,
        })
        .collect();
    assert!(
        notices
            .iter()
            .any(|o| o.security == sec("A") && o.status == OrderStatus::Cancelled)
    );
    let day = notices
        .iter()
        .find(|o| o.tif == TimeInForce::Day && o.status == OrderStatus::Expired)
        .unwrap();
    assert_eq!(day.effective_session, calendar[1].session.key);
    assert_eq!(
        day.updated_at.as_ref().unwrap().time_ns,
        calendar[1].session.close_ns
    );
    assert_eq!(
        h.writer
            .records
            .iter()
            .filter(|r| matches!(r, ResultRecord::Order(o) if o.status == OrderStatus::Cancelled))
            .count(),
        1
    );
}

#[test]
fn actual_driver_corporate_effects_and_gtc_cancellation_use_real_closing_postcondition() {
    let calendar = rows();
    let mut h = Harness::new("10000");
    let mut action = oracle::dividend(&calendar, CashDividendTax::SyntheticFlat { rate: d("0") });
    if let CorporateActionKind::CashDividend { payment, .. } = &mut action.kind {
        *payment = oracle::boundary(&calendar[1], ActionPhase::SessionClose);
    }
    h.execution
        .add_corporate_action(action, calendar[0].before_open_ns)
        .unwrap();
    h.host.initialize = Some(Box::new(|_, commands, _| {
        commands.submit(&intent(
            "A",
            IntentValue::Quantity(q(100)),
            TimeInForce::Gtc,
        ))?;
        Ok(())
    }));
    let mut calls = 0;
    h.host.bars = Some(Box::new(move |_, view, commands| {
        calls += 1;
        if calls == 1 {
            commands.submit(&intent("A", IntentValue::Quantity(q(10)), TimeInForce::Gtc))?;
        } else {
            assert_eq!(view.account()?.receivables, d("0"));
            assert_eq!(view.account()?.cash, d("9019"));
        }
        Ok(())
    }));
    let report = h.run(
        Frequency::Day,
        2,
        calendar[..2]
            .iter()
            .map(|row| isolated::daily(row, "A"))
            .collect(),
        1,
    );
    assert!(
        matches!(report.outcome, RunOutcome::Succeeded { .. }),
        "{:?}",
        report.outcome
    );
    assert_eq!(trades(&h).len(), 1);
    assert!(h.host.notices.iter().any(|n| matches!(n, Notification::Order(o) if o.status == OrderStatus::Cancelled && o.message.contains("公司行动"))));
    let effects: Vec<_> = h
        .writer
        .records
        .iter()
        .filter_map(|r| match r {
            ResultRecord::CorporateAction(e) => Some(e),
            _ => None,
        })
        .collect();
    assert_eq!(
        effects.iter().map(|e| e.effect.kind).collect::<Vec<_>>(),
        vec![
            CorporateEffectKind::Registered,
            CorporateEffectKind::ExDividend,
            CorporateEffectKind::PaidDividend
        ]
    );
    assert_eq!(effects[2].time_ns, calendar[1].session.close_ns);
    assert_eq!(effects[2].effect.cash_delta, d("20"));
}

#[test]
fn actual_driver_same_ns_ticks_and_candidate_price_budget_preserve_events_and_cash() {
    let row = &rows()[0];
    let mut h = Harness::new("2001");
    h.host.initialize = Some(Box::new(|_, commands, _| {
        commands.submit(&intent(
            "A",
            IntentValue::Quantity(q(200)),
            TimeInForce::Gtc,
        ))?;
        Ok(())
    }));
    let events = vec![
        tick(row, "A", 1, time(row, 570), "12"),
        tick(row, "A", 2, time(row, 570), "0.2"),
    ];
    let report = h.run(Frequency::Tick, 1, events, 1);
    assert!(
        matches!(report.outcome, RunOutcome::Succeeded { .. }),
        "{:?}",
        report.outcome
    );
    let fills = trades(&h);
    assert_eq!(fills.len(), 2);
    assert_eq!((fills[0].quantity, fills[1].quantity), (q(166), q(34)));
    assert_eq!((fills[0].fee, fills[1].fee), (d("1"), d("0")));
    assert_eq!(h.host.tick_keys.len(), 2);
    assert_eq!(report.progress.completed_events, 2);
    assert_eq!(
        h.execution.value(row.after_close_ns).unwrap().cash,
        d("1.2")
    );
}

#[test]
fn actual_driver_last_close_tick_records_entitlement_before_close_timer_callback() {
    let calendar = rows();
    let row = &calendar[0];
    let mut h = Harness::new("10000");
    h.execution
        .add_corporate_action(
            oracle::dividend(&calendar, CashDividendTax::SyntheticFlat { rate: d("0") }),
            row.before_open_ns,
        )
        .unwrap();
    h.host.initialize = Some(Box::new(|_, commands, registration| {
        commands.submit(&intent(
            "A",
            IntentValue::Quantity(q(100)),
            TimeInForce::Gtc,
        ))?;
        registration.register(
            CallbackId::new("close")?,
            Cadence::Daily,
            ScheduleTime::LocalMinute(900),
        )?;
        Ok(())
    }));
    h.matcher.cap = 50;
    h.host.scheduled_hook = Some(Box::new(|view, _| {
        assert_eq!(view.account()?.positions[&sec("A")].quantity, q(100));
        Ok(())
    }));
    let events = vec![
        tick(row, "A", 1, row.session.close_ns, "10"),
        tick(row, "A", 2, row.session.close_ns, "10"),
    ];
    let report = h.run(Frequency::Tick, 1, events, 2);
    assert!(
        matches!(report.outcome, RunOutcome::Succeeded { .. }),
        "{:?}",
        report.outcome
    );
    assert_eq!(h.host.tick_keys.len(), 2);
    assert_eq!(h.host.scheduled_calls.len(), 1);
    let registered = h
        .writer
        .records
        .iter()
        .find_map(|record| match record {
            ResultRecord::CorporateAction(e)
                if e.effect.kind == CorporateEffectKind::Registered =>
            {
                Some(e)
            }
            _ => None,
        })
        .unwrap();
    assert_eq!(registered.effect.quantity, q(100));
    h.execution.check_session_end(&row.session.key).unwrap();
}

#[test]
fn actual_driver_cumulative_gap_candidates_do_not_spend_same_available_cash_twice() {
    let row = &rows()[0];
    let mut h = Harness::new("350");
    h.host.initialize = Some(Box::new(|_, commands, _| {
        commands.submit(&intent("A", IntentValue::Quantity(q(10)), TimeInForce::Gtc))?;
        commands.submit(&intent("A", IntentValue::Quantity(q(10)), TimeInForce::Gtc))?;
        Ok(())
    }));
    let report = h.run(
        Frequency::Tick,
        1,
        vec![tick(row, "A", 1, time(row, 570), "20")],
        1,
    );
    assert!(
        matches!(report.outcome, RunOutcome::Succeeded { .. }),
        "{:?}",
        report.outcome
    );
    let fills = trades(&h);
    // 10*20+1, then 7*20+1: total 342; cash 8. Independent quotes before
    // previewing the first fill would each permit 10 and spend 402 from 350.
    assert_eq!(
        fills.iter().map(|f| f.quantity).collect::<Vec<_>>(),
        vec![q(10), q(7)]
    );
    assert_eq!(h.execution.value(row.after_close_ns).unwrap().cash, d("8"));
    assert_eq!(h.execution.account().held_quantity(&sec("A")), q(17));
    assert_eq!(h.execution.account().totals().trading_fees, d("2"));
    assert_eq!(
        h.execution.open_orders()[0].reason_code,
        Some(ErrorCode::InsufficientCash)
    );
}

#[test]
fn actual_driver_terminal_cancel_after_cache_eviction_and_unknown_cancel_do_not_reenter() {
    let mut h = Harness::with_limits(
        "10000",
        OrderLimits {
            terminal_cache: 1,
            ..OrderLimits::default()
        },
    );
    h.host.initialize = Some(Box::new(|view, commands, _| {
        let a = commands
            .submit(&intent("A", IntentValue::Quantity(q(1)), TimeInForce::Gtc))?
            .order_id
            .unwrap();
        let b = commands
            .submit(&intent("B", IntentValue::Quantity(q(1)), TimeInForce::Gtc))?
            .order_id
            .unwrap();
        assert!(commands.cancel(&a)?.accepted);
        assert!(commands.cancel(&b)?.accepted);
        let again = commands.cancel(&a)?;
        assert!(again.accepted && again.unchanged);
        let unknown = commands.cancel("unknown-order")?;
        assert!(!unknown.accepted);
        assert_eq!(unknown.reason_code, Some(ErrorCode::InvalidOrder));
        assert_eq!(view.account()?.frozen_cash, d("0"));
        assert_eq!(view.account()?.cash, d("10000"));
        Ok(())
    }));
    let report = h.run(Frequency::Day, 1, vec![], 1);
    assert!(
        matches!(report.outcome, RunOutcome::Succeeded { .. }),
        "{:?}",
        report.outcome
    );
    assert_eq!(h.writer.records.iter().filter(|record| matches!(record, ResultRecord::Order(order) if order.status == OrderStatus::Cancelled)).count(), 2);
}

#[test]
fn actual_driver_close_tick_before_last_keeps_auction_phase_and_last_callback_day_is_next_session()
{
    let calendar = rows();
    let row = &calendar[0];
    let mut h = Harness::new("10000");
    let mut calls = 0;
    h.host.ticks = Some(Box::new(move |_, _, commands| {
        calls += 1;
        if calls == 1 {
            let refused =
                commands.submit(&intent("A", IntentValue::Quantity(q(1)), TimeInForce::Day))?;
            assert!(!refused.accepted);
            assert_eq!(refused.reason_code, Some(ErrorCode::InvalidOrder));
            let mut limit = intent("A", IntentValue::Quantity(q(1)), TimeInForce::Day);
            limit.style = OrderStyle::Limit { price: p("10") };
            assert!(commands.submit(&limit)?.accepted);
        } else {
            assert!(
                commands
                    .submit(&intent("A", IntentValue::Quantity(q(1)), TimeInForce::Day))?
                    .accepted
            );
        }
        Ok(())
    }));
    let report = h.run(
        Frequency::Tick,
        1,
        vec![
            tick(row, "A", 1, row.session.close_ns, "10"),
            tick(row, "A", 2, row.session.close_ns, "10"),
        ],
        1,
    );
    assert!(
        matches!(report.outcome, RunOutcome::Succeeded { .. }),
        "{:?}",
        report.outcome
    );
    assert_eq!(trades(&h).len(), 1);
    assert_eq!(h.execution.open_orders().len(), 1);
    assert_eq!(
        h.execution.open_orders()[0].effective_session,
        calendar[1].session.key
    );
}

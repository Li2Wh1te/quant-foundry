use super::support::*;
use qf_core::clock::{Cadence, CallbackId, ScheduleTime, SessionCalendar};
use qf_core::engine::*;
use qf_core::results::ResultRecord;
use qf_core::run::RunOutcome;

struct Rules;
impl RulesPort<BarRules> for Rules {
    fn for_event(&mut self, event: &MarketEvent, row: &CalendarSession) -> QfResult<BarRules> {
        Ok(rules(row, event.key().security.as_str()))
    }
}
struct Harness {
    config: RunConfig,
    execution: Manager,
    matcher: BarMatcher,
    host: isolated::Host,
    writer: isolated::Writer,
    data: isolated::Data,
    control: isolated::Control,
}
impl Harness {
    fn new(frequency: Frequency, participation: &str, cash: &str) -> Self {
        let config = config(frequency, participation, "0", cash);
        let calendar = rows();
        let facts = Facts::default();
        let mut account = Account::new(
            config.initial_cash,
            calendar.clone(),
            usage(),
            AccountLimits::default(),
        )
        .unwrap();
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
        let execution = OrderManager::new(
            account,
            facts,
            config.universe.clone(),
            OrderLimits::default(),
        )
        .unwrap();
        Self {
            matcher: BarMatcher::from_run(&config).unwrap(),
            config,
            execution,
            host: isolated::Host::default(),
            writer: isolated::Writer::default(),
            data: isolated::Data::default(),
            control: isolated::Control::default(),
        }
    }
    fn run(
        &mut self,
        count: usize,
        events: Vec<MarketEvent>,
        chunk: usize,
        reverse_sources: bool,
    ) -> EngineReport {
        let calendar = SessionCalendar::new(
            MODEL.into(),
            rows()[..count].to_vec(),
            Some(rows()[count].session.key.clone()),
        )
        .unwrap();
        let (a, b): (Vec<_>, Vec<_>) = events
            .into_iter()
            .partition(|e| e.key().security == sec("A"));
        let mut sources = vec![
            isolated::Source::new(a, chunk),
            isolated::Source::new(b, chunk),
        ];
        if reverse_sources {
            sources.reverse();
        }
        Engine::from_run(
            &self.config,
            "synthetic_bar_result".into(),
            EngineLimits::default(),
            calendar,
            sources,
        )
        .unwrap()
        .run(EnginePorts {
            data: &mut self.data,
            execution: &mut self.execution,
            matcher: &mut self.matcher,
            rules: &mut Rules,
            strategy: &mut self.host,
            control: &mut self.control,
            results: &mut self.writer,
        })
    }
    fn trades(&self) -> Vec<&Fill> {
        self.writer
            .records
            .iter()
            .filter_map(|r| {
                if let ResultRecord::Trade(f) = r {
                    Some(f)
                } else {
                    None
                }
            })
            .collect()
    }
    fn success(&self, report: &EngineReport) {
        assert!(
            matches!(report.outcome, RunOutcome::Succeeded { .. }),
            "{:?}",
            report.outcome
        );
        assert_eq!(self.writer.finalized, 1);
        assert_eq!(self.data.closed, 1);
        assert_eq!(self.host.finish_count, 1);
        assert!(report.cleanup_errors.is_empty());
    }
}
fn first_bar(
    row: &CalendarSession,
    symbol: &str,
    frequency: Frequency,
    volume: i64,
) -> MarketEvent {
    let end = if frequency == Frequency::Day {
        900
    } else {
        570 + match frequency {
            Frequency::Minute => 1,
            Frequency::FiveMinutes => 5,
            Frequency::FifteenMinutes => 15,
            Frequency::ThirtyMinutes => 30,
            Frequency::Hour => 60,
            _ => unreachable!(),
        }
    };
    bar(
        row,
        symbol,
        570,
        end,
        1,
        ["10", "12", "9", "10"],
        Some(volume),
    )
}
fn intent(symbol: &str, quantity: i64, tif: TimeInForce) -> OrderIntent {
    OrderIntent {
        security: sec(symbol),
        side: Side::Buy,
        value: IntentValue::Quantity(q(quantity)),
        style: OrderStyle::Market,
        tif,
        submitted_at: command(&rows()[0], symbol, 1, rows()[0].before_open_ns),
    }
}

#[test]
fn real_driver_multisymbol_barrier_and_all_bar_frequencies_are_chunk_and_source_independent() {
    let mut semantic_baseline = None;
    for frequency in [
        Frequency::Day,
        Frequency::Minute,
        Frequency::FiveMinutes,
        Frequency::FifteenMinutes,
        Frequency::ThirtyMinutes,
        Frequency::Hour,
    ] {
        let mut records_baseline = None;
        for (chunk, reverse_sources) in [(1, false), (2, true), (17, false)] {
            let mut h = Harness::new(frequency, "0.5", "10000");
            h.writer.credit_records = 1;
            h.host.initialize = Some(Box::new(|_, commands, _| {
                assert!(
                    commands
                        .submit(&intent("A", 10, TimeInForce::Gtc))?
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
                    assert!(!view.account()?.positions.contains_key(&sec("B")));
                    assert!(
                        commands
                            .submit(&intent("B", 10, TimeInForce::Gtc))?
                            .accepted
                    );
                }
                Ok(())
            }));
            let events = rows()[..2]
                .iter()
                .flat_map(|row| {
                    [
                        first_bar(row, "B", frequency, 100),
                        first_bar(row, "A", frequency, 100),
                    ]
                })
                .collect();
            let report = h.run(2, events, chunk, reverse_sources);
            h.success(&report);
            assert_eq!(h.host.bar_boundaries.len(), 2);
            let fills = h.trades();
            assert_eq!(fills.len(), 2);
            assert_eq!(fills[0].security, sec("A"));
            assert_eq!(fills[1].security, sec("B"));
            assert_eq!(
                fills[1].execution_time_ns,
                first_bar(&rows()[1], "B", frequency, 100).key().time_ns
            );
            let semantic = (
                fills
                    .iter()
                    .map(|f| (f.security.clone(), f.quantity, f.price, f.fee))
                    .collect::<Vec<_>>(),
                h.execution.value(rows()[1].after_close_ns).unwrap().cash,
            );
            assert_eq!(semantic.1, d("9798")); // Two independent 10*10+1 fills.
            if let Some(baseline) = &semantic_baseline {
                assert_eq!(baseline, &semantic);
            } else {
                semantic_baseline = Some(semantic);
            }
            let descriptions: Vec<_> = h
                .writer
                .records
                .iter()
                .filter_map(|r| {
                    if let ResultRecord::ExecutionModel(d) = r {
                        Some(d)
                    } else {
                        None
                    }
                })
                .collect();
            assert_eq!(descriptions.len(), 1);
            assert_eq!(descriptions[0].frequency, frequency);
            assert_eq!(descriptions[0].participation_rate, d("0.5"));
            assert!(
                descriptions[0]
                    .assumptions
                    .iter()
                    .any(|s| s.contains("OHLC"))
            );
            let wire = serde_json::to_value(&h.writer.records[0]).unwrap();
            assert_eq!(wire["kind"], "execution_model");
            assert_eq!(wire["record"]["slippage_bps"], "0");
            h.execution
                .check_session_end(&rows()[1].session.key)
                .unwrap();
            if let Some(baseline) = &records_baseline {
                assert_eq!(baseline, &h.writer.records);
            } else {
                records_baseline = Some(h.writer.records);
            }
        }
    }
}

#[test]
fn before_open_schedule_orders_enter_the_following_first_bar() {
    for frequency in [Frequency::Day, Frequency::Minute, Frequency::Hour] {
        let mut h = Harness::new(frequency, "1", "10000");
        h.host.initialize = Some(Box::new(|_, _, registration| {
            registration.register(
                CallbackId::new("before")?,
                Cadence::Daily,
                ScheduleTime::BeforeOpen,
            )?;
            Ok(())
        }));
        h.host.scheduled_hook = Some(Box::new(|_, commands| {
            assert!(
                commands
                    .submit(&intent("A", 10, TimeInForce::Day))?
                    .accepted
            );
            Ok(())
        }));
        let event = first_bar(&rows()[0], "A", frequency, 100);
        let end = event.key().time_ns;
        let report = h.run(1, vec![event], 1, false);
        h.success(&report);
        assert_eq!(h.trades().len(), 1);
        assert_eq!(h.trades()[0].execution_time_ns, end);
        assert_eq!(h.trades()[0].quantity, q(10));
        assert!(h.execution.open_orders().is_empty());
    }
}

#[test]
fn after_close_day_is_next_session_fills_then_expires_after_last_market() {
    let calendar = rows();
    let mut h = Harness::new(Frequency::Day, "1", "10000");
    let mut calls = 0;
    h.host.after = Some(Box::new(move |_, commands| {
        calls += 1;
        if calls == 1 {
            assert!(commands.submit(&intent("B", 5, TimeInForce::Day))?.accepted);
        }
        Ok(())
    }));
    let report = h.run(
        2,
        vec![
            first_bar(&calendar[0], "B", Frequency::Day, 0),
            first_bar(&calendar[1], "B", Frequency::Day, 2),
        ],
        1,
        true,
    );
    h.success(&report);
    let fills = h.trades();
    assert_eq!(fills.len(), 1);
    assert_eq!(fills[0].quantity, q(2));
    assert_eq!(fills[0].execution_time_ns, calendar[1].session.close_ns);
    let expired = h
        .writer
        .records
        .iter()
        .find_map(|r| {
            if let ResultRecord::Order(o) = r {
                (o.status == OrderStatus::Expired).then_some(o)
            } else {
                None
            }
        })
        .unwrap();
    assert_eq!(expired.effective_session, calendar[1].session.key);
    assert_eq!(expired.filled_quantity, q(2));
    assert_eq!(
        expired.updated_at.as_ref().unwrap().time_ns,
        calendar[1].session.close_ns
    );
    assert!(h.execution.open_orders().is_empty());
    let account = h.execution.value(calendar[1].after_close_ns).unwrap();
    assert_eq!(account.cash, d("9979"));
    assert_eq!(account.frozen_cash, d("0"));
}

#[test]
fn trade_notification_order_for_other_symbol_cannot_enter_same_close_bar() {
    let mut h = Harness::new(Frequency::Day, "1", "10000");
    h.host.initialize = Some(Box::new(|_, commands, _| {
        commands.submit(&intent("A", 10, TimeInForce::Gtc))?;
        Ok(())
    }));
    h.host.notice = Some(Box::new(|notice, _, commands| {
        if matches!(notice,Notification::Trade(f) if f.security==sec("A")) {
            commands.submit(&intent("B", 10, TimeInForce::Gtc))?;
        }
        Ok(())
    }));
    let events = rows()[..2]
        .iter()
        .flat_map(|row| {
            [
                first_bar(row, "A", Frequency::Day, 100),
                first_bar(row, "B", Frequency::Day, 100),
            ]
        })
        .collect();
    let report = h.run(2, events, 1, true);
    h.success(&report);
    assert_eq!(h.trades().len(), 2);
    assert_eq!(h.trades()[1].execution_time_ns, rows()[1].session.close_ns);
}

#[test]
fn actual_bar_driver_uses_cumulative_budget_and_real_closing_actions_and_postcondition() {
    let calendar = rows();
    let mut h = Harness::new(Frequency::Day, "1", "350");
    h.host.initialize = Some(Box::new(|_, commands, _| {
        commands.submit(&intent("A", 10, TimeInForce::Gtc))?;
        commands.submit(&intent("A", 10, TimeInForce::Gtc))?;
        Ok(())
    }));
    let report = h.run(
        1,
        vec![bar(
            &calendar[0],
            "A",
            570,
            900,
            1,
            ["20", "21", "19", "20"],
            Some(100),
        )],
        1,
        false,
    );
    h.success(&report);
    assert_eq!(
        h.trades().iter().map(|f| f.quantity).collect::<Vec<_>>(),
        vec![q(10), q(7)]
    );
    assert_eq!(
        h.execution.value(calendar[0].after_close_ns).unwrap().cash,
        d("8")
    );

    let mut h = Harness::new(Frequency::Day, "1", "10000");
    let mut dividend = oracle::dividend(&calendar, CashDividendTax::SyntheticFlat { rate: d("0") });
    if let CorporateActionKind::CashDividend { payment, .. } = &mut dividend.kind {
        *payment = oracle::boundary(&calendar[1], ActionPhase::SessionClose);
    }
    h.execution
        .add_corporate_action(dividend, calendar[0].before_open_ns)
        .unwrap();
    h.host.initialize = Some(Box::new(|_, commands, _| {
        commands.submit(&intent("A", 100, TimeInForce::Gtc))?;
        Ok(())
    }));
    let report = h.run(
        2,
        calendar[..2]
            .iter()
            .map(|row| first_bar(row, "A", Frequency::Day, 100))
            .collect(),
        2,
        false,
    );
    h.success(&report);
    let value = h.execution.value(calendar[1].after_close_ns).unwrap();
    assert_eq!(value.cash, d("9019"));
    assert_eq!(value.receivables, d("0"));
    let effects: Vec<_> = h
        .writer
        .records
        .iter()
        .filter_map(|r| {
            if let ResultRecord::CorporateAction(e) = r {
                Some(e.effect.kind)
            } else {
                None
            }
        })
        .collect();
    assert_eq!(
        effects,
        vec![
            CorporateEffectKind::Registered,
            CorporateEffectKind::ExDividend,
            CorporateEffectKind::PaidDividend
        ]
    );
    h.execution
        .check_session_end(&calendar[1].session.key)
        .unwrap();
}

#[test]
fn description_is_required_result_even_without_market_and_mismatched_matcher_config_refuses() {
    let mut h = Harness::new(Frequency::Day, "0.1", "10000");
    let report = h.run(1, vec![], 1, false);
    h.success(&report);
    assert!(matches!(
        &h.writer.records[0],
        ResultRecord::ExecutionModel(_)
    ));
    let mut h = Harness::new(Frequency::Day, "0.1", "10000");
    h.host.initialize = Some(Box::new(|_, _, _| {
        Err(QfError::new(
            ErrorCode::StrategyError,
            "initialize",
            "synthetic initialization failure",
        ))
    }));
    let report = h.run(1, vec![], 1, false);
    assert!(matches!(
        report.outcome,
        RunOutcome::Failed {
            error: QfError {
                code: ErrorCode::StrategyError,
                ..
            },
            partial: true
        }
    ));
    assert_eq!(h.writer.records.len(), 1);
    assert!(matches!(
        &h.writer.records[0],
        ResultRecord::ExecutionModel(_)
    ));
    assert_eq!(h.writer.aborted, 1);
    let mut h = Harness::new(Frequency::Day, "0.1", "10000");
    h.matcher = matcher("0.1", "0"); // Minute matcher cannot describe a daily run.
    let report = h.run(1, vec![], 1, false);
    assert!(matches!(
        report.outcome,
        RunOutcome::Failed {
            error: QfError {
                code: ErrorCode::InvalidRunConfig,
                ..
            },
            ..
        }
    ));
    assert!(h.host.calls.is_empty());
    let mut h = Harness::new(Frequency::Day, "0.1", "10000");
    h.matcher = BarMatcher::from_run(&config(Frequency::Day, "0.9", "7", "10000")).unwrap();
    let report = h.run(1, vec![], 1, false);
    assert!(matches!(
        report.outcome,
        RunOutcome::Failed {
            error: QfError {
                code: ErrorCode::InvalidRunConfig,
                ..
            },
            ..
        }
    ));
    assert!(h.host.calls.is_empty());
}

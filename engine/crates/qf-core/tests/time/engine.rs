use super::support::*;
use qf_core::ErrorCode;
use qf_core::clock::{Cadence, CallbackId, ScheduleTime, SubscriptionKind};
use qf_core::engine::Notification;
use qf_core::orders::{OrderStatus, TimeInForce};
use qf_core::run::{ExecutionModel, Frequency, RunOutcome};
use qf_core::types::{ExactDecimal, MarketEvent};
use std::cell::Cell;
use std::rc::Rc;

#[test]
fn cross_symbol_bar_barrier_matches_all_accounts_before_one_callback() {
    let mut baseline = None;
    for chunk in [1, 2, 7] {
        for reverse in [false, true] {
            let sessions = calendar(2);
            let mut h = Harness::default();
            h.host.initialize = Some(Box::new(|_, commands, _| {
                commands.submit(&intent("A", TimeInForce::Gtc))?;
                Ok(())
            }));
            let mut calls = 0;
            h.host.bars = Some(Box::new(move |boundary, view, commands| {
                assert_eq!(boundary.securities, [security("A"), security("B")]);
                calls += 1;
                if calls == 1 {
                    // Synchronous reservations update the same callback's account.
                    assert_eq!(view.account()?.frozen_cash, ExactDecimal::ZERO);
                    commands.submit(&intent("B", TimeInForce::Gtc))?;
                    assert_eq!(view.account()?.frozen_cash, ExactDecimal::ONE);
                }
                Ok(())
            }));
            let mut sources: Vec<_> = ["A", "B"]
                .into_iter()
                .map(|symbol| {
                    Source::new(
                        sessions
                            .sessions()
                            .iter()
                            .map(|s| daily(s, symbol))
                            .collect(),
                        chunk,
                    )
                })
                .collect();
            if reverse {
                sources.reverse();
            }
            let report = h.run(config(Frequency::Day), sessions.clone(), sources);
            assert_eq!(code(&report), None);
            assert_eq!(report.progress.completed_events, 4);
            assert_eq!(h.host.bar_boundaries.len(), 2);
            assert_eq!(h.execution.fills.len(), 2);
            assert_eq!(h.execution.fills[0].security, security("A"));
            assert_eq!(h.execution.fills[1].security, security("B"));
            assert_eq!(
                h.execution.fills[1].execution_time_ns,
                sessions.sessions()[1].session.close_ns
            );
            assert_eq!(
                h.execution.orders["o2"].eligible_interval_start,
                Some(sessions.sessions()[0].session.close_ns)
            );
            let trace = h.host.trace.borrow();
            assert!(
                trace.iter().position(|x| x == "fill:A").unwrap()
                    < trace.iter().position(|x| x == "handle_data").unwrap()
            );
            let actual = (
                h.writer.records.clone(),
                h.host.bar_boundaries.clone(),
                h.matcher.seen.clone(),
            );
            if let Some(expected) = &baseline {
                assert_eq!(&actual, expected);
            } else {
                baseline = Some(actual);
            }
            assert_eq!(h.writer.finalized, 1);
            assert_eq!(h.host.finish_count, 1);
        }
    }
}
#[test]
fn intraday_hour_callback_orders_cannot_use_the_interval_just_completed() {
    let session = row(0);
    let mut h = Harness::default();
    h.host.initialize = Some(Box::new(|_, _, r| {
        r.register(
            CallbackId::new("at_1030")?,
            Cadence::Daily,
            "10:30".parse()?,
        )?;
        Ok(())
    }));
    h.host.scheduled_hook = Some(Box::new(|_, c| {
        c.submit(&intent("B", TimeInForce::Gtc))?;
        Ok(())
    }));
    let events = ["A", "B"]
        .into_iter()
        .flat_map(|symbol| {
            [
                bar(
                    &session,
                    symbol,
                    local(&session, 570),
                    local(&session, 630),
                    1,
                ),
                bar(
                    &session,
                    symbol,
                    local(&session, 630),
                    local(&session, 690),
                    2,
                ),
            ]
        })
        .collect();
    let report = h.run(
        config(Frequency::Hour),
        calendar(1),
        vec![Source::new(events, 1)],
    );
    assert_eq!(code(&report), None);
    assert_eq!(h.execution.fills.len(), 1);
    assert_eq!(h.execution.fills[0].execution_time_ns, local(&session, 690));
    assert_eq!(h.host.bar_boundaries.len(), 2);
    assert_eq!(
        h.matcher
            .seen
            .iter()
            .find(|(key, _)| key.security == security("B") && key.time_ns == local(&session, 630))
            .unwrap()
            .1,
        Vec::<String>::new()
    );
}
#[test]
fn same_nanosecond_ticks_keep_every_callback_and_next_sequence_activation() {
    let session = row(0);
    let time = ns(local(&session, 600).get() + 123);
    let events = vec![
        tick(&session, "A", "trade", time, 1),
        tick(&session, "A", "trade", time, 2),
    ];
    let mut h = Harness::default();
    h.data.future = events.iter().map(MarketEvent::key).collect();
    let mut count = 0;
    h.host.ticks = Some(Box::new(move |_, view, commands| {
        count += 1;
        assert_eq!(view.now_ns(), time);
        assert_eq!(
            view.read(&request(time, Frequency::Tick))?.keys.len(),
            count
        );
        if count == 1 {
            commands.submit(&intent("A", TimeInForce::Gtc))?;
        }
        Ok(())
    }));
    let report = h.run(
        config(Frequency::Tick),
        calendar(1),
        vec![Source::new(events.clone(), 2)],
    );
    assert_eq!(code(&report), None);
    assert_eq!(
        h.host.tick_keys,
        events.iter().map(MarketEvent::key).collect::<Vec<_>>()
    );
    assert_eq!(h.execution.fills.len(), 1);
    assert!(h.matcher.seen[0].1.is_empty());
    assert_eq!(h.matcher.seen[1].1, ["o1"]);
    assert_eq!(h.execution.normalized[0].submitted_at.time_ns, time);
    assert_eq!(
        h.execution.orders["o1"].eligible_after_event,
        Some(events[0].key())
    );
}

#[test]
fn close_nanosecond_tick_successor_can_fill_before_day_expiry() {
    let session = row(0);
    let mut h = Harness::default();
    let mut count = 0;
    h.host.ticks = Some(Box::new(move |_, _, commands| {
        count += 1;
        if count == 1 {
            commands.submit(&intent("A", TimeInForce::Day))?;
        }
        if count == 2 {
            commands.submit(&intent("B", TimeInForce::Day))?;
        }
        Ok(())
    }));
    let report = h.run(
        config(Frequency::Tick),
        calendar(1),
        vec![Source::new(
            vec![
                tick(&session, "A", "a", session.session.close_ns, 1),
                tick(&session, "A", "a", session.session.close_ns, 2),
            ],
            1,
        )],
    );
    assert_eq!(code(&report), None);
    assert_eq!(h.execution.fills.len(), 1);
    assert_eq!(
        h.execution.orders["o1"].effective_session,
        session_key("S1")
    );
    assert_eq!(h.execution.orders["o1"].status, OrderStatus::Filled);
    assert_eq!(
        h.execution.orders["o2"].effective_session,
        session_key("NEXT")
    );
    assert_eq!(h.execution.orders["o2"].status, OrderStatus::Open);
}
#[test]
fn quote_tick_path_uses_shared_identity_without_enabling_trade_model() {
    let session = row(0);
    let time = local(&session, 600);
    let mut h = Harness::default();
    let mut count = 0;
    h.host.ticks = Some(Box::new(move |_, _, commands| {
        count += 1;
        if count == 1 {
            commands.submit(&intent("A", TimeInForce::Gtc))?;
        }
        Ok(())
    }));
    let mut cfg = config(Frequency::Tick);
    cfg.execution_model = ExecutionModel::QuoteTickV1;
    let report = h.run(
        cfg,
        calendar(1),
        vec![Source::new(
            vec![quote(&session, "A", time, 1), quote(&session, "A", time, 2)],
            1,
        )],
    );
    assert_eq!(code(&report), None);
    assert_eq!(h.host.tick_keys.len(), 2);
    assert_eq!(h.execution.fills.len(), 1);
    let mut mixed = Harness::default();
    let report = mixed.run(
        config(Frequency::Tick),
        calendar(1),
        vec![Source::new(vec![quote(&session, "A", time, 1)], 1)],
    );
    assert_eq!(code(&report), Some(ErrorCode::InvalidContract));
}
#[test]
fn explicit_subscriptions_deduplicate_and_matching_still_consumes_universe() {
    let session = row(0);
    let mut h = Harness::default();
    h.host.initialize = Some(Box::new(|_, c, r| {
        r.subscribe(&[security("B"), security("B")], SubscriptionKind::Bar)?;
        r.subscribe(&[security("B")], SubscriptionKind::Bar)?;
        c.submit(&intent("A", TimeInForce::Gtc))?;
        Ok(())
    }));
    let report = h.run(
        config(Frequency::Day),
        calendar(1),
        vec![Source::new(
            vec![daily(&session, "A"), daily(&session, "B")],
            1,
        )],
    );
    assert_eq!(code(&report), None);
    assert_eq!(h.host.bar_boundaries.len(), 1);
    assert_eq!(h.host.bar_boundaries[0].securities, [security("B")]);
    assert_eq!(h.execution.fills.len(), 1);
    let mut ticks = Harness::default();
    ticks.host.initialize = Some(Box::new(|_, _, r| r.subscribe(&[], SubscriptionKind::Tick)));
    let report = ticks.run(
        config(Frequency::Tick),
        calendar(1),
        vec![Source::new(
            vec![tick(&session, "A", "a", local(&session, 600), 1)],
            1,
        )],
    );
    assert_eq!(code(&report), None);
    assert!(ticks.host.tick_keys.is_empty());
    assert_eq!(report.progress.completed_events, 1);
}
#[test]
fn lifecycle_and_duplicate_week_month_registrations_execute_once_per_registration() {
    let mut h = Harness::default();
    h.host.initialize = Some(Box::new(|_, _, r| {
        for (name, cadence, time) in [
            ("before", Cadence::Daily, ScheduleTime::BeforeOpen),
            ("last", Cadence::Weekly(-1), ScheduleTime::AfterClose),
            ("last", Cadence::Monthly(-1), ScheduleTime::AfterClose),
            ("first", Cadence::Weekly(1), ScheduleTime::AfterClose),
        ] {
            r.register(CallbackId::new(name)?, cadence, time)?;
        }
        Ok(())
    }));
    let report = h.run(config(Frequency::Day), calendar(3), vec![]);
    assert_eq!(code(&report), None);
    assert_eq!(
        h.host.calls.iter().filter(|c| *c == "initialize").count(),
        1
    );
    assert_eq!(
        h.host.calls.iter().filter(|c| *c == "before_open").count(),
        3
    );
    assert_eq!(
        h.host.calls.iter().filter(|c| *c == "after_close").count(),
        3
    );
    assert_eq!(
        h.host
            .scheduled_calls
            .iter()
            .map(|c| c.callback.as_str())
            .collect::<Vec<_>>(),
        ["before", "last", "last", "before", "first", "before"]
    );
    assert!(
        h.control
            .progress
            .windows(2)
            .all(|p| p[0].completed_events <= p[1].completed_events && p[0].now_ns <= p[1].now_ns)
    );
}
#[test]
fn after_close_day_order_expires_only_after_the_next_sessions_last_market() {
    let sessions = calendar(3);
    let mut h = Harness::default();
    h.matcher.suppress = true;
    let mut count = 0;
    h.host.after = Some(Box::new(move |_, c| {
        count += 1;
        if count == 1 {
            c.submit(&intent("A", TimeInForce::Day))?;
        }
        Ok(())
    }));
    let report = h.run(
        config(Frequency::Day),
        sessions.clone(),
        vec![Source::new(
            sessions.sessions().iter().map(|s| daily(s, "A")).collect(),
            1,
        )],
    );
    assert_eq!(code(&report), None);
    let order = &h.execution.orders["o1"];
    assert_eq!(order.effective_session, session_key("S2"));
    assert_eq!(order.status, OrderStatus::Expired);
    assert_eq!(
        h.matcher
            .seen
            .iter()
            .map(|(_, ids)| ids.clone())
            .collect::<Vec<_>>(),
        [vec![], vec!["o1".to_string()], vec![]]
    );
    let trace = h.host.trace.borrow();
    let expiry = trace.iter().position(|x| x == "expire:S2").unwrap();
    let after = trace
        .iter()
        .position(|x| x == &format!("after:{}", sessions.sessions()[1].after_close_ns))
        .unwrap();
    assert!(expiry < after);
    let expiry_notice = h
        .host
        .notices
        .iter()
        .find(|n| matches!(n, Notification::Order(o) if o.status == OrderStatus::Expired))
        .unwrap();
    assert!(
        matches!(expiry_notice, Notification::Order(o) if o.effective_session == session_key("S2"))
    );
}
#[test]
fn notification_generated_commands_are_nonrecursive_and_cannot_escape_limits() {
    let mut h = Harness::default();
    let depth = Rc::new(Cell::new(0));
    let max_depth = Rc::new(Cell::new(0));
    let observed = max_depth.clone();
    h.host.initialize = Some(Box::new(|_, c, _| {
        c.submit(&intent("A", TimeInForce::Gtc))?;
        Ok(())
    }));
    h.host.notice = Some(Box::new(move |_, _, c| {
        depth.set(depth.get() + 1);
        max_depth.set(max_depth.get().max(depth.get()));
        // A strategy swallowing resource errors still cannot extend this loop.
        let _ = c.submit(&intent("A", TimeInForce::Gtc));
        depth.set(depth.get() - 1);
        Ok(())
    }));
    let mut cfg = config(Frequency::Day);
    cfg.limits.max_notifications_per_boundary = 8;
    let report = h.run(cfg, calendar(1), vec![]);
    assert_eq!(code(&report), Some(ErrorCode::ResourceLimit));
    assert_eq!(observed.get(), 1);
    assert!(h.host.notices.len() <= 8);
    assert_eq!(h.host.finish_count, 1);
    assert_eq!(h.writer.finalized, 0);
    assert_eq!(h.writer.aborted, 1);
}
#[test]
fn cancellation_before_source_reads_and_during_tick_run_has_one_terminal_path() {
    let session = row(0);
    let source = Source::new(vec![daily(&session, "A")], 1);
    let reads = source.reads.clone();
    let mut early = Harness::default();
    early.control.cancelled.set(true);
    let report = early.run(config(Frequency::Day), calendar(1), vec![source]);
    assert!(matches!(
        report.outcome,
        RunOutcome::Cancelled { partial: false }
    ));
    assert_eq!(reads.get(), 0);
    assert_eq!(early.host.finish_count, 1);
    assert_eq!(early.data.closed, 1);
    let mut h = Harness::default();
    h.control.cancel_after_events = Some(1);
    let mut cfg = config(Frequency::Tick);
    cfg.limits.progress_every_events = 1;
    let events = (1..=50)
        .map(|n| tick(&session, "A", "a", local(&session, 600), n))
        .collect();
    let report = h.run(cfg, calendar(1), vec![Source::new(events, 1)]);
    assert!(matches!(
        report.outcome,
        RunOutcome::Cancelled { partial: true }
    ));
    assert_eq!(report.progress.completed_events, 1);
    assert_eq!(h.writer.aborted, 1);
    assert_eq!(h.writer.finalized, 0);
    assert!(h.control.checks < 200);
}
#[test]
fn strategy_exception_preserves_function_line_and_does_not_succeed() {
    let session = row(0);
    let mut h = Harness::default();
    h.host.bars = Some(Box::new(|_, _, _| {
        let mut e = failure(ErrorCode::StrategyError);
        e.scope.insert("function".into(), "handle_data".into());
        e.scope.insert("line".into(), "42".into());
        Err(e)
    }));
    let report = h.run(
        config(Frequency::Day),
        calendar(1),
        vec![Source::new(vec![daily(&session, "A")], 1)],
    );
    assert_eq!(code(&report), Some(ErrorCode::StrategyError));
    if let RunOutcome::Failed { error, partial } = report.outcome {
        assert!(partial);
        assert_eq!(error.scope["function"], "handle_data");
        assert_eq!(error.scope["line"], "42");
    } else {
        panic!("expected failed");
    }
    assert_eq!(h.host.finish_count, 1);
    assert_eq!(h.writer.aborted, 1);
    assert_eq!(h.writer.finalized, 0);
}
#[test]
fn future_request_rejects_before_port_and_same_ns_prefetch_leak_also_rejects() {
    let session = row(0);
    let time = local(&session, 600);
    let mut h = Harness::default();
    h.host.ticks = Some(Box::new(|_, view, _| {
        let future = ns(view.now_ns().get() + 1);
        assert_eq!(
            view.read(&request(future, Frequency::Tick))
                .unwrap_err()
                .code,
            ErrorCode::LookaheadForbidden
        );
        Ok(())
    }));
    let report = h.run(
        config(Frequency::Tick),
        calendar(1),
        vec![Source::new(vec![tick(&session, "A", "a", time, 1)], 1)],
    );
    assert_eq!(code(&report), None);
    assert_eq!(h.data.reads, 0);
    for leaked_time in [time, ns(time.get() + 1)] {
        let mut leak = Harness::default();
        leak.data.leaked = Some(tick(&session, "A", "a", leaked_time, 2).key());
        leak.host.ticks = Some(Box::new(|_, view, _| {
            view.read(&request(view.now_ns(), Frequency::Tick))?;
            Ok(())
        }));
        let report = leak.run(
            config(Frequency::Tick),
            calendar(1),
            vec![Source::new(vec![tick(&session, "A", "a", time, 1)], 1)],
        );
        assert_eq!(code(&report), Some(ErrorCode::LookaheadForbidden));
    }
}

#[test]
fn initialize_and_before_open_can_read_completed_history_but_not_todays_bar() {
    let session = row(0);
    let previous = tick(&session, "A", "history", ns(-1), 1).key();
    let today = daily(&session, "A").key();
    let mut h = Harness::default();
    h.data.future = vec![previous.clone(), today];
    h.host.initialize = Some(Box::new(move |view, _, _| {
        let frame = view.read(&request(view.now_ns(), Frequency::Day))?;
        assert_eq!(frame.keys.as_slice(), std::slice::from_ref(&previous));
        Ok(())
    }));
    let report = h.run(
        config(Frequency::Day),
        calendar(1),
        vec![Source::new(vec![daily(&session, "A")], 1)],
    );
    assert_eq!(code(&report), None);
    assert_eq!(h.data.reads, 1);
}
#[test]
fn dependency_change_at_final_check_or_next_chunk_cannot_commit_success() {
    let session = row(0);
    let mut h = Harness::default();
    h.data.change_at = Some(5); // final check after finish, not just a generation read
    let report = h.run(
        config(Frequency::Day),
        calendar(1),
        vec![Source::new(vec![daily(&session, "A")], 1)],
    );
    assert_eq!(code(&report), Some(ErrorCode::DataChanged));
    assert_eq!(h.writer.finalized, 0);
    let mut source = Source::new(
        vec![
            tick(&session, "A", "a", local(&session, 600), 1),
            tick(&session, "A", "a", local(&session, 601), 2),
        ],
        1,
    );
    source.fail_after = Some(1);
    let mut h = Harness::default();
    let report = h.run(config(Frequency::Tick), calendar(1), vec![source]);
    assert_eq!(code(&report), Some(ErrorCode::DataChanged));
    assert_eq!(h.writer.finalized, 0);
}
#[test]
fn result_credit_and_finalize_cancellation_keep_partial_results_and_one_outcome() {
    for mode in 0..3 {
        let mut h = Harness::default();
        h.host.initialize = Some(Box::new(|_, c, _| {
            c.submit(&intent("A", TimeInForce::Gtc))?;
            Ok(())
        }));
        match mode {
            0 => h.writer.credit_records = 0,
            1 => h.writer.credit_bytes = 1,
            _ => h.writer.fail_write = true,
        }
        let report = h.run(config(Frequency::Day), calendar(1), vec![]);
        assert_eq!(code(&report), Some(ErrorCode::ResultBudgetExceeded));
        assert_eq!(h.writer.finalized, 0);
        assert_eq!(h.writer.aborted, 1);
    }
    let mut h = Harness::default();
    h.writer.final_cancel = true;
    h.host.initialize = Some(Box::new(|_, c, _| {
        c.submit(&intent("A", TimeInForce::Gtc))?;
        Ok(())
    }));
    let report = h.run(config(Frequency::Day), calendar(1), vec![]);
    assert!(matches!(
        report.outcome,
        RunOutcome::Cancelled { partial: true }
    ));
    assert!(!h.writer.records.is_empty());
    assert_eq!(h.writer.finalized, 1);
    assert_eq!(h.writer.aborted, 1);
}
#[test]
fn bounded_commands_bar_groups_and_total_events_cannot_run_forever() {
    let mut h = Harness::default();
    h.host.initialize = Some(Box::new(|_, c, _| {
        for _ in 0..4 {
            let _ = c.submit(&intent("A", TimeInForce::Gtc));
        }
        Ok(())
    }));
    let mut cfg = config(Frequency::Day);
    cfg.limits.max_commands_per_boundary = 2;
    let report = h.run(cfg, calendar(1), vec![]);
    assert_eq!(code(&report), Some(ErrorCode::ResourceLimit));
    assert_eq!(h.execution.normalized.len(), 2);
    let session = row(0);
    let mut h = Harness::default();
    let mut cfg = config(Frequency::Day);
    cfg.limits.max_bar_events_per_boundary = 1;
    let report = h.run(
        cfg,
        calendar(1),
        vec![Source::new(
            vec![daily(&session, "A"), daily(&session, "B")],
            1,
        )],
    );
    assert_eq!(code(&report), Some(ErrorCode::ResourceLimit));
    assert!(h.host.bar_boundaries.is_empty());
    let mut h = Harness::default();
    let mut cfg = config(Frequency::Tick);
    cfg.limits.max_events = 1;
    let report = h.run(
        cfg,
        calendar(1),
        vec![Source::new(
            vec![
                tick(&session, "A", "a", local(&session, 600), 1),
                tick(&session, "A", "a", local(&session, 600), 2),
            ],
            1,
        )],
    );
    assert_eq!(code(&report), Some(ErrorCode::ResourceLimit));
    assert_eq!(report.progress.completed_events, 1);
}
#[test]
fn all_supported_bar_frequencies_flow_through_one_driver() {
    let session = row(0);
    for frequency in [
        Frequency::Minute,
        Frequency::FiveMinutes,
        Frequency::FifteenMinutes,
        Frequency::ThirtyMinutes,
        Frequency::Hour,
    ] {
        let mut h = Harness::default();
        let events = session
            .buckets(frequency)
            .unwrap()
            .take(2)
            .enumerate()
            .map(|(i, bucket)| bar(&session, "A", bucket.start_ns, bucket.end_ns, i as u64))
            .collect();
        let report = h.run(config(frequency), calendar(1), vec![Source::new(events, 1)]);
        assert_eq!(code(&report), None, "{frequency:?}");
        assert_eq!(h.host.bar_boundaries.len(), 2);
    }
}
#[test]
fn orders_from_expiry_notifications_are_assigned_next_session() {
    let sessions = calendar(2);
    let mut h = Harness::default();
    h.host.initialize = Some(Box::new(|_, c, _| {
        c.submit(&intent("A", TimeInForce::Day))?;
        Ok(())
    }));
    let mut submitted = false;
    h.host.notice = Some(Box::new(move |notice, _, c| {
        if !submitted
            && matches!(notice, Notification::Order(o) if o.status == OrderStatus::Expired)
        {
            submitted = true;
            c.submit(&intent("B", TimeInForce::Day))?;
        }
        Ok(())
    }));
    let report = h.run(config(Frequency::Day), sessions, vec![]);
    assert_eq!(code(&report), None);
    assert_eq!(
        h.execution.orders["o2"].effective_session,
        session_key("S2")
    );
    assert_eq!(h.execution.orders["o2"].status, OrderStatus::Expired);
}

#[test]
fn normalized_d01_run_config_keeps_calendar_reference_and_date_scope() {
    use qf_core::engine::{Engine, EngineLimits};
    use qf_core::run::RunConfig;
    let mut wire: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../contracts/examples/run_config.json"
    ))
    .unwrap();
    wire["start"] = serde_json::json!("2026-01-30");
    wire["end"] = serde_json::json!("2026-02-03");
    wire["reference_calendar"] = serde_json::json!("synthetic_clock_only");
    let run = RunConfig::from_json(&wire.to_string()).unwrap();
    assert!(
        Engine::<Source>::from_run(
            &run,
            "result".into(),
            EngineLimits::default(),
            calendar(3),
            vec![]
        )
        .is_ok()
    );
    wire["reference_calendar"] = serde_json::json!("other_calendar");
    let run = RunConfig::from_json(&wire.to_string()).unwrap();
    assert_eq!(
        Engine::<Source>::from_run(
            &run,
            "result".into(),
            EngineLimits::default(),
            calendar(3),
            vec![]
        )
        .err()
        .unwrap()
        .code,
        ErrorCode::InvalidRunConfig
    );
    wire["reference_calendar"] = serde_json::json!("synthetic_clock_only");
    wire["end"] = serde_json::json!("2026-02-02");
    let run = RunConfig::from_json(&wire.to_string()).unwrap();
    assert_eq!(
        Engine::<Source>::from_run(
            &run,
            "result".into(),
            EngineLimits::default(),
            calendar(3),
            vec![]
        )
        .err()
        .unwrap()
        .code,
        ErrorCode::InvalidRunConfig
    );
}

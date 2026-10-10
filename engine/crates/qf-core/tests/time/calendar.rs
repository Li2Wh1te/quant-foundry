use super::support::*;
use qf_core::clock::{
    Cadence, CallbackId, Registration, ScheduleTime, SessionCalendar, SimClock, SubscriptionKind,
};
use qf_core::run::Frequency;
use qf_core::{ErrorCode, QfResult};

#[test]
fn daily_weekly_monthly_counts_use_authoritative_full_period_ordinals() {
    let calendar = calendar(3);
    let mut registration = Registration::new(&[security("A")], Frequency::Day).unwrap();
    for (name, cadence) in [
        ("daily", Cadence::Daily),
        ("weekly_last", Cadence::Weekly(-1)),
        ("monthly_last", Cadence::Monthly(-1)),
        ("weekly_1", Cadence::Weekly(1)),
        ("monthly_1", Cadence::Monthly(1)),
        ("monthly_99", Cadence::Monthly(99)),
    ] {
        registration
            .register(
                CallbackId::new(name).unwrap(),
                cadence,
                ScheduleTime::AfterClose,
            )
            .unwrap();
    }
    let names: Vec<Vec<_>> = calendar
        .sessions()
        .iter()
        .map(|s| {
            registration
                .for_session(s)
                .unwrap()
                .iter()
                .map(|c| c.callback.as_str().to_owned())
                .collect()
        })
        .collect();
    assert_eq!(
        names,
        vec![
            vec!["daily", "weekly_last", "monthly_last"],
            vec!["daily", "weekly_1", "monthly_1"],
            vec!["daily"]
        ]
    );
    // Starting mid-month cannot reinterpret that first run day as trading day 1.
    let partial = SessionCalendar::new(
        "synthetic_clock_only".into(),
        vec![row(2)],
        Some(session_key("NEXT")),
    )
    .unwrap();
    assert_eq!(
        registration
            .for_session(&partial.sessions()[0])
            .unwrap()
            .len(),
        1
    );
}
#[test]
fn repeated_schedule_calls_preserve_registration_order_subscriptions_deduplicate() {
    let mut registration =
        Registration::new(&[security("A"), security("B")], Frequency::Day).unwrap();
    for name in ["same", "middle", "same"] {
        registration
            .register(
                CallbackId::new(name).unwrap(),
                Cadence::Daily,
                ScheduleTime::AfterClose,
            )
            .unwrap();
    }
    registration
        .subscribe(&[security("B"), security("B")], SubscriptionKind::Bar)
        .unwrap();
    registration
        .subscribe(&[security("B")], SubscriptionKind::Bar)
        .unwrap();
    assert!(!registration.subscribed(&security("A"), SubscriptionKind::Bar));
    assert!(registration.subscribed(&security("B"), SubscriptionKind::Bar));
    assert!(!registration.subscribed(&security("A"), SubscriptionKind::Tick));
    let calls = registration.for_session(&row(0)).unwrap();
    assert_eq!(
        calls
            .iter()
            .map(|c| c.callback.as_str())
            .collect::<Vec<_>>(),
        ["same", "middle", "same"]
    );
    registration.seal();
    assert!(registration.subscribe(&[], SubscriptionKind::Bar).is_err());
    assert!(
        registration
            .register(
                CallbackId::new("late").unwrap(),
                Cadence::Daily,
                ScheduleTime::AfterClose
            )
            .is_err()
    );
}
#[test]
fn minute_hour_buckets_never_cross_lunch_and_partial_tail_is_explicit() {
    let session = row(0);
    for frequency in [
        Frequency::Minute,
        Frequency::FiveMinutes,
        Frequency::FifteenMinutes,
        Frequency::ThirtyMinutes,
        Frequency::Hour,
    ] {
        let buckets: Vec<_> = session.buckets(frequency).unwrap().collect();
        assert!(buckets.iter().all(|b| b.start_ns < b.end_ns
            && (b.end_ns <= local(&session, 690) || b.start_ns >= local(&session, 780))));
        assert!(buckets.iter().all(|b| b.full_interval));
    }
    let mut shortened = session.clone();
    shortened.bar_windows[1].end_ns = local(&session, 895);
    shortened.validate().unwrap();
    let buckets: Vec<_> = shortened.buckets(Frequency::Hour).unwrap().collect();
    assert!(!buckets.last().unwrap().full_interval);
    assert_eq!(buckets.len(), 4);
}
#[test]
fn daily_intraday_lunch_and_incompatible_precision_schedules_reject() {
    let mut daily = Registration::new(&[security("A")], Frequency::Day).unwrap();
    assert_eq!(
        daily
            .register(
                CallbackId::new("intraday").unwrap(),
                Cadence::Daily,
                "10:15".parse().unwrap()
            )
            .unwrap_err()
            .code,
        ErrorCode::CapabilityUnavailable
    );
    for (frequency, time) in [
        (Frequency::Minute, "12:00"),
        (Frequency::Hour, "10:15"),
        (Frequency::FiveMinutes, "10:16"),
    ] {
        let mut r = Registration::new(&[security("A")], frequency).unwrap();
        r.register(
            CallbackId::new("intraday").unwrap(),
            Cadence::Daily,
            time.parse().unwrap(),
        )
        .unwrap();
        assert_eq!(
            r.for_session(&row(0)).unwrap_err().code,
            ErrorCode::CapabilityUnavailable
        );
    }
    for time in ["24:00", "10:60", "1:00", "xx:yy"] {
        assert!(time.parse::<ScheduleTime>().is_err());
    }
    for n in [0, -2, i16::MIN] {
        assert!(
            daily
                .register(
                    CallbackId::new("bad").unwrap(),
                    Cadence::Weekly(n),
                    ScheduleTime::AfterClose
                )
                .is_err()
        );
    }
    let mut auction = Registration::new(&[security("A")], Frequency::Tick).unwrap();
    auction
        .register(
            CallbackId::new("opening_call").unwrap(),
            Cadence::Daily,
            "09:20".parse().unwrap(),
        )
        .unwrap();
    assert_eq!(
        auction.for_session(&row(0)).unwrap()[0].time_ns,
        local(&row(0), 560)
    );
}
#[test]
fn invalid_calendar_gaps_ordinals_and_event_windows_fail_closed() {
    let mut wrong = row(1);
    wrong.month.trading_day = 2;
    assert!(
        SessionCalendar::new("synthetic_clock_only".into(), vec![row(0), wrong], None).is_err()
    );
    let session = row(0);
    assert!(
        session
            .validate_event(
                &bar(&session, "A", local(&session, 689), local(&session, 781), 1),
                Frequency::Minute
            )
            .is_err()
    );
    assert!(
        session
            .validate_event(
                &tick(&session, "A", "a", local(&session, 720), 1),
                Frequency::Tick
            )
            .is_err()
    );
    assert_eq!(
        calendar(1)
            .effective_session(0, session.after_close_ns, true)
            .unwrap(),
        session_key("NEXT")
    );
    let no_next = SessionCalendar::new("synthetic_clock_only".into(), vec![session], None).unwrap();
    assert!(
        no_next
            .effective_session(0, row(0).after_close_ns, true)
            .is_err()
    );
}
#[test]
fn simulated_clock_preserves_nanoseconds_and_refuses_time_reversal() -> QfResult<()> {
    let time = ns(1_767_225_600_000_000_123);
    let mut clock = SimClock::new(time);
    clock.advance(time)?;
    assert_eq!(clock.visibility().now_ns, time);
    assert_eq!(
        clock.advance(ns(time.get() - 1)).unwrap_err().code,
        ErrorCode::InvalidContract
    );
    Ok(())
}

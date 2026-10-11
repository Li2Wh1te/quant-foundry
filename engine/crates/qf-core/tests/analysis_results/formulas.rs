use qf_core::analysis::*;
use qf_core::matching::Fill;
use qf_core::orders::Side;
use qf_core::run::RunConfig;
use qf_core::types::*;
fn config() -> RunConfig {
    serde_json::from_str(include_str!(
        "../../../../../contracts/examples/run_config.json"
    ))
    .unwrap()
}
fn num(m: &Metric) -> f64 {
    match m {
        Metric::Available { value } => value.get(),
        _ => panic!("{m:?}"),
    }
}
fn reason(m: &Metric) -> &str {
    match m {
        Metric::Unavailable { reason } => reason,
        _ => panic!("{m:?}"),
    }
}
fn near(m: &Metric, n: f64) {
    assert!((num(m) - n).abs() < 1e-11, "{} != {n}", num(m));
}
fn make(n: u64) -> Performance {
    let mut value = serde_json::to_value(config()).unwrap();
    value["initial_cash"] = serde_json::json!("100");
    Performance::from_run(&serde_json::from_value(value).unwrap(), "fixture", n).unwrap()
}
fn sample(p: &mut Performance, i: u64, value: Option<&str>) -> EquityPoint {
    p.session(
        i,
        &SessionKey::new(format!("S{i}")).unwrap(),
        Nanoseconds::new(i as i64 + 1),
        value.map(|x| x.parse().unwrap()),
        None,
        None,
    )
    .unwrap()
}
#[test]
fn independent_three_returns_ddof_drawdown_fees_and_turnover() {
    let mut p = make(3);
    p.begin(Nanoseconds::new(0), None).unwrap();
    for (i, e) in ["110", "88", "105.6"].iter().enumerate() {
        sample(&mut p, i as u64, Some(e));
    }
    for (side, q, price) in [(Side::Buy, 100, "10"), (Side::Sell, 40, "12")] {
        p.fill(&Fill {
            trade_id: "fixture".into(),
            order_id: "order".into(),
            security: SecurityKey::new("A").unwrap(),
            side,
            quantity: Quantity::new(q).unwrap(),
            price: price.parse().unwrap(),
            fee: "1".parse().unwrap(),
            execution_time_ns: Nanoseconds::new(1),
        })
        .unwrap();
    }
    let s = p.summary(None);
    near(&s.cumulative_return, 0.056);
    near(&s.maximum_drawdown, 0.2);
    // r=[1/10,-1/5,1/5]; mean=1/30; sample variance=13/300.
    near(&s.volatility, (13.0_f64 / 300.0).sqrt() * 252.0_f64.sqrt());
    near(
        &s.sharpe,
        (1.0 / 30.0) / (13.0_f64 / 300.0).sqrt() * 252.0_f64.sqrt(),
    );
    near(&s.annualized_return, 1.056_f64.powf(84.0) - 1.0);
    near(&s.turnover, 1480.0 / (2.0 * 101.2));
    assert_eq!(s.trades.fill_fees.to_string(), "2");
    assert_eq!(s.trades.total_notional.to_string(), "1480");
    assert_eq!(s.trades.fills, 2);
    assert_eq!(reason(&s.benchmark_return), "NO_BENCHMARK");
}
#[test]
fn empty_single_flat_and_zero_denominator_are_distinct() {
    let mut p = make(0);
    p.begin(Nanoseconds::new(0), None).unwrap();
    let s = p.summary(None);
    assert_eq!(reason(&s.cumulative_return), "NO_SESSION_SAMPLES");
    let mut p = make(1);
    p.begin(Nanoseconds::new(0), None).unwrap();
    sample(&mut p, 0, Some("100"));
    let s = p.summary(None);
    near(&s.cumulative_return, 0.0);
    assert_eq!(reason(&s.volatility), "INSUFFICIENT_RETURN_SAMPLES");
    let mut p = make(2);
    p.begin(Nanoseconds::new(0), None).unwrap();
    sample(&mut p, 0, Some("100"));
    sample(&mut p, 1, Some("100"));
    let s = p.summary(None);
    near(&s.volatility, 0.0);
    assert_eq!(reason(&s.sharpe), "ZERO_VOLATILITY");
    assert_eq!(reason(&s.trades.mean_fill_notional), "NO_TRADES");
    let mut p = make(2);
    p.begin(Nanoseconds::new(0), None).unwrap();
    sample(&mut p, 0, Some("0"));
    sample(&mut p, 1, Some("0"));
    let s = p.summary(None);
    near(&s.cumulative_return, -1.0);
    near(&s.annualized_return, -1.0);
    near(&s.maximum_drawdown, 1.0);
    assert_eq!(reason(&s.sharpe), "NONPOSITIVE_DENOMINATOR");
}
#[test]
fn missing_close_breaks_both_segments_and_all_whole_interval_metrics() {
    let mut p = make(4);
    p.begin(Nanoseconds::new(0), None).unwrap();
    sample(&mut p, 0, Some("110"));
    let missing = sample(&mut p, 1, None);
    let following = sample(&mut p, 2, Some("120"));
    sample(&mut p, 3, Some("132"));
    assert_eq!(reason(&missing.session_return), "MISSING_EQUITY_SEGMENT");
    assert_eq!(reason(&following.session_return), "MISSING_EQUITY_SEGMENT");
    let s = p.summary(None);
    assert_eq!(s.valid_return_samples, 2);
    assert_eq!(s.missing_equity_sessions, 1);
    for m in [
        &s.cumulative_return,
        &s.maximum_drawdown,
        &s.annualized_return,
        &s.volatility,
        &s.sharpe,
        &s.turnover,
    ] {
        assert_eq!(reason(m), "MISSING_EQUITY");
    }
}
#[test]
fn repeating_return_tiny_decimal_gap_and_invalid_sampling() {
    let mut p = make(2);
    p.begin(Nanoseconds::new(0), None).unwrap();
    sample(&mut p, 0, Some("300"));
    let point = sample(&mut p, 1, Some("100"));
    near(&point.session_return, -2.0 / 3.0);
    assert!(
        p.session(
            1,
            &SessionKey::new("S").unwrap(),
            Nanoseconds::new(3),
            Some(Money::ONE),
            None,
            None
        )
        .is_err()
    );
    let mut p = make(1);
    p.begin(Nanoseconds::new(0), None).unwrap();
    let point = sample(&mut p, 0, Some("100.000000000000000001"));
    assert_eq!(point.equity.unwrap().to_string(), "100.000000000000000001");
    assert!((num(&point.session_return) - 1e-20).abs() < 1e-30);
}
#[test]
fn minute_tick_and_fine_curves_do_not_change_session_annualization() {
    let mut results = Vec::new();
    for freq in ["1d", "1m", "tick"] {
        let mut c = serde_json::to_value(config()).unwrap();
        c["initial_cash"] = serde_json::json!("100");
        c["frequency"] = serde_json::json!(freq);
        c["execution_model"] = serde_json::json!(if freq == "tick" {
            "trade_tick_v1"
        } else {
            "bar_next_interval_v1"
        });
        let mut p =
            Performance::from_run(&serde_json::from_value(c).unwrap(), "fixture", 2).unwrap();
        p.begin(Nanoseconds::new(0), None).unwrap();
        for _ in 0..10000 {
            let _ = p.bar_curve(
                &SessionKey::new("S").unwrap(),
                Nanoseconds::new(1),
                Some("999".parse().unwrap()),
            );
        }
        sample(&mut p, 0, Some("110"));
        sample(&mut p, 1, Some("100"));
        results.push(p.summary(None));
    }
    assert_eq!(results[0].volatility, results[1].volatility);
    assert_eq!(results[1].sharpe, results[2].sharpe);
}
#[test]
fn benchmark_must_cover_exact_origin_each_close_and_terminal() {
    let mut c = serde_json::to_value(config()).unwrap();
    c["benchmark"] = serde_json::json!("BENCH");
    c["initial_cash"] = serde_json::json!("100");
    let config: RunConfig = serde_json::from_value(c).unwrap();
    let bp = |time, value: &str| {
        Some(BenchmarkPoint {
            time_ns: Nanoseconds::new(time),
            value: Some(value.parse().unwrap()),
            unavailable_reason: None,
        })
    };
    let mut p = Performance::from_run(&config, "fixture", 2).unwrap();
    p.begin(Nanoseconds::new(0), bp(0, "50")).unwrap();
    for (i, e, b) in [(0, "110", "55"), (1, "120", "60")] {
        let point = p
            .session(
                i,
                &SessionKey::new(format!("S{i}")).unwrap(),
                Nanoseconds::new(i as i64 + 1),
                Some(e.parse().unwrap()),
                None,
                bp(i as i64 + 1, b),
            )
            .unwrap();
        near(
            &point.benchmark_cumulative_return,
            if i == 0 { 0.1 } else { 0.2 },
        );
        near(&point.cumulative_excess_return, 0.0);
        assert_eq!(point.benchmark_value.unwrap().to_string(), b);
    }
    let s = p.summary(None);
    near(&s.benchmark_return, 0.2);
    near(&s.cumulative_excess_return, 0.0);
    let mut p = Performance::from_run(&config, "fixture", 1).unwrap();
    p.begin(Nanoseconds::new(0), bp(9, "50")).unwrap();
    p.session(
        0,
        &SessionKey::new("S").unwrap(),
        Nanoseconds::new(1),
        Some("110".parse().unwrap()),
        None,
        bp(1, "55"),
    )
    .unwrap();
    assert_eq!(
        reason(&p.summary(None).benchmark_return),
        "BENCHMARK_INTERVAL_MISMATCH_OR_INVALID_VALUE"
    );
    let mut p = Performance::from_run(&config, "fixture", 2).unwrap();
    p.begin(Nanoseconds::new(0), bp(0, "50")).unwrap();
    p.session(
        0,
        &SessionKey::new("S0").unwrap(),
        Nanoseconds::new(1),
        Some("110".parse().unwrap()),
        None,
        None,
    )
    .unwrap();
    p.session(
        1,
        &SessionKey::new("S1").unwrap(),
        Nanoseconds::new(2),
        Some("120".parse().unwrap()),
        None,
        bp(2, "60"),
    )
    .unwrap();
    assert_eq!(
        reason(&p.summary(None).benchmark_return),
        "MISSING_BENCHMARK"
    );
}
#[test]
fn configured_annual_basis_and_nonzero_risk_free_have_independent_oracle() {
    let mut c = serde_json::to_value(config()).unwrap();
    c["initial_cash"] = serde_json::json!("100");
    c["annualization_sessions"] = serde_json::json!(2);
    c["risk_free_rate"] = serde_json::json!("0.21");
    let mut p = Performance::from_run(&serde_json::from_value(c).unwrap(), "fixture", 2).unwrap();
    p.begin(Nanoseconds::new(0), None).unwrap();
    sample(&mut p, 0, Some("110"));
    sample(&mut p, 1, Some("143"));
    let s = p.summary(None);
    // daily rf=sqrt(1.21)-1=.1; excess=[0,.2], sample std=sqrt(.02).
    near(&s.sharpe, 1.0);
    near(&s.volatility, 0.2);
    near(&s.annualized_return, 0.43);
    assert_eq!(s.policy.annualization_sessions, 2);
    assert_eq!(s.policy.risk_free_rate.to_string(), "0.21");
}

#[test]
fn near_total_loss_and_near_minus_one_risk_free_keep_positive_growth() {
    let mut c = serde_json::to_value(config()).unwrap();
    c["initial_cash"] = serde_json::json!("100");
    c["annualization_sessions"] = serde_json::json!(1);
    let mut p =
        Performance::from_run(&serde_json::from_value(c.clone()).unwrap(), "fixture", 252).unwrap();
    p.begin(Nanoseconds::new(0), None).unwrap();
    for i in 0..252 {
        sample(&mut p, i, Some("0.0000000000000000000000000001"));
    }
    near(
        &p.summary(None).annualized_return,
        1e-30_f64.powf(1.0 / 252.0) - 1.0,
    );
    c["annualization_sessions"] = serde_json::json!(252);
    c["risk_free_rate"] = serde_json::json!("-0.9999999999999999999999999999");
    let mut p = Performance::from_run(&serde_json::from_value(c).unwrap(), "fixture", 2).unwrap();
    p.begin(Nanoseconds::new(0), None).unwrap();
    sample(&mut p, 0, Some("110"));
    sample(&mut p, 1, Some("132"));
    // Annual growth is 10^-28, so session growth is 10^(-28/252)=10^(-1/9).
    let rf_daily = 10.0_f64.powf(-1.0 / 9.0) - 1.0;
    near(
        &p.summary(None).sharpe,
        (0.15 - rf_daily) / 0.005_f64.sqrt() * 252.0_f64.sqrt(),
    );
}
#[test]
fn fine_curve_peak_is_separate_from_default_daily_drawdown() {
    let mut p = make(1);
    p.begin(Nanoseconds::new(0), None).unwrap();
    let session = SessionKey::new("fixture").unwrap();
    let fine = p.bar_curve(&session, Nanoseconds::new(1), Some("200".parse().unwrap()));
    near(&fine.drawdown, 0.0);
    let close = p
        .session(
            0,
            &session,
            Nanoseconds::new(2),
            Some("100".parse().unwrap()),
            None,
            None,
        )
        .unwrap();
    near(&close.drawdown, -0.5);
    near(&p.summary(None).maximum_drawdown, 0.0);
}

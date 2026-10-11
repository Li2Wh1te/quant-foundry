use qf_core::ErrorCode;
use qf_core::analysis::indicators::{self as i, Status};
use qf_core::data::views::{
    Boundary, MAX_VIEW_ROWS, ReadView, compare_decimal, exact_decimal_text,
};
use qf_core::types::Nanoseconds;
use std::collections::BTreeMap;

fn close(a: f64, b: f64) {
    assert!((a - b).abs() < 1e-10, "{a} != {b}");
}
#[test]
fn sma_seed_ema_and_sample_standard_deviation() {
    let input = [Some(1.), Some(2.), Some(3.), Some(4.), Some(5.)];
    let sma = i::sma(&input, 3).unwrap();
    assert_eq!(sma[1].status, Status::Warmup);
    close(sma[2].value.unwrap(), 2.);
    close(sma[4].value.unwrap(), 4.);
    let ema = i::ema(&input, 3).unwrap();
    close(ema[2].value.unwrap(), 2.);
    close(ema[3].value.unwrap(), 3.);
    close(ema[4].value.unwrap(), 4.);
    let std = i::rolling_std(&input, 3, 1).unwrap();
    close(std[2].value.unwrap(), 1.);
    close(
        i::rolling_std(&input, 3, 0).unwrap()[2].value.unwrap(),
        (2.0_f64 / 3.).sqrt(),
    );
}
#[test]
fn wilder_rsi_and_atr_independent_small_oracles() {
    let rsi = i::rsi(&[Some(10.), Some(12.), Some(11.), Some(13.), Some(12.)], 2).unwrap();
    assert_eq!(rsi[1].status, Status::Warmup);
    close(rsi[2].value.unwrap(), 200. / 3.);
    close(rsi[3].value.unwrap(), 600. / 7.);
    close(rsi[4].value.unwrap(), 600. / 11.);
    let atr = i::atr(
        &[Some(11.), Some(13.), Some(12.), Some(14.)],
        &[Some(9.), Some(11.), Some(10.), Some(12.)],
        &[Some(10.), Some(12.), Some(11.), Some(13.)],
        2,
    )
    .unwrap();
    // TR = 2,3,2,3; Wilder seed 2.5, then 2.25 and 2.625.
    close(atr[1].value.unwrap(), 2.5);
    close(atr[2].value.unwrap(), 2.25);
    close(atr[3].value.unwrap(), 2.625);
}
#[test]
fn missing_samples_reset_seed_and_macd_signal_has_separate_warmup() {
    let input = [
        Some(1.),
        Some(2.),
        None,
        Some(4.),
        Some(5.),
        Some(6.),
        Some(7.),
    ];
    let ema = i::ema(&input, 3).unwrap();
    assert_eq!(ema[2].status, Status::Missing);
    assert_eq!(ema[4].status, Status::Missing);
    close(ema[5].value.unwrap(), 5.);
    let line = i::macd(&[Some(1.), Some(2.), Some(3.), Some(4.), Some(5.)], 2, 3, 2).unwrap();
    close(line.macd[2].value.unwrap(), 0.5);
    assert_eq!(line.signal[2].status, Status::Warmup);
    close(line.signal[3].value.unwrap(), 0.5);
    close(line.histogram[4].value.unwrap(), 0.);
}
#[test]
fn recursive_gap_reseeds_wilder_and_both_macd_emas_and_signal() {
    let input = [
        Some(1.),
        Some(2.),
        Some(3.),
        None,
        Some(10.),
        Some(12.),
        Some(11.),
        Some(13.),
        Some(12.),
        Some(14.),
    ];
    let rsi = i::rsi(&input, 2).unwrap();
    assert_eq!(rsi[5].status, Status::Missing);
    close(rsi[6].value.unwrap(), 200. / 3.);
    close(rsi[7].value.unwrap(), 600. / 7.);
    let atr = i::atr(
        &[
            Some(2.),
            Some(3.),
            Some(4.),
            None,
            Some(11.),
            Some(13.),
            Some(12.),
            Some(14.),
        ],
        &[
            Some(0.),
            Some(1.),
            Some(2.),
            None,
            Some(9.),
            Some(11.),
            Some(10.),
            Some(12.),
        ],
        &input[..8],
        2,
    )
    .unwrap();
    assert_eq!(atr[4].status, Status::Missing);
    close(atr[5].value.unwrap(), 2.5);
    close(atr[6].value.unwrap(), 2.25);
    close(atr[7].value.unwrap(), 2.625);
    let macd = i::macd(&input, 2, 3, 2).unwrap();
    assert_eq!(macd.macd[5].status, Status::Missing);
    close(macd.macd[6].value.unwrap(), 0.);
    assert_eq!(macd.signal[6].status, Status::Missing);
    close(macd.signal[7].value.unwrap(), 1. / 6.);
    close(macd.histogram[7].value.unwrap(), 1. / 6.);
    close(macd.signal[8].value.unwrap(), 7. / 54.);
    close(macd.histogram[8].value.unwrap(), -1. / 54.);
    close(macd.histogram[9].value.unwrap(), 13. / 162.);
}
#[test]
fn equal_ranks_average_and_unknown_values_remain_missing() {
    let input = BTreeMap::from([
        ("B".into(), Some(10.)),
        ("A".into(), Some(10.)),
        ("D".into(), None),
        ("C".into(), Some(20.)),
    ]);
    let rank = i::rank(&input, true).unwrap();
    close(rank["A"].value.unwrap(), 1.5);
    close(rank["B"].value.unwrap(), 1.5);
    close(rank["C"].value.unwrap(), 3.);
    assert_eq!(rank["D"].status, Status::Missing);
    assert!(i::sma(&[Some(f64::INFINITY)], 1).is_err());
    assert!(i::sma(&[Some(1.)], 0).is_err());
    assert!(i::rolling_std(&[Some(1.)], 1, 1).is_err());
}
#[test]
fn exact_research_decimal_comparison_exceeds_execution_range() {
    use std::cmp::Ordering::*;
    let a = "123456789012345678901234567890123456789.123456789012345678901";
    assert!(exact_decimal_text(a));
    assert_eq!(compare_decimal(a, a), Equal);
    assert_eq!(compare_decimal("10", "2"), Greater);
    assert_eq!(compare_decimal("-10.1", "-10.01"), Less);
    assert_eq!(compare_decimal("-0.0", "0"), Equal);
    assert_eq!(compare_decimal("0.1", "0.10"), Equal);
    assert!(!exact_decimal_text("NaN"));
    assert!(!exact_decimal_text("01"));
    assert!(!exact_decimal_text("1e3"));
}
#[test]
fn expired_callback_lease_is_shared_and_terminal() {
    let mut view = ReadView::new(
        Boundary {
            now_ns: Nanoseconds::new(123),
            market_through: None,
        },
        10,
        65536,
    )
    .unwrap();
    let lease = view.lease();
    view.check().unwrap();
    view.expire();
    assert_eq!(lease.check().unwrap_err().code, ErrorCode::InvalidContract);
    assert_eq!(view.reset().unwrap_err().code, ErrorCode::InvalidContract);
    assert!(
        ReadView::new(
            Boundary {
                now_ns: Nanoseconds::new(123),
                market_through: None
            },
            MAX_VIEW_ROWS + 1,
            65536
        )
        .is_err()
    );
}

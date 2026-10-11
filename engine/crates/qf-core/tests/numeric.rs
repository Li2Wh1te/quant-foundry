use qf_core::ErrorCode;
use qf_core::types::{
    ExactDecimal as D, Nanoseconds, Price, Quantity as Q, QuantityStep as S, RoundingPolicy as R,
};
fn d(text: &str) -> D {
    text.parse().unwrap()
}
fn q(value: i64) -> Q {
    Q::new(value).unwrap()
}

#[test]
fn exact_input_and_real_range_boundaries() {
    let max = d("79228162514264337593543950335");
    assert_eq!(d("79228162514264337593543950335.00000"), max);
    assert_eq!(max.round(28, R::HalfEven).unwrap(), max);
    assert_eq!(max.checked_mul(D::ONE).unwrap(), max);
    assert_eq!(
        max.checked_add(D::ONE).unwrap_err().code,
        ErrorCode::NumericRangeUnsupported
    );
    assert!(max.checked_mul(d("1.01")).is_err());
    for invalid in [
        "79228162514264337593543950336",
        "0.00000000000000000000000000001",
        "NaN",
        "nan",
        "inf",
        "Infinity",
        "-Infinity",
        "1e-2",
        "01",
        "+1",
        " 1",
        "1.",
        ".1",
        "1_000",
        "1.0.0",
    ] {
        assert!(invalid.parse::<D>().is_err(), "accepted {invalid}");
    }
    assert_eq!(
        d("0.00000000000000000000000000010"),
        d("0.0000000000000000000000000001")
    );
    assert!("0".parse::<Price>().is_err());
    assert!("-1".parse::<Price>().is_err());
    assert!("79228162514264337593543950336".parse::<Price>().is_err());
}

#[test]
fn exact_arithmetic_cannot_hide_storage_rounding() {
    assert_eq!(d("0.1").checked_mul(d("0.1")).unwrap(), d("0.01"));
    assert_eq!(d("0.1").checked_add(d("0.01")).unwrap(), d("0.11"));
    assert_eq!(
        d("10000000000000000000000000000")
            .checked_add(d("-9999999999999999999999999999"))
            .unwrap(),
        D::ONE
    );
    let tiny = d("0.0000000000000000000000000001");
    assert!(tiny.checked_mul(d("0.1")).is_err());
    assert!(
        d("7922816251426433759354395033.5")
            .checked_add(d("0.01"))
            .is_err()
    );
    // Explicit rounded multiplication is permitted at the declared boundary.
    assert_eq!(
        tiny.mul_rounded(d("0.1"), 28, R::HalfEven).unwrap(),
        D::ZERO
    );
    assert_eq!(
        d("99999999999999.99999999999999")
            .mul_rounded(d("0.0000000000001"), 12, R::HalfEven)
            .unwrap(),
        d("10")
    );
}

#[test]
fn half_even_rounding_including_negative_and_cent_boundaries() {
    for (input, expected) in [
        ("0.005", "0.00"),
        ("0.015", "0.02"),
        ("0.025", "0.02"),
        ("-0.005", "0.00"),
        ("-0.015", "-0.02"),
        ("0.105", "0.10"),
        ("0.115", "0.12"),
        ("0.0149999999999999999999999999", "0.01"),
        ("0.0150000000000000000000000001", "0.02"),
    ] {
        assert_eq!(
            d(input).round(2, R::HalfEven).unwrap().to_string(),
            expected,
            "{input}"
        );
    }
    assert_eq!(d("0.199").round(2, R::TowardZero).unwrap(), d("0.19"));
    assert_eq!(d("-0.199").round(2, R::TowardZero).unwrap(), d("-0.19"));
}

#[test]
fn repeating_average_and_residual_cost_are_defined() {
    assert_eq!(
        D::ONE
            .div_rounded(d("3"), 12, R::HalfEven)
            .unwrap()
            .to_string(),
        "0.333333333333"
    );
    assert_eq!(
        d("-2")
            .div_rounded(d("3"), 12, R::HalfEven)
            .unwrap()
            .to_string(),
        "-0.666666666667"
    );
    assert!(D::ONE.div_rounded(D::ZERO, 12, R::HalfEven).is_err());
    let (first, remaining) = D::release_cost(D::ONE, q(3), q(1)).unwrap();
    let (second, remaining) = D::release_cost(remaining, q(2), q(1)).unwrap();
    let (last, remaining) = D::release_cost(remaining, q(1), q(1)).unwrap();
    assert_eq!(
        first
            .checked_add(second)
            .unwrap()
            .checked_add(last)
            .unwrap(),
        D::ONE
    );
    assert_eq!(remaining, D::ZERO);
    let (released, remaining) = D::release_cost(d("1001"), q(100), q(40)).unwrap();
    assert_eq!(released, d("400.4"));
    assert_eq!(remaining, d("600.6"));
    assert_eq!(
        d("480")
            .checked_sub(d("1"))
            .unwrap()
            .checked_sub(released)
            .unwrap(),
        d("78.6")
    );
    assert!(D::release_cost(D::ONE, q(1), q(2)).is_err());
}

#[test]
fn quantity_rationals_do_not_round_targets_up() {
    assert_eq!(
        D::legal_quantity(q(3), d("2"), d("3"), S::new(1).unwrap()).unwrap(),
        q(2)
    );
    assert_eq!(
        D::legal_quantity(
            q(3),
            d("0.6666666666666666666666666666"),
            D::ONE,
            S::new(1).unwrap()
        )
        .unwrap(),
        q(1)
    );
    assert_eq!(
        D::legal_quantity(q(301), d("1"), d("3"), S::new(100).unwrap()).unwrap(),
        q(100)
    );
    assert_eq!(
        D::legal_quantity(q(299), d("1"), d("3"), S::new(100).unwrap()).unwrap(),
        q(0)
    );
    assert!(Q::new(-1).is_err());
    assert!(S::new(0).is_err());
    assert!(q(i64::MAX).checked_add(q(1)).is_err());
    assert!(q(0).checked_sub(q(1)).is_err());
    assert!(D::legal_quantity(q(i64::MAX), d("2"), D::ONE, S::new(1).unwrap()).is_err());
    // Cancellation of decimal scales stays bounded even at extreme exponents.
    let tiny = d("0.0000000000000000000000000001");
    assert_eq!(
        D::legal_quantity(q(i64::MAX), tiny, tiny, S::new(1).unwrap()).unwrap(),
        q(i64::MAX)
    );
}

#[test]
fn json_never_uses_float_money_or_time() {
    for invalid in ["0.1", "true", "null", "\"NaN\""] {
        assert!(serde_json::from_str::<D>(invalid).is_err());
    }
    for invalid in ["-1", "true", "1.5", "9223372036854775808"] {
        assert!(serde_json::from_str::<Q>(invalid).is_err());
    }
    for value in [i64::MIN, 1767225600000000123, i64::MAX] {
        let time = Nanoseconds::new(value);
        let json = serde_json::to_string(&time).unwrap();
        assert_eq!(json, format!("\"{value}\""));
        assert_eq!(serde_json::from_str::<Nanoseconds>(&json).unwrap(), time);
    }
    for invalid in [
        "1767225600000000123",
        "\"9223372036854775808\"",
        "\"+1\"",
        "\"01\"",
    ] {
        assert!(serde_json::from_str::<Nanoseconds>(invalid).is_err());
    }
}

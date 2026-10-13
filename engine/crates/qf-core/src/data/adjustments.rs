//! Pure price arithmetic shared by current display and strict S3 research.
//! Callers retain authority for identity, factor coverage, anchor and time.
use crate::QfResult;
use crate::types::{ExactDecimal, RoundingPolicy};

/// Existing S3 formula and numeric range: exact multiply, 12-place half-even.
pub fn adjusted_price(
    price: ExactDecimal,
    factor: ExactDecimal,
    anchor: ExactDecimal,
) -> QfResult<ExactDecimal> {
    price
        .checked_mul(factor)?
        .div_rounded(anchor, 12, RoundingPolicy::HalfEven)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn independent_contract_example_and_half_even() {
        let transform = |price: &str, factor: &str, anchor: &str| {
            adjusted_price(
                price.parse().unwrap(),
                factor.parse().unwrap(),
                anchor.parse().unwrap(),
            )
            .unwrap()
            .canonical()
        };
        assert_eq!(transform("100", "1", "2"), "50");
        assert_eq!(transform("60", "2", "2"), "60");
        assert_eq!(transform("100", "1", "1"), "100");
        assert_eq!(transform("60", "2", "1"), "120");
        assert_eq!(transform("1", "1", "3"), "0.333333333333");
        assert_eq!(transform("1.0000000000005", "1", "1"), "1");
        assert_eq!(transform("1.0000000000015", "1", "1"), "1.000000000002");
    }
}

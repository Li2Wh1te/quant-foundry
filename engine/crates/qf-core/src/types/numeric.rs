//! Decimal storage is rust_decimal's signed 96-bit coefficient and scale 0..28.
//! Bounded 512-bit intermediates prove exactness and implement explicit rounding;
//! no library checked operator is assumed to preserve discarded decimal digits.
use super::error::{ErrorCode, QfError, QfResult};
use bnum::{cast::CastFrom, types::I512};
const WIDE_ZERO: I512 = I512::from_bytes([0; 64]);
use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use std::{fmt, str::FromStr};

const MAX_COEFFICIENT: i128 = (1_i128 << 96) - 1;
pub const COST_SCALE: u32 = 12;

fn range() -> QfError {
    QfError::new(
        ErrorCode::NumericRangeUnsupported,
        "numeric",
        "数值不能无损表示或超出受支持范围",
    )
}

fn power(scale: u32) -> I512 {
    I512::cast_from(10).pow(scale)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RoundingPolicy {
    HalfEven,
    /// Nearest value; exact midpoints round away from zero. Used by dated
    /// exchange price rules and explicitly configured CNY fee settlement.
    HalfUp,
    TowardZero,
    /// Explicit saved-account ROUND_UP agreement; never an analysis default.
    AwayFromZero,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ExactDecimal(Decimal);

impl ExactDecimal {
    pub const ZERO: Self = Self(Decimal::ZERO);
    pub const ONE: Self = Self(Decimal::ONE);

    pub fn is_negative(self) -> bool {
        self.0 < Decimal::ZERO
    }
    pub fn is_zero(self) -> bool {
        self.0.is_zero()
    }
    pub fn scale(self) -> u32 {
        self.0.scale()
    }
    pub fn canonical(self) -> String {
        self.0.normalize().to_string()
    }
    pub fn from_integer(value: i64) -> Self {
        Self(Decimal::from(value))
    }

    fn from_parts(mut coefficient: I512, mut scale: u32) -> QfResult<Self> {
        let max = I512::cast_from(MAX_COEFFICIENT);
        // Exact operations may shed only zeros, including excess storage scale.
        while (scale > 28 || coefficient.abs() > max) && scale > 0 {
            if coefficient % I512::cast_from(10) != WIDE_ZERO {
                return Err(range());
            }
            coefficient /= I512::cast_from(10);
            scale -= 1;
        }
        if scale > 28 || coefficient.abs() > max {
            return Err(range());
        }
        let signed = i128::cast_from(coefficient); // Bounds above prove this cast lossless.
        Ok(Self(Decimal::from_i128_with_scale(signed, scale)))
    }

    pub fn checked_add(self, other: Self) -> QfResult<Self> {
        let scale = self.scale().max(other.scale());
        let left = I512::cast_from(self.0.mantissa()) * power(scale - self.scale());
        let right = I512::cast_from(other.0.mantissa()) * power(scale - other.scale());
        Self::from_parts(left + right, scale)
    }

    pub fn checked_sub(self, other: Self) -> QfResult<Self> {
        let negated = Self(Decimal::from_i128_with_scale(
            -other.0.mantissa(),
            other.scale(),
        ));
        self.checked_add(negated)
    }

    pub fn checked_mul(self, other: Self) -> QfResult<Self> {
        Self::from_parts(
            I512::cast_from(self.0.mantissa()) * I512::cast_from(other.0.mantissa()),
            self.scale() + other.scale(),
        )
    }

    fn rounded_quotient(numerator: I512, denominator: I512, policy: RoundingPolicy) -> I512 {
        let quotient = numerator / denominator;
        let remainder = numerator % denominator;
        if policy == RoundingPolicy::TowardZero || remainder == WIDE_ZERO {
            return quotient;
        }
        let twice = remainder.abs() * I512::cast_from(2);
        let bound = denominator.abs();
        if policy == RoundingPolicy::AwayFromZero
            || twice > bound
            || (twice == bound
                && (policy == RoundingPolicy::HalfUp || quotient % I512::cast_from(2) != WIDE_ZERO))
        {
            let sign = if (numerator < WIDE_ZERO) == (denominator < WIDE_ZERO) {
                1
            } else {
                -1
            };
            quotient + I512::cast_from(sign)
        } else {
            quotient
        }
    }

    pub fn round(self, scale: u32, policy: RoundingPolicy) -> QfResult<Self> {
        if scale > 28 {
            return Err(range());
        }
        let coefficient = I512::cast_from(self.0.mantissa());
        let rounded = if scale >= self.scale() {
            coefficient * power(scale - self.scale())
        } else {
            Self::rounded_quotient(coefficient, power(self.scale() - scale), policy)
        };
        // Shed only redundant zeros when necessary; the rounded value remains exact.
        Self::from_parts(rounded, scale)
    }

    pub fn div_rounded(self, other: Self, scale: u32, policy: RoundingPolicy) -> QfResult<Self> {
        if other.is_zero() || scale > 28 {
            return Err(range());
        }
        let numerator = I512::cast_from(self.0.mantissa()) * power(other.scale() + scale);
        let denominator = I512::cast_from(other.0.mantissa()) * power(self.scale());
        Self::from_parts(
            Self::rounded_quotient(numerator, denominator, policy),
            scale,
        )
    }

    pub fn mul_rounded(self, other: Self, scale: u32, policy: RoundingPolicy) -> QfResult<Self> {
        if scale > 28 {
            return Err(range());
        }
        let product = I512::cast_from(self.0.mantissa()) * I512::cast_from(other.0.mantissa());
        let product_scale = self.scale() + other.scale();
        let rounded = if scale >= product_scale {
            product * power(scale - product_scale)
        } else {
            Self::rounded_quotient(product, power(product_scale - scale), policy)
        };
        Self::from_parts(rounded, scale)
    }

    /// floor(quantity * numerator / denominator / step) * step; no rounded ratio.
    pub fn legal_quantity(
        quantity: Quantity,
        numerator: Self,
        denominator: Self,
        step: QuantityStep,
    ) -> QfResult<Quantity> {
        if numerator.is_negative() || denominator <= Self::ZERO {
            return Err(range());
        }
        let top = I512::cast_from(quantity.get())
            * I512::cast_from(numerator.0.mantissa())
            * power(denominator.scale());
        let bottom = I512::cast_from(denominator.0.mantissa())
            * power(numerator.scale())
            * I512::cast_from(step.get());
        let result = (top / bottom) * I512::cast_from(step.get());
        if result > I512::cast_from(i64::MAX) {
            return Err(range());
        }
        Quantity::new(i64::cast_from(result)) // Bounds above prove this cast lossless.
    }

    /// floor(value * proportion / price / step) * step, without rounding an
    /// intermediate target amount. D06 percent targets use the original rational.
    pub fn legal_value_quantity(
        value: Self,
        proportion: Self,
        price: Self,
        step: QuantityStep,
    ) -> QfResult<Quantity> {
        if value.is_negative() || proportion.is_negative() || price <= Self::ZERO {
            return Err(range());
        }
        let top = I512::cast_from(value.0.mantissa())
            * I512::cast_from(proportion.0.mantissa())
            * power(price.scale());
        let bottom = I512::cast_from(price.0.mantissa())
            * power(value.scale() + proportion.scale())
            * I512::cast_from(step.get());
        let result = (top / bottom) * I512::cast_from(step.get());
        if result > I512::cast_from(i64::MAX) {
            return Err(range());
        }
        Quantity::new(i64::cast_from(result))
    }

    /// Cost total is authoritative; the final sale releases all residual cost.
    pub fn release_cost(total: Self, held: Quantity, sold: Quantity) -> QfResult<(Self, Self)> {
        if total.is_negative() || held.get() == 0 || sold > held {
            return Err(range());
        }
        if sold == held {
            return Ok((total, Self::ZERO));
        }
        // Round the original rational once, avoiding rounded ratio * total.
        let numerator =
            I512::cast_from(total.0.mantissa()) * I512::cast_from(sold.get()) * power(COST_SCALE);
        let denominator = I512::cast_from(held.get()) * power(total.scale());
        let released = Self::from_parts(
            Self::rounded_quotient(numerator, denominator, RoundingPolicy::HalfEven),
            COST_SCALE,
        )?;
        let remaining = total.checked_sub(released)?;
        if remaining.is_negative() {
            return Err(range());
        }
        Ok((released, remaining))
    }
}

impl FromStr for ExactDecimal {
    type Err = QfError;
    fn from_str(input: &str) -> QfResult<Self> {
        if input.len() > 128 || input.is_empty() {
            return Err(range());
        }
        let unsigned = input.strip_prefix('-').unwrap_or(input);
        let mut parts = unsigned.split('.');
        let whole = parts.next().unwrap_or("");
        let fraction = parts.next();
        if whole.is_empty()
            || !whole.bytes().all(|c| c.is_ascii_digit())
            || (whole.len() > 1 && whole.starts_with('0'))
            || parts.next().is_some()
            || fraction.is_some_and(|v| v.is_empty() || !v.bytes().all(|c| c.is_ascii_digit()))
        {
            return Err(range());
        }
        if let Ok(value) = Decimal::from_str_exact(input) {
            return Ok(Self(value));
        }
        // Values with redundant fractional zeros are still losslessly representable.
        let trimmed = if fraction.is_some() {
            input.trim_end_matches('0').trim_end_matches('.')
        } else {
            input
        };
        Decimal::from_str_exact(trimmed)
            .map(Self)
            .map_err(|_| range())
    }
}
impl fmt::Display for ExactDecimal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}
impl Serialize for ExactDecimal {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.to_string())
    }
}
impl<'de> Deserialize<'de> for ExactDecimal {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(de::Error::custom)
    }
}

/// CNY amount; signed values support P&L. Cash bounds belong to the account port.
pub type Money = ExactDecimal;

pub(crate) fn nonnegative_decimal<'de, D: Deserializer<'de>>(
    d: D,
) -> Result<ExactDecimal, D::Error> {
    let text = String::deserialize(d)?;
    if text.starts_with('-') {
        return Err(de::Error::custom("数值必须为非负十进制字符串"));
    }
    text.parse().map_err(de::Error::custom)
}
pub(crate) fn optional_nonnegative<'de, D: Deserializer<'de>>(
    d: D,
) -> Result<Option<ExactDecimal>, D::Error> {
    nonnegative_decimal(d).map(Some)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct Price(ExactDecimal);
impl Price {
    pub fn new(value: ExactDecimal) -> QfResult<Self> {
        if value <= ExactDecimal::ZERO {
            return Err(range());
        }
        Ok(Self(value))
    }
    pub fn get(self) -> ExactDecimal {
        self.0
    }
}
impl FromStr for Price {
    type Err = QfError;
    fn from_str(input: &str) -> QfResult<Self> {
        Self::new(input.parse()?)
    }
}
impl<'de> Deserialize<'de> for Price {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Self::new(ExactDecimal::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct Quantity(i64);
impl Quantity {
    pub const ZERO: Self = Self(0);
    pub fn new(value: i64) -> QfResult<Self> {
        if value < 0 {
            return Err(QfError::new(
                ErrorCode::InvalidOrder,
                "quantity",
                "数量必须为非负整数",
            ));
        }
        Ok(Self(value))
    }
    pub fn get(self) -> i64 {
        self.0
    }
    pub fn checked_add(self, other: Self) -> QfResult<Self> {
        Self::new(self.0.checked_add(other.0).ok_or_else(range)?)
    }
    pub fn checked_sub(self, other: Self) -> QfResult<Self> {
        Self::new(self.0.checked_sub(other.0).ok_or_else(range)?)
    }
}
impl<'de> Deserialize<'de> for Quantity {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Self::new(i64::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct QuantityStep(Quantity);
impl QuantityStep {
    pub fn new(value: i64) -> QfResult<Self> {
        if value == 0 {
            return Err(QfError::new(
                ErrorCode::InvalidOrder,
                "quantity_step",
                "数量步长必须为正整数",
            ));
        }
        Ok(Self(Quantity::new(value)?))
    }
    pub fn get(self) -> i64 {
        self.0.get()
    }
    pub fn quantize(self, value: Quantity) -> Quantity {
        Quantity(value.get() / self.get() * self.get())
    }
}
impl<'de> Deserialize<'de> for QuantityStep {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Self::new(i64::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

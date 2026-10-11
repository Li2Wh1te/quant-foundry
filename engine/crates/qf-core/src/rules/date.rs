//! Civil effective dates only. Trading-session counting remains owned by D03.
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Deserializer, Serialize, de};
use std::{fmt, str::FromStr};

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct RuleDate(String);

impl RuleDate {
    pub fn as_str(&self) -> &str {
        &self.0
    }
    pub fn next_day(&self) -> QfResult<Self> {
        let year: u32 = self.0[..4].parse().map_err(|_| invalid())?;
        let month: u32 = self.0[5..7].parse().map_err(|_| invalid())?;
        let day: u32 = self.0[8..].parse().map_err(|_| invalid())?;
        if let Ok(date) = format!("{year:04}-{month:02}-{:02}", day + 1).parse() {
            return Ok(date);
        }
        let (year, month) = if month == 12 {
            (year + 1, 1)
        } else {
            (year, month + 1)
        };
        format!("{year:04}-{month:02}-01").parse()
    }

    /// Tax holding periods use natural months/years, not trading days.
    /// A missing anniversary (e.g. 31 February) is deliberately unavailable;
    /// the settlement provider must supply an authoritative holding band.
    pub(crate) fn anniversary(&self, months: u32) -> QfResult<Self> {
        let year: u32 = self.0[..4].parse().map_err(|_| invalid())?;
        let month: u32 = self.0[5..7].parse().map_err(|_| invalid())?;
        let day = &self.0[8..];
        let total = year * 12 + month - 1 + months;
        format!("{:04}-{:02}-{day}", total / 12, total % 12 + 1).parse()
    }
}

fn invalid() -> QfError {
    QfError::new(
        ErrorCode::RuleUnavailable,
        "rule_date",
        "规则日期无效或自然月边界需要权威结算事实",
    )
}
impl FromStr for RuleDate {
    type Err = QfError;
    fn from_str(value: &str) -> QfResult<Self> {
        if !crate::run::valid_date(value) {
            return Err(invalid());
        }
        Ok(Self(value.to_owned()))
    }
}
impl fmt::Display for RuleDate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl<'de> Deserialize<'de> for RuleDate {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        String::deserialize(d)?.parse().map_err(de::Error::custom)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EffectiveRange {
    pub from: RuleDate,
    /// Inclusive end of verified coverage, not an assertion that the law expires.
    pub through: RuleDate,
}
impl EffectiveRange {
    pub fn validate(&self) -> QfResult<()> {
        if self.from > self.through {
            return Err(invalid());
        }
        Ok(())
    }
    pub fn contains(&self, date: &RuleDate) -> bool {
        &self.from <= date && date <= &self.through
    }
    pub fn overlaps(&self, other: &Self) -> bool {
        self.from <= other.through && other.from <= self.through
    }
}

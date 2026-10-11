use super::error::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use std::{fmt, str::FromStr};

pub(crate) fn label(value: &str, max: usize) -> QfResult<()> {
    if value.is_empty()
        || value.len() > max
        || value.trim() != value
        || value.chars().any(char::is_control)
    {
        return Err(QfError::new(
            ErrorCode::InvalidContract,
            "key",
            "标识必须非空、无控制字符且长度有界",
        ));
    }
    Ok(())
}

macro_rules! key {
    ($name:ident) => {
        #[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
        #[serde(transparent)]
        pub struct $name(String);
        impl $name {
            pub fn new(value: impl Into<String>) -> QfResult<Self> {
                let value = value.into();
                label(&value, 128)?;
                Ok(Self(value))
            }
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }
        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                Self::new(String::deserialize(d)?).map_err(de::Error::custom)
            }
        }
    };
}
key!(SecurityKey);
key!(SessionKey);
key!(ChannelKey);

macro_rules! decimal_integer {
    ($name:ident, $inner:ty) => {
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name($inner);
        impl $name {
            pub const fn new(value: $inner) -> Self {
                Self(value)
            }
            pub const fn get(self) -> $inner {
                self.0
            }
        }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                self.0.fmt(f)
            }
        }
        impl FromStr for $name {
            type Err = QfError;
            fn from_str(value: &str) -> QfResult<Self> {
                let number: $inner = value.parse().map_err(|_| {
                    QfError::new(
                        ErrorCode::NumericRangeUnsupported,
                        "integer",
                        "整数超出受支持范围",
                    )
                })?;
                if number.to_string() != value {
                    return Err(QfError::new(
                        ErrorCode::InvalidContract,
                        "integer",
                        "时间与序号必须使用规范十进制字符串",
                    ));
                }
                Ok(Self(number))
            }
        }
        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
                s.serialize_str(&self.to_string())
            }
        }
        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                String::deserialize(d)?.parse().map_err(de::Error::custom)
            }
        }
    };
}
decimal_integer!(Nanoseconds, i64);
decimal_integer!(Sequence, u64);

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventIdentity {
    pub source_session: SessionKey,
    pub channel: ChannelKey,
    pub sequence: Option<Sequence>,
    /// Stable upstream input identity, independent of chunk/file iteration order.
    pub stable_input_sequence: Sequence,
}

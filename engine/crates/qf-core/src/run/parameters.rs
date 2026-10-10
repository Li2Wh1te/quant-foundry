//! Preserve JSON object identity while retaining arbitrary-precision number tokens.
//!
//! Value's arbitrary_precision visitor also recognizes internal marker keys in
//! ordinary user objects. Decode containers from raw JSON instead, so no user
//! key is mistaken for a number (or RawValue) marker.
use super::{MAX_CONTROL_BYTES, MAX_PARAMETER_DEPTH, config_error};
use crate::QfResult;
use serde::{Deserialize, Deserializer, de};
use serde_json::{Map, Value, value::RawValue};
use std::collections::BTreeMap;

pub(super) fn deserialize<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Map<String, Value>, D::Error> {
    let raw = Box::<RawValue>::deserialize(deserializer)?;
    match parse(raw.get(), 0).map_err(de::Error::custom)? {
        Value::Object(map) => Ok(map),
        _ => Err(de::Error::custom(config_error())),
    }
}

fn parse(input: &str, parent_depth: usize) -> QfResult<Value> {
    if input.len() > MAX_CONTROL_BYTES {
        return Err(config_error());
    }
    let text = input.trim_start();
    match text.as_bytes().first() {
        Some(b'{') => {
            if parent_depth >= MAX_PARAMETER_DEPTH {
                return Err(config_error());
            }
            let raw: BTreeMap<String, Box<RawValue>> =
                serde_json::from_str(text).map_err(|_| config_error())?;
            let mut map = Map::new();
            for (key, value) in raw {
                map.insert(key, parse(value.get(), parent_depth + 1)?);
            }
            Ok(Value::Object(map))
        }
        Some(b'[') => {
            if parent_depth >= MAX_PARAMETER_DEPTH {
                return Err(config_error());
            }
            let raw: Vec<Box<RawValue>> = serde_json::from_str(text).map_err(|_| config_error())?;
            raw.iter()
                .map(|value| parse(value.get(), parent_depth + 1))
                .collect::<QfResult<Vec<_>>>()
                .map(Value::Array)
        }
        _ => serde_json::from_str(text).map_err(|_| config_error()),
    }
}

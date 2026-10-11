//! D05 deterministic float64 research calculations. Never execution prices.
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const MAX_SAMPLES: usize = 100_000;
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Available,
    Warmup,
    Missing,
}
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct Point {
    pub value: Option<f64>,
    pub status: Status,
}
impl Point {
    fn na(status: Status) -> Self {
        Self {
            value: None,
            status,
        }
    }
    fn value(value: f64) -> QfResult<Self> {
        if !value.is_finite() {
            return Err(invalid("指标中间值超出有限 float64 范围"));
        }
        Ok(Self {
            value: Some(value),
            status: Status::Available,
        })
    }
}
fn invalid(message: &str) -> QfError {
    QfError::new(ErrorCode::NumericRangeUnsupported, "indicator", message)
}
fn validate(values: &[Option<f64>], period: usize) -> QfResult<()> {
    if values.len() > MAX_SAMPLES || period == 0 || period > 10_000 {
        return Err(QfError::new(
            ErrorCode::ResourceLimit,
            "indicator",
            "指标样本数或窗口超限",
        ));
    }
    if values.iter().flatten().any(|v| !v.is_finite()) {
        return Err(invalid("指标只接受有限数值或明确缺失"));
    }
    Ok(())
}
pub fn sma(values: &[Option<f64>], period: usize) -> QfResult<Vec<Point>> {
    validate(values, period)?;
    let (mut sum, mut missing) = (0.0, 0_usize);
    let mut result = Vec::with_capacity(values.len());
    for (i, value) in values.iter().enumerate() {
        if let Some(value) = value {
            sum += value / period as f64;
        } else {
            missing += 1;
        }
        if i >= period {
            if let Some(value) = values[i - period] {
                sum -= value / period as f64;
            } else {
                missing -= 1;
            }
        }
        result.push(if missing > 0 {
            Point::na(Status::Missing)
        } else if i + 1 < period {
            Point::na(Status::Warmup)
        } else {
            Point::value(sum)?
        });
    }
    Ok(result)
}
fn smoothed(values: &[Option<f64>], period: usize, alpha: f64) -> QfResult<Vec<Point>> {
    validate(values, period)?;
    let mut result = Vec::with_capacity(values.len());
    let (mut seed, mut count, mut state, mut missing) = (0.0, 0, None, false);
    for value in values {
        let Some(value) = value else {
            seed = 0.0;
            count = 0;
            state = None;
            missing = true;
            result.push(Point::na(Status::Missing));
            continue;
        };
        if let Some(previous) = state {
            let next = (1.0 - alpha) * previous + alpha * value;
            result.push(Point::value(next)?);
            state = Some(next);
        } else {
            seed += value / period as f64;
            count += 1;
            if count == period {
                result.push(Point::value(seed)?);
                state = Some(seed);
                missing = false;
            } else {
                result.push(Point::na(if missing {
                    Status::Missing
                } else {
                    Status::Warmup
                }));
            }
        }
    }
    Ok(result)
}
pub fn ema(values: &[Option<f64>], period: usize) -> QfResult<Vec<Point>> {
    smoothed(values, period, 2.0 / (period as f64 + 1.0))
}
pub fn rolling_std(values: &[Option<f64>], period: usize, ddof: usize) -> QfResult<Vec<Point>> {
    validate(values, period)?;
    if values.len().saturating_mul(period) > 10_000_000 {
        return Err(QfError::new(
            ErrorCode::ResourceLimit,
            "indicator",
            "严格双遍方差超过计算预算",
        ));
    }
    if ddof >= period {
        return Err(QfError::new(
            ErrorCode::InvalidContract,
            "indicator",
            "ddof 必须小于窗口",
        ));
    }
    values
        .iter()
        .enumerate()
        .map(|(i, _)| {
            let window = &values[(i + 1).saturating_sub(period)..=i];
            if window.iter().any(Option::is_none) {
                return Ok(Point::na(Status::Missing));
            }
            if window.len() < period {
                return Ok(Point::na(Status::Warmup));
            }
            // Centered two-pass variance, no cancellation of E[x²]-E[x]².
            let mean: f64 = window.iter().flatten().map(|v| v / period as f64).sum();
            let variance = window
                .iter()
                .flatten()
                .map(|v| (v - mean).powi(2) / (period - ddof) as f64)
                .sum::<f64>();
            Point::value(variance.sqrt())
        })
        .collect()
}
pub fn rsi(values: &[Option<f64>], period: usize) -> QfResult<Vec<Point>> {
    validate(values, period)?;
    let mut gains = Vec::with_capacity(values.len());
    let mut losses = Vec::with_capacity(values.len());
    let mut previous: Option<f64> = None;
    for value in values {
        let delta = match (previous, value) {
            (Some(p), Some(v)) => Some(v - p),
            _ => None,
        };
        gains.push(delta.map(|d| d.max(0.0)));
        losses.push(delta.map(|d| (-d).max(0.0)));
        previous = *value;
    }
    let gain = smoothed(&gains, period, 1.0 / period as f64)?;
    let loss = smoothed(&losses, period, 1.0 / period as f64)?;
    let mut had_missing = false;
    gain.iter()
        .zip(&loss)
        .enumerate()
        .map(|(i, (g, l))| {
            had_missing |= values[i].is_none();
            match (g.value, l.value) {
                (Some(g), Some(l)) => Point::value(if g == 0.0 && l == 0.0 {
                    50.0
                } else if l == 0.0 {
                    100.0
                } else {
                    100.0 * (g / (g + l))
                }),
                _ => Ok(Point::na(if had_missing {
                    Status::Missing
                } else {
                    Status::Warmup
                })),
            }
        })
        .collect()
}
pub fn atr(
    high: &[Option<f64>],
    low: &[Option<f64>],
    close: &[Option<f64>],
    period: usize,
) -> QfResult<Vec<Point>> {
    validate(high, period)?;
    validate(low, period)?;
    validate(close, period)?;
    if high.len() != low.len() || high.len() != close.len() {
        return Err(invalid("ATR 输入长度不一致"));
    }
    let mut previous: Option<f64> = None;
    let mut ranges = Vec::with_capacity(high.len());
    for ((h, l), c) in high.iter().zip(low).zip(close) {
        match (h, l, c) {
            (Some(h), Some(l), Some(c)) if h >= l && c >= l && c <= h => {
                let range =
                    previous.map_or(h - l, |p| (h - l).max((h - p).abs()).max((l - p).abs()));
                ranges.push(Some(range));
                previous = Some(*c);
            }
            (Some(_), Some(_), Some(_)) => return Err(invalid("ATR 的 OHLC 输入不一致")),
            _ => {
                ranges.push(None);
                previous = None;
            }
        }
    }
    smoothed(&ranges, period, 1.0 / period as f64)
}
#[derive(Debug, Clone, Serialize)]
pub struct Macd {
    pub macd: Vec<Point>,
    pub signal: Vec<Point>,
    pub histogram: Vec<Point>,
}
pub fn macd(values: &[Option<f64>], fast: usize, slow: usize, signal: usize) -> QfResult<Macd> {
    validate(values, fast)?;
    validate(values, slow)?;
    validate(values, signal)?;
    if fast >= slow {
        return Err(QfError::new(
            ErrorCode::InvalidContract,
            "indicator",
            "MACD 要求 fast < slow",
        ));
    }
    let fast_values = ema(values, fast)?;
    let slow_values = ema(values, slow)?;
    let line: Vec<Point> = fast_values
        .iter()
        .zip(&slow_values)
        .map(|(a, b)| match (a.value, b.value) {
            (Some(a), Some(b)) => Point::value(a - b),
            _ => Ok(Point::na(b.status)),
        })
        .collect::<QfResult<_>>()?;
    let mut signal_values = ema(&line.iter().map(|p| p.value).collect::<Vec<_>>(), signal)?;
    let mut had_missing = false;
    for (i, point) in signal_values.iter_mut().enumerate() {
        had_missing |= values[i].is_none();
        if point.value.is_none() {
            point.status = if had_missing {
                Status::Missing
            } else {
                Status::Warmup
            };
        }
    }
    let histogram = line
        .iter()
        .zip(&signal_values)
        .map(|(a, b)| match (a.value, b.value) {
            (Some(a), Some(b)) => Point::value(a - b),
            _ => Ok(Point::na(b.status)),
        })
        .collect::<QfResult<_>>()?;
    Ok(Macd {
        macd: line,
        signal: signal_values,
        histogram,
    })
}
pub fn rank(
    values: &BTreeMap<String, Option<f64>>,
    ascending: bool,
) -> QfResult<BTreeMap<String, Point>> {
    validate(&values.values().copied().collect::<Vec<_>>(), 1)?;
    let mut valid: Vec<_> = values
        .iter()
        .filter_map(|(s, v)| v.map(|v| (s.clone(), v)))
        .collect();
    valid.sort_by(|(a, x), (b, y)| {
        (if ascending {
            x.total_cmp(y)
        } else {
            y.total_cmp(x)
        })
        .then(a.cmp(b))
    });
    let mut result: BTreeMap<_, _> = values
        .keys()
        .map(|s| (s.clone(), Point::na(Status::Missing)))
        .collect();
    let mut i = 0;
    while i < valid.len() {
        let mut end = i + 1;
        while end < valid.len() && valid[end].1 == valid[i].1 {
            end += 1;
        }
        let point = Point::value((i + 1 + end) as f64 / 2.0)?;
        for (s, _) in &valid[i..end] {
            result.insert(s.clone(), point);
        }
        i = end;
    }
    Ok(result)
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub kind: String,
    #[serde(default)]
    pub values: Vec<Option<f64>>,
    #[serde(default)]
    pub high: Vec<Option<f64>>,
    #[serde(default)]
    pub low: Vec<Option<f64>>,
    #[serde(default)]
    pub close: Vec<Option<f64>>,
    #[serde(default = "default_period")]
    pub period: usize,
    #[serde(default = "default_ddof")]
    pub ddof: usize,
    #[serde(default = "default_fast")]
    pub fast: usize,
    #[serde(default = "default_slow")]
    pub slow: usize,
    #[serde(default = "default_signal")]
    pub signal: usize,
    #[serde(default)]
    pub cross_section: BTreeMap<String, Option<f64>>,
    #[serde(default = "default_ascending")]
    pub ascending: bool,
}
fn default_period() -> usize {
    14
}
fn default_ddof() -> usize {
    1
}
fn default_fast() -> usize {
    12
}
fn default_slow() -> usize {
    26
}
fn default_signal() -> usize {
    9
}
fn default_ascending() -> bool {
    true
}
pub fn calculate(request: Request) -> QfResult<serde_json::Value> {
    let result = match request.kind.as_str() {
        "sma" => serde_json::to_value(sma(&request.values, request.period)?),
        "ema" => serde_json::to_value(ema(&request.values, request.period)?),
        "rsi" => serde_json::to_value(rsi(&request.values, request.period)?),
        "atr" => serde_json::to_value(atr(
            &request.high,
            &request.low,
            &request.close,
            request.period,
        )?),
        "rolling_std" => {
            serde_json::to_value(rolling_std(&request.values, request.period, request.ddof)?)
        }
        "macd" => serde_json::to_value(macd(
            &request.values,
            request.fast,
            request.slow,
            request.signal,
        )?),
        "rank" => serde_json::to_value(rank(&request.cross_section, request.ascending)?),
        _ => {
            return Err(QfError::new(
                ErrorCode::CapabilityUnavailable,
                "indicator",
                "不支持此指标",
            ));
        }
    };
    result.map_err(|_| invalid("指标输出无效"))
}

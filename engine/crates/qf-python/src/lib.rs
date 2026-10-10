//! Thin D01 boundary; Rust is the authority for numeric and run validation.
use pyo3::prelude::*;
use pyo3::{
    create_exception,
    exceptions::{PyTypeError, PyValueError},
    types::{PyAny, PyBool, PyDict, PyInt, PyModule},
};
use qf_core::types::{
    ExactDecimal, MarketEvent, Nanoseconds, Price, Quantity, QuantityStep, RoundingPolicy,
};
use qf_core::{QfError, QfResult};

create_exception!(_native, ContractError, PyValueError);
fn python_error(error: QfError) -> PyErr {
    let code = error.code.as_str();
    let operation = error.operation.clone();
    Python::attach(|py| {
        let exception = ContractError::new_err(error.to_string());
        let value = exception.value(py);
        // Set only known safe diagnostic fields, never parser excerpts of input.
        let _ = value.setattr("code", code);
        let _ = value.setattr("operation", operation);
        let _ = value.setattr("message", error.message);
        let scope = PyDict::new(py);
        for (key, item) in error.scope {
            let _ = scope.set_item(key, item);
        }
        let _ = value.setattr("scope", scope);
        exception
    })
}
fn convert<T>(result: QfResult<T>) -> PyResult<T> {
    result.map_err(python_error)
}

#[pyfunction]
fn validate_run_config_json(py: Python<'_>, input: &str) -> PyResult<String> {
    convert(py.detach(|| qf_core::run::RunConfig::from_json(input)?.normalized_json()))
}
#[pyfunction]
fn capabilities_json() -> PyResult<String> {
    serde_json::to_string(&qf_core::run::capabilities())
        .map_err(|_| PyValueError::new_err("contract serialization"))
}
fn fee_json<T: serde::de::DeserializeOwned>(input: &str) -> QfResult<T> {
    if input.len() > qf_core::run::MAX_CONTROL_BYTES {
        return Err(QfError::new(
            qf_core::ErrorCode::ResourceLimit,
            "fee_config",
            "费用配置消息超过预算",
        ));
    }
    serde_json::from_str(input).map_err(|_| {
        QfError::new(
            qf_core::ErrorCode::RuleUnavailable,
            "fee_config",
            "费用配置契约无效",
        )
    })
}
#[pyfunction]
#[pyo3(signature = (input, overrides="{}"))]
fn validate_commission_config_json(input: &str, overrides: &str) -> PyResult<String> {
    convert((|| {
        let config: qf_core::rules::fees::CommissionConfig = fee_json(input)?;
        let overrides: qf_core::rules::CostOverrides = fee_json(overrides)?;
        let config = config.with_overrides(&overrides)?;
        serde_json::to_string(&config).map_err(|_| {
            QfError::new(
                qf_core::ErrorCode::InvalidContract,
                "fee_config",
                "费用配置序列化失败",
            )
        })
    })())
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct FeeCompositionRequest {
    scope: qf_core::rules::fees::FeeScope,
    effective: qf_core::rules::date::EffectiveRange,
    commission: qf_core::rules::fees::CommissionConfig,
    #[serde(default)]
    cost_overrides: qf_core::rules::CostOverrides,
}
#[pyfunction]
fn compose_official_fee_config_json(py: Python<'_>, input: &str) -> PyResult<String> {
    convert(py.detach(|| {
        let request: FeeCompositionRequest = fee_json(input)?;
        let fees = qf_core::rules::catalog::verified_fee_catalog()?.compose(
            request.scope,
            &request.effective,
            &request.commission,
            &request.cost_overrides,
            &qf_core::rules::market::RuleUse::Market,
        )?;
        serde_json::to_string(
            &serde_json::json!({"scope": fees.scope(), "fee_config": fees.config()}),
        )
        .map_err(|_| {
            QfError::new(
                qf_core::ErrorCode::InvalidContract,
                "fee_config",
                "费用配置序列化失败",
            )
        })
    }))
}
#[pyfunction]
fn parse_decimal(input: &str) -> PyResult<String> {
    convert(input.parse::<ExactDecimal>()).map(|v| v.to_string())
}
#[pyfunction]
fn parse_price(input: &str) -> PyResult<String> {
    convert(input.parse::<Price>()).map(|v| v.get().to_string())
}
#[pyfunction]
fn round_decimal(input: &str, scale: u32) -> PyResult<String> {
    convert((|| {
        input
            .parse::<ExactDecimal>()?
            .round(scale, RoundingPolicy::HalfEven)
    })())
    .map(|v| v.to_string())
}
#[pyfunction]
fn divide_decimal(numerator: &str, denominator: &str, scale: u32) -> PyResult<String> {
    convert((|| {
        numerator.parse::<ExactDecimal>()?.div_rounded(
            denominator.parse()?,
            scale,
            RoundingPolicy::HalfEven,
        )
    })())
    .map(|v| v.to_string())
}
#[pyfunction]
fn legal_quantity(
    quantity: &Bound<'_, PyAny>,
    numerator: &str,
    denominator: &str,
    step: &Bound<'_, PyAny>,
) -> PyResult<i64> {
    let integer = |v: &Bound<'_, PyAny>| -> PyResult<i64> {
        if v.is_instance_of::<PyBool>() || !v.is_instance_of::<PyInt>() {
            return Err(PyTypeError::new_err(
                "quantity and step require integers, excluding bool",
            ));
        }
        v.extract::<i64>()
    };
    let quantity = integer(quantity)?;
    let step = integer(step)?;
    convert((|| {
        ExactDecimal::legal_quantity(
            Quantity::new(quantity)?,
            numerator.parse()?,
            denominator.parse()?,
            QuantityStep::new(step)?,
        )
    })())
    .map(Quantity::get)
}
#[pyfunction]
fn roundtrip_ns(input: &str) -> PyResult<String> {
    convert(input.parse::<Nanoseconds>()).map(|v| v.to_string())
}
#[pyfunction]
fn validate_market_event_json(input: &str) -> PyResult<String> {
    convert((|| {
        if input.len() > qf_core::run::MAX_CONTROL_BYTES {
            return Err(qf_core::QfError::new(
                qf_core::ErrorCode::ResourceLimit,
                "market_event",
                "消息超过预算",
            ));
        }
        let event: MarketEvent = serde_json::from_str(input).map_err(|_| {
            qf_core::QfError::new(
                qf_core::ErrorCode::InvalidContract,
                "market_event",
                "事件契约无效",
            )
        })?;
        event.validate()?;
        serde_json::to_string(&event).map_err(|_| {
            qf_core::QfError::new(
                qf_core::ErrorCode::InvalidContract,
                "market_event",
                "事件序列化无效",
            )
        })
    })())
}

fn research_json(value: &impl serde::Serialize) -> qf_core::QfResult<String> {
    serde_json::to_string(value).map_err(|_| {
        QfError::new(
            qf_core::ErrorCode::InvalidContract,
            "research_view",
            "研究输出序列化失败",
        )
    })
}
#[pyfunction]
fn indicator_json(py: Python<'_>, input: &str) -> PyResult<String> {
    convert(py.detach(|| {
        research_json(&qf_core::analysis::indicators::calculate(
            qf_core::data::views::bounded_json(input)?,
        )?)
    }))
}
/// D10 enters one of these per callback and ALWAYS expires it on exit, including
/// exceptions. This narrow binding does not drive callbacks or import strategies.
#[pyclass(name = "ReadView")]
struct PythonReadView {
    view: qf_core::data::views::ReadView,
}
#[pymethods]
impl PythonReadView {
    #[new]
    #[pyo3(signature = (boundary, max_rows=10000, max_bytes=64*1024*1024))]
    fn new(boundary: &str, max_rows: usize, max_bytes: usize) -> PyResult<Self> {
        Ok(Self {
            view: convert((|| {
                qf_core::data::views::ReadView::new(
                    qf_core::data::views::bounded_json(boundary)?,
                    max_rows,
                    max_bytes,
                )
            })())?,
        })
    }
    fn check(&self) -> PyResult<()> {
        convert(self.view.check())
    }
    fn visible_key_json(&self, key: &str) -> PyResult<bool> {
        convert((|| {
            self.view
                .contains_market(&qf_core::data::views::bounded_json(key)?)
        })())
    }
    fn reset(&mut self) -> PyResult<()> {
        convert(self.view.reset())
    }
    fn expire(&mut self) {
        self.view.expire();
    }
    fn push_market(&mut self, py: Python<'_>, metadata: &str, payload: &[u8]) -> PyResult<()> {
        convert(py.detach(|| {
            self.view
                .push_market(payload, &qf_core::data::views::bounded_json(metadata)?)
        }))
    }
    fn push_research(&mut self, py: Python<'_>, metadata: &str, payload: &[u8]) -> PyResult<()> {
        convert(py.detach(|| {
            self.view
                .push_research(payload, &qf_core::data::views::bounded_json(metadata)?)
        }))
    }
    #[pyo3(signature = (request, source_frequency, calendar="[]"))]
    fn prices_json(
        &mut self,
        py: Python<'_>,
        request: &str,
        source_frequency: &str,
        calendar: &str,
    ) -> PyResult<String> {
        convert(py.detach(|| {
            let request: qf_core::data::DataRequest = qf_core::data::views::bounded_json(request)?;
            let source = qf_core::data::views::bounded_json(&format!("\"{source_frequency}\""))?;
            self.view.resample(
                source,
                request.frequency,
                &qf_core::data::views::bounded_json::<Vec<qf_core::clock::CalendarSession>>(
                    calendar,
                )?,
            )?;
            self.view.adjust(request.adjustment, request.end_ns)?;
            research_json(&self.view.prices(&request)?)
        }))
    }
    fn research_json(&self, py: Python<'_>, request: &str) -> PyResult<String> {
        convert(py.detach(|| {
            research_json(
                &self
                    .view
                    .research(&qf_core::data::views::bounded_json(request)?)?,
            )
        }))
    }
    fn research_prices(&mut self, py: Python<'_>, request: &str) -> PyResult<()> {
        convert(py.detach(|| {
            self.view
                .research_prices(&qf_core::data::views::bounded_json(request)?)
        }))
    }
    fn current_json(&self, py: Python<'_>, securities: &str) -> PyResult<String> {
        convert(py.detach(|| {
            research_json(&self.view.current(
                &qf_core::data::views::bounded_json::<Vec<String>>(securities)?,
            )?)
        }))
    }
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("ContractError", m.py().get_type::<ContractError>())?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(validate_run_config_json, m)?)?;
    m.add_function(wrap_pyfunction!(capabilities_json, m)?)?;
    m.add_function(wrap_pyfunction!(parse_decimal, m)?)?;
    m.add_function(wrap_pyfunction!(parse_price, m)?)?;
    m.add_function(wrap_pyfunction!(round_decimal, m)?)?;
    m.add_function(wrap_pyfunction!(divide_decimal, m)?)?;
    m.add_function(wrap_pyfunction!(legal_quantity, m)?)?;
    m.add_function(wrap_pyfunction!(roundtrip_ns, m)?)?;
    m.add_function(wrap_pyfunction!(validate_market_event_json, m)?)?;
    m.add_function(wrap_pyfunction!(validate_commission_config_json, m)?)?;
    m.add_function(wrap_pyfunction!(compose_official_fee_config_json, m)?)?;
    m.add_function(wrap_pyfunction!(indicator_json, m)?)?;
    m.add_class::<PythonReadView>()?;
    Ok(())
}

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
        let _ = value.setattr("scope", PyDict::new(py));
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
    Ok(())
}

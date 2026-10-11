use qf_core::data::{ActualScope, Adjustment, BatchMetadata, DataBatch, DataRequest, TimeUnit};
use qf_core::results::{
    FiniteStatistic, LogLevel, LogRecord, ResultBatch, ResultRecord, SinkCredit,
};
use qf_core::rules::{DatedFeeComponent, FeeComponentKind, FeeConfig, FeeFact};
use qf_core::run::{
    BatchRequest, Frequency, MAX_PARAMETER_BYTES, RunConfig, RunStatus, capabilities,
};
use qf_core::types::market::{Currency, QuantityUnit};
use qf_core::types::{MarketEvent, Nanoseconds, Quantity, Sequence};
use qf_core::{ErrorCode, QfError};
use serde_json::{Value, json};

fn base() -> Value {
    serde_json::from_str(include_str!(
        "../../../../contracts/examples/run_config.json"
    ))
    .unwrap()
}

#[test]
fn shared_valid_invalid_service_boundary() {
    let cases: Vec<Value> = serde_json::from_str(include_str!(
        "../../../../contracts/examples/run_config_cases.json"
    ))
    .unwrap();
    for case in cases {
        let mut value = base();
        for (key, patch) in case["patch"].as_object().unwrap() {
            value[key] = patch.clone();
        }
        if let Some(bytes) = case["build"]["parameter_string_bytes"].as_u64() {
            value["parameters"] = json!({"a":"x".repeat(bytes as usize)});
        }
        for key in case["drop"].as_array().unwrap() {
            value.as_object_mut().unwrap().remove(key.as_str().unwrap());
        }
        let result = RunConfig::from_json(&value.to_string());
        assert_eq!(
            result.is_ok(),
            case["valid"].as_bool().unwrap(),
            "{}: {result:?}",
            case["id"]
        );
        if let Err(error) = result {
            assert_eq!(error.code, ErrorCode::InvalidRunConfig);
        }
    }
}

#[test]
fn normalized_defaults_and_idempotent_numeric_spelling() {
    let config = RunConfig::from_json(&base().to_string()).unwrap();
    let normalized = config.normalized_json().unwrap();
    let value: Value = serde_json::from_str(&normalized).unwrap();
    assert_eq!(value["initial_cash"], "10000");
    assert_eq!(value["participation_rate"], "0.1");
    assert_eq!(value["slippage_bps"], "0");
    assert_eq!(value["annualization_sessions"], 252);
    assert_eq!(value["universe"], json!(["000001.SZ", "510300.SH"]));
    assert_eq!(
        RunConfig::from_json(&normalized)
            .unwrap()
            .normalized_json()
            .unwrap(),
        normalized
    );
    let mut other = base();
    other["initial_cash"] = "10000.0000".into();
    other["participation_rate"] = "0.10000".into();
    assert_eq!(
        RunConfig::from_json(&other.to_string())
            .unwrap()
            .normalized_json()
            .unwrap(),
        normalized
    );
}

#[test]
fn parameters_do_not_silently_convert_large_integers_or_infinity() {
    let mut value = base();
    value["parameters"] = serde_json::from_str("{\"n\":18446744073709551617}").unwrap();
    let config = RunConfig::from_json(&value.to_string()).unwrap();
    let normalized: Value = serde_json::from_str(&config.normalized_json().unwrap()).unwrap();
    assert_eq!(
        normalized["parameters"]["n"].to_string(),
        "18446744073709551617"
    );
    let invalid = value.to_string().replace("18446744073709551617", "1e400");
    assert!(RunConfig::from_json(&invalid).is_err());
    let large_integer = format!("1{}1", "0".repeat(998));
    let valid = value
        .to_string()
        .replace("18446744073709551617", &large_integer);
    let config = RunConfig::from_json(&valid).unwrap();
    assert_eq!(config.parameters["n"].to_string(), large_integer);
}

#[test]
fn parameter_object_keys_are_never_internal_serde_markers() {
    let mut value = base();
    let parameters = json!({
        "$serde_json::private::Number": "123",
        "nested": [
            {"$serde_json::private::Number": "18446744073709551617"},
            {"$serde_json::private::RawValue": "[1,2]"},
            {"$serde_json::private::Number": "not a number", "other": true}
        ],
        "large_integer": serde_json::from_str::<Value>("18446744073709551617").unwrap()
    });
    value["parameters"] = parameters.clone();
    let from_json = RunConfig::from_json(&value.to_string()).unwrap();
    assert_eq!(from_json.parameters, *parameters.as_object().unwrap());
    let from_value: RunConfig = serde_json::from_value(value).unwrap();
    assert_eq!(from_value, from_json);
    let normalized = from_json.normalized_json().unwrap();
    let repeated = RunConfig::from_json(&normalized).unwrap();
    assert_eq!(repeated.parameters, from_json.parameters);
    assert_eq!(repeated.normalized_json().unwrap(), normalized);
}

#[test]
fn root_depth_and_utf8_parameter_budget_boundaries() {
    let mut value = base();
    // {"a":"..."} takes 8 additional normalized bytes.
    value["parameters"] = json!({"a":"x".repeat(MAX_PARAMETER_BYTES - 8)});
    assert!(RunConfig::from_json(&value.to_string()).is_ok());
    value["parameters"] = json!({"a":"x".repeat(MAX_PARAMETER_BYTES - 7)});
    assert!(RunConfig::from_json(&value.to_string()).is_err());
    value["parameters"] = json!({"a":"汉".repeat(MAX_PARAMETER_BYTES / 3)});
    assert!(RunConfig::from_json(&value.to_string()).is_err());
    let mut nested = json!(0);
    for _ in 0..7 {
        nested = json!({"a":nested});
    }
    value["parameters"] = json!({"nested":nested});
    assert!(RunConfig::from_json(&value.to_string()).is_ok());
    value["parameters"] = json!({"nested":value["parameters"].clone()});
    assert!(RunConfig::from_json(&value.to_string()).is_err());
    assert!(RunConfig::from_json(&" ".repeat(1024 * 1024 + 1)).is_err());
}

#[test]
fn quotes_keep_two_sided_units_and_same_nanosecond_identity() {
    let text = include_str!("../../../../contracts/examples/quote_tick.json");
    let first: MarketEvent = serde_json::from_str(text).unwrap();
    first.validate().unwrap();
    let mut next: Value = serde_json::from_str(text).unwrap();
    next["event"]["identity"]["sequence"] = "3".into();
    next["event"]["identity"]["stable_input_sequence"] = "3".into();
    let second: MarketEvent = serde_json::from_value(next.clone()).unwrap();
    assert_eq!(first.key().time_ns, second.key().time_ns);
    assert_ne!(first.key(), second.key());
    assert!(first.key() < second.key());
    next["event"]["bid_quantity"] = Value::Null;
    if let MarketEvent::QuoteTick(q) = serde_json::from_value(next.clone()).unwrap() {
        assert_eq!(q.bid_quantity, None);
        assert_eq!(q.ask_quantity, Some(Quantity::new(200).unwrap()));
    } else {
        panic!("wrong event");
    }
    next["event"]["bid_quantity"] = (-1).into();
    assert!(serde_json::from_value::<MarketEvent>(next).is_err());
    assert!(serde_json::from_str::<MarketEvent>("{\"kind\":\"unknown\",\"event\":{}}").is_err());
    assert_eq!(
        serde_json::from_str::<MarketEvent>(&serde_json::to_string(&first).unwrap()).unwrap(),
        first
    );
}

#[test]
fn data_contract_rejects_future_and_unbounded_batches() {
    let request = DataRequest {
        securities: vec![],
        fields: vec!["close".into()],
        frequency: Frequency::Day,
        start_ns: None,
        count_per_security: Some(20),
        end_ns: Nanoseconds::new(1767225600000000124),
        adjustment: Adjustment::None,
    };
    assert_eq!(
        request
            .validate(Nanoseconds::new(1767225600000000123))
            .unwrap_err()
            .code,
        ErrorCode::LookaheadForbidden
    );
    request.validate(request.end_ns).unwrap();
    let metadata = BatchMetadata {
        schema_id: "qf.arrow.v1".into(),
        time_unit: TimeUnit::UtcNanoseconds,
        quantity_unit: QuantityUnit::Shares,
        price_currency: Currency::CNY,
        rows: 0,
        actual_scope: ActualScope {
            start_ns: None,
            end_ns: None,
            securities: vec![],
        },
        limitations: vec![],
    };
    assert!(DataBatch::new(metadata.clone(), vec![0; 4], 3).is_err());
    assert!(DataBatch::new(metadata.clone(), vec![], 64 * 1024 * 1024 + 1).is_err());
    assert_eq!(
        DataBatch::new(metadata, vec![0; 4], 4)
            .unwrap()
            .payload()
            .len(),
        4
    );
    let mut too_long: BatchMetadata = serde_json::from_value(
        json!({"schema_id":"qf.arrow.v1", "time_unit":"utc_nanoseconds",
        "quantity_unit":"shares", "price_currency":"CNY", "rows":0,
        "actual_scope":{"start_ns":null,"end_ns":null,"securities":[]}, "limitations":[]}),
    )
    .unwrap();
    too_long.limitations = vec!["x".repeat(1024 * 1024)];
    assert!(DataBatch::new(too_long, vec![], 1).is_err());
}

#[test]
fn result_credit_finite_statistics_and_pages_are_bounded() {
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert!(FiniteStatistic::new(value).is_err());
    }
    assert_eq!(FiniteStatistic::new(0.1).unwrap().get(), 0.1);
    let records = vec![ResultRecord::Log(LogRecord {
        time_ns: Nanoseconds::new(1),
        level: LogLevel::Info,
        message: "fixture".into(),
        truncated: false,
    })];
    assert!(
        ResultBatch::new(
            Sequence::new(1),
            records.clone(),
            &SinkCredit {
                max_records: 0,
                max_bytes: 1024
            }
        )
        .is_err()
    );
    assert!(
        ResultBatch::new(
            Sequence::new(1),
            records.clone(),
            &SinkCredit {
                max_records: 1,
                max_bytes: 10
            }
        )
        .is_err()
    );
    assert_eq!(
        ResultBatch::new(
            Sequence::new(1),
            records.clone(),
            &SinkCredit {
                max_records: 1,
                max_bytes: 1024
            }
        )
        .unwrap()
        .records()
        .len(),
        1
    );
    assert!(
        ResultBatch::new(
            Sequence::new(u64::MAX),
            [records.clone(), records].concat(),
            &SinkCredit {
                max_records: 2,
                max_bytes: 1024
            }
        )
        .is_err()
    );
    assert!(qf_core::results::validate_page(10001, None).is_err());
    assert!(qf_core::results::validate_page(100, Some(&"x".repeat(4097))).is_err());
}

#[test]
fn typed_fees_require_explicit_facts_and_reject_unknown_components() {
    let mut fees: FeeConfig = serde_json::from_value(
        json!({"commission_rate":"0", "minimum_commission":"0", "currency":"CNY",
        "settlement_scale":2, "rounding":"half_even", "components":[], "synthetic_model":null}),
    )
    .unwrap();
    assert_eq!(
        fees.validate().unwrap_err().code,
        ErrorCode::RuleUnavailable
    );
    fees.synthetic_model = Some("zero-cost-synthetic-fixture".into());
    fees.validate().unwrap();
    fees.synthetic_model = None;
    fees.components.push(DatedFeeComponent {
        kind: FeeComponentKind::StampDuty,
        effective_from: "2026-01-01".into(),
        effective_through: "2026-01-31".into(),
        included_in_commission: false,
        fact: FeeFact::Inapplicable {
            basis: "synthetic inapplicability fixture".into(),
        },
    });
    fees.validate().unwrap();
    let mut later = fees.components[0].clone();
    later.effective_from = "2026-02-01".into();
    later.effective_through = "2026-02-28".into();
    fees.components.push(later);
    fees.validate().unwrap(); // Dated rule changes are valid, not duplicate fees.
    fees.components.push(fees.components[0].clone());
    assert!(fees.validate().is_err());
    assert!(serde_json::from_value::<FeeConfig>(json!({"fees":{"whatever":0}})).is_err());
    let error = QfError::new(ErrorCode::DataChanged, "gateway.check", "输入状态已变化");
    assert_eq!(
        serde_json::to_value(&error).unwrap()["code"],
        "DATA_CHANGED"
    );
}

#[test]
fn batch_context_is_fixed_and_capabilities_are_honest() {
    let first = RunConfig::from_json(&base().to_string()).unwrap();
    let mut next = base();
    next["parameters"] = json!({"window":30});
    let mut batch = BatchRequest {
        configs: vec![
            first.clone(),
            RunConfig::from_json(&next.to_string()).unwrap(),
        ],
    };
    batch.validate().unwrap();
    next["initial_cash"] = "10001".into();
    batch.configs[1] = RunConfig::from_json(&next.to_string()).unwrap();
    assert!(batch.validate().is_err());
    batch.configs = vec![first; 257];
    assert!(batch.validate().is_err());
    let caps = capabilities();
    assert!(caps.execution_models.is_empty());
    assert!(caps.frequencies.is_empty());
    assert!(!caps.production_data_available);
    assert!(RunStatus::Succeeded.terminal());
    assert!(!RunStatus::Running.terminal());
}

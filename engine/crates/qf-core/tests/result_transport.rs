use qf_core::results::{ResultBatch, ResultRecord, SinkCredit};
use serde_json::{Value, json};
fn point() -> Value {
    json!({"kind":"equity","record":{
        "time_ns":"1767225600000000123","session":null,"sampling":"initial","equity":"10000", "unavailable_reason":null,
        "net_value":{"status":"available","value":1.0},"session_return":{"status":"unavailable","reason":"INITIAL_POINT"},
        "cumulative_return":{"status":"available","value":0.0},"drawdown":{"status":"available","value":0.0},
        "benchmark_value":null,"benchmark_cumulative_return":{"status":"unavailable","reason":"NO_BENCHMARK"},
        "cumulative_excess_return":{"status":"unavailable","reason":"NO_BENCHMARK"}}})
}
fn decode(record: Value) -> qf_core::QfResult<ResultBatch> {
    ResultBatch::decode(
        &serde_json::to_vec(&json!({"first_sequence":"1","records":[record]})).unwrap(),
        &SinkCredit {
            max_records: 64,
            max_bytes: 1024 * 1024,
        },
    )
}
#[test]
fn finite_equity_metrics_roundtrip_through_real_result_batch_envelope() {
    let decoded = decode(point()).unwrap();
    let encoded = serde_json::to_vec(&decoded).unwrap();
    let again = ResultBatch::decode(
        &encoded,
        &SinkCredit {
            max_records: 64,
            max_bytes: 1024 * 1024,
        },
    )
    .unwrap();
    assert_eq!(decoded, again);
    match &again.records()[0] {
        ResultRecord::Equity(point) => {
            assert_eq!(point.time_ns.get(), 1767225600000000123);
            assert_eq!(point.equity.unwrap().to_string(), "10000");
        }
        _ => panic!("wrong record kind"),
    }
}
#[test]
fn numeric_strings_bools_private_number_maps_and_unknown_envelopes_stay_rejected() {
    for invalid in [
        json!("1.0"),
        json!(true),
        json!({"$serde_json::private::Number":"1.0"}),
        json!(null),
    ] {
        let mut record = point();
        record["record"]["net_value"]["value"] = invalid;
        assert!(decode(record).is_err());
    }
    let mut record = point();
    record["path"] = json!("/etc/passwd");
    assert!(decode(record).is_err());
    let mut record = point();
    record["record"]["path"] = json!("/etc/passwd");
    assert!(decode(record).is_err());
    let mut record = point();
    record["kind"] = json!("unknown");
    assert!(decode(record).is_err());
}
#[test]
fn statistic_user_record_keeps_finite_float_and_decimal_units() {
    let decoded=decode(json!({"kind":"record","record":{"time_ns":"123","values":{
        "stat":{"kind":"statistic","value":0.25},"amount":{"kind":"decimal","value":"0.333333333333"}}}})).unwrap();
    let again: ResultRecord =
        serde_json::from_str(&serde_json::to_string(&decoded.records()[0]).unwrap()).unwrap();
    assert_eq!(again, decoded.records()[0]);
}

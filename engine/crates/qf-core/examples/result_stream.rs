//! Reproducible D11 module resource probe; explicitly NOT product B3.
use qf_core::analysis::Performance;
use qf_core::matching::Fill;
use qf_core::orders::Side;
use qf_core::results::file::{FileLimits, FileResultSink};
use qf_core::results::{ResultBatch, ResultRecord, ResultSink};
use qf_core::run::{RunConfig, RunOutcome};
use qf_core::types::market::{Currency, MarketUnits, QuantityUnit};
use qf_core::types::*;
use qf_core::{ErrorCode, QfError, QfResult};
use std::sync::{Arc, atomic::AtomicBool};
fn write(s: &mut FileResultSink, n: &mut u64, record: ResultRecord) -> QfResult<()> {
    let credit = s.credit()?;
    s.write(&ResultBatch::new(Sequence::new(*n), vec![record], &credit)?)?;
    *n += 1;
    Ok(())
}
fn main() -> QfResult<()> {
    let args: Vec<_> = std::env::args().collect();
    let events: u64 = args
        .get(1)
        .and_then(|n| n.parse().ok())
        .unwrap_or(10_000_000);
    if events == 0 || events > 10_000_000 || !events.is_multiple_of(20_000) {
        return Err(QfError::new(
            ErrorCode::InvalidContract,
            "probe",
            "20k..10m events in 20k blocks required",
        ));
    }
    let mut config: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../contracts/examples/run_config.json"
    ))
    .unwrap();
    config["frequency"] = serde_json::json!("tick");
    config["execution_model"] = serde_json::json!("trade_tick_v1");
    let config: RunConfig = serde_json::from_value(config).unwrap();
    let mut performance =
        Performance::from_run(&config, "synthetic_d11_module_calendar", events / 20_000)?;
    let cancel = Arc::new(AtomicBool::new(false));
    let mut sink = FileResultSink::create(
        &std::env::temp_dir(),
        "d11-stream-probe",
        FileLimits::default(),
        cancel,
    )?;
    let mut sequence = 1;
    let mut processed = 0;
    let mut checksum = 0;
    let security = SecurityKey::new("A.SH")?;
    let session = SessionKey::new("synthetic")?;
    let origin = 1_767_225_600_000_000_123_i64;
    let price: Price = "10".parse()?;
    let initial = performance.begin(Nanoseconds::new(origin - 1), None)?;
    write(&mut sink, &mut sequence, ResultRecord::Equity(initial))?;
    let result = (|| -> QfResult<()> {
        for i in 0..events {
            // Only this event exists. Same-ns events retain distinct identities.
            let tick = TradeTick {
                security: security.clone(),
                session: session.clone(),
                time_ns: Nanoseconds::new(origin + (i / 10) as i64),
                identity: EventIdentity {
                    source_session: session.clone(),
                    channel: qf_core::types::keys::ChannelKey::new("fixture")?,
                    sequence: Some(Sequence::new(i + 1)),
                    stable_input_sequence: Sequence::new(i + 1),
                },
                price,
                quantity: Quantity::new(1)?,
                units: MarketUnits {
                    price_currency: Currency::CNY,
                    quantity_unit: QuantityUnit::Shares,
                },
            };
            let event = MarketEvent::TradeTick(tick);
            event.validate()?;
            checksum ^= event.key().identity.stable_input_sequence.get();
            processed += 1;
            if (i + 1).is_multiple_of(1000) {
                let fill = Fill {
                    trade_id: format!("fixture-fill-{}", i + 1),
                    order_id: format!("fixture-order-{}", i + 1),
                    security: security.clone(),
                    side: Side::Buy,
                    quantity: Quantity::new(1)?,
                    price,
                    fee: "0.01".parse()?,
                    execution_time_ns: event.key().time_ns,
                };
                performance.fill(&fill)?;
                write(&mut sink, &mut sequence, ResultRecord::Trade(fill))?;
            }
            if (i + 1).is_multiple_of(20_000) {
                let index = (i + 1) / 20_000 - 1;
                let value = if index.is_multiple_of(2) {
                    "11000"
                } else {
                    "10000"
                };
                let point = performance.session(
                    index,
                    &session,
                    event.key().time_ns,
                    Some(value.parse()?),
                    None,
                    None,
                )?;
                write(&mut sink, &mut sequence, ResultRecord::Equity(point))?;
            }
        }
        let summary = performance.summary(None);
        assert_eq!(summary.trades.fills, events / 1000);
        assert_eq!(
            summary.trades.total_notional,
            Money::from_integer((events / 100) as i64)
        );
        write(
            &mut sink,
            &mut sequence,
            ResultRecord::Summary(Box::new(summary)),
        )?;
        sink.finalize(&RunOutcome::Succeeded {
            result_id: "d11-stream-probe".into(),
        })
    })();
    if let Err(error) = result {
        assert!(
            sink.finalize(&RunOutcome::Succeeded {
                result_id: "d11-stream-probe".into()
            })
            .is_err()
        );
        let _ = sink.abort(&error);
        assert!(!matches!(
            sink.outcome(),
            Some(RunOutcome::Succeeded { .. })
        ));
        println!(
            "{}",
            serde_json::json!({"probe":"D11_MODULE_ONLY","requested_events":events,"processed_events":processed,"failure":error,"outcome":sink.outcome()})
        );
        if args.get(2).is_some_and(|s| s == "expect-write-failure") {
            return Ok(());
        }
        return Err(error);
    }
    println!(
        "{}",
        serde_json::json!({"probe":"D11_MODULE_ONLY_NOT_B1_B4","events":processed,"checksum":checksum,"peak_retained_market_events":1,
        "python_callbacks":0,"production_executed":false,"results":sink.overview(),"shards":sink.manifest().unwrap().shards.len()})
    );
    Ok(())
}

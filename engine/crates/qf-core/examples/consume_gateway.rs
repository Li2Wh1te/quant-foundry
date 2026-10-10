//! Isolated D04 integration probe, not a strategy runner/production entrypoint.
use qf_core::clock::{Checkpoint, MergeLimits, StreamingMerge};
use qf_core::data::arrow::ArrowEventSource;
use qf_core::data::ipc::IpcGateway;
use qf_core::data::{Adjustment, DataGateway, DataRequest, RunScope};
use qf_core::run::Frequency;
use qf_core::types::SecurityKey;
use qf_core::{QfError, QfResult};
use std::os::unix::net::UnixStream;
use std::sync::{Arc, atomic::AtomicBool};
use std::time::Duration;

struct Control;
impl Checkpoint for Control {
    fn check(&mut self) -> QfResult<()> {
        Ok(())
    }
}
fn run() -> QfResult<serde_json::Value> {
    let args: Vec<String> = std::env::args().collect();
    let invalid = || {
        QfError::new(
            qf_core::ErrorCode::InvalidContract,
            "integration_probe",
            "测试参数无效",
        )
    };
    if args.len() != 9 {
        return Err(invalid());
    }
    let socket = UnixStream::connect(&args[1]).map_err(|_| invalid())?;
    let security = SecurityKey::new(&args[3])?;
    let scope = RunScope {
        run_id: args[2].clone(),
        universe: vec![security.clone()],
    };
    let request = DataRequest {
        securities: vec![security],
        fields: args[8].split(',').map(String::from).collect(),
        frequency: serde_json::from_str::<Frequency>(&format!("\"{}\"", args[7]))
            .map_err(|_| invalid())?,
        start_ns: Some(args[5].parse()?),
        end_ns: args[6].parse()?,
        count_per_security: None,
        adjustment: Adjustment::None,
    };
    let credit: usize = args[4].parse().map_err(|_| invalid())?;
    let mut gateway = IpcGateway::new(
        socket,
        scope.clone(),
        64 * 1024 * 1024,
        Duration::from_secs(5),
        Arc::new(AtomicBool::new(false)),
    )?;
    let context = gateway.open(&scope)?;
    let mut count = 0_u64;
    let mut samples = Vec::new();
    let stats = {
        let source = ArrowEventSource::new(gateway.stream("market", &request)?, 64 * 1024 * 1024)?;
        let limits = MergeLimits {
            chunk_events: credit,
            max_buffered_events: credit,
            ..MergeLimits::default()
        };
        let mut merge = StreamingMerge::new(vec![source], limits)?;
        while let Some(event) = merge.pop(&mut Control)? {
            count += 1;
            if samples.len() < 8 {
                samples.push(event);
            }
        }
        merge.stats()
    };
    gateway.check(&context)?;
    gateway.close(context)?;
    Ok(
        serde_json::json!({"count":count,"samples":samples,"peak_buffered_events":stats.peak_buffered_events,
        "source_reads":stats.source_reads}),
    )
}
fn main() {
    match run() {
        Ok(value) => println!("{value}"),
        Err(error) => {
            println!("{}", serde_json::to_string(&error).unwrap_or_default());
            std::process::exit(1);
        }
    }
}

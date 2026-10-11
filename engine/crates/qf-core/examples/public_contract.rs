//! Compile/run using only public contracts. No Python or database client needed.
use qf_core::QfResult;
use qf_core::run::RunConfig;
use qf_core::types::{ExactDecimal, MarketEvent, Quantity, QuantityStep, RoundingPolicy};
fn main() -> QfResult<()> {
    let config = RunConfig::from_json(include_str!(
        "../../../../contracts/examples/run_config.json"
    ))?;
    let quote: MarketEvent = serde_json::from_str(include_str!(
        "../../../../contracts/examples/quote_tick.json"
    ))
    .expect("public fixture");
    quote.validate()?;
    let average = ExactDecimal::ONE.div_rounded("3".parse()?, 12, RoundingPolicy::HalfEven)?;
    let shares = ExactDecimal::legal_quantity(
        Quantity::new(301)?,
        ExactDecimal::ONE,
        "3".parse()?,
        QuantityStep::new(100)?,
    )?;
    assert_eq!(average.to_string(), "0.333333333333");
    assert_eq!(shares.get(), 100);
    println!("{}", config.normalized_json()?);
    println!(
        "{}",
        serde_json::to_string(&quote).expect("event serialization")
    );
    Ok(())
}

//! Independent synthetic_business_oracles; production_executed=false.
//! Real D06 orders + D09 account + D02 rules/fees + D03 engine. Only input
//! facts, host/data/result adapters and explicit fill plans are isolated.
#[path = "orders/business.rs"]
mod business;
#[path = "orders/engine.rs"]
mod engine;
#[path = "orders/support.rs"]
mod support;

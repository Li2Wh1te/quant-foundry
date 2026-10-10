//! Stable S3 module paths. D01 implements contracts and numerics, not an engine.
pub mod accounting;
pub mod analysis;
pub mod clock;
pub mod data;
pub mod engine;
pub mod matching;
pub mod orders;
pub mod results;
pub mod rules;
pub mod run;
pub mod strategy;
pub mod types;

pub use types::error::{ErrorCode, QfError, QfResult};

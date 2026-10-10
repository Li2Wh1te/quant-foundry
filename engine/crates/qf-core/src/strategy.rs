use crate::QfResult;
use crate::accounting::AccountView;
use crate::data::DataRequest;
use crate::orders::{OrderIntent, OrderResult};
use crate::types::{MarketEvent, Nanoseconds};

/// D05/D10 implement callback-scoped views; expiry is a real runtime boundary,
/// not a claim that a private Python attribute provides security isolation.
pub trait ReadView {
    type Frame;
    fn now_ns(&self) -> Nanoseconds;
    fn account(&self) -> QfResult<AccountView>;
    fn read(&mut self, request: &DataRequest) -> QfResult<Self::Frame>;
}
pub trait CommandSink {
    fn submit(&mut self, intent: &OrderIntent) -> QfResult<OrderResult>;
    fn cancel(&mut self, order_id: &str) -> QfResult<OrderResult>;
}
pub trait StrategyHost {
    fn initialize(
        &mut self,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()>;
    fn callback(
        &mut self,
        event: &MarketEvent,
        view: &mut dyn ReadView<Frame = Self::Frame>,
        commands: &mut dyn CommandSink,
    ) -> QfResult<()>;
    fn finish(&mut self) -> QfResult<()>;
    type Frame;
}

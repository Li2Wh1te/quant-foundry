use crate::QfResult;
use crate::accounting::AccountView;
use crate::clock::Visibility;
use crate::data::DataRequest;
use crate::orders::Order;
use crate::orders::{OrderIntent, OrderResult};
use crate::results::ResultRecord;
use crate::types::{MarketEvent, Nanoseconds};

/// D05/D10 implement callback-scoped views; expiry is a real runtime boundary,
/// not a claim that a private Python attribute provides security isolation.
pub trait ReadView {
    type Frame;
    fn now_ns(&self) -> Nanoseconds;
    fn account(&self) -> QfResult<AccountView>;
    fn read(&mut self, request: &DataRequest) -> QfResult<Self::Frame>;
    fn visibility(&self) -> Visibility {
        Visibility {
            now_ns: self.now_ns(),
            market_through: None,
        }
    }
    fn current_data(
        &self,
        _securities: &[crate::types::SecurityKey],
    ) -> QfResult<Vec<MarketEvent>> {
        Err(crate::QfError::new(
            crate::ErrorCode::CapabilityUnavailable,
            "current_data",
            "当前行情视图未接入",
        ))
    }
}
pub trait CommandSink {
    fn submit(&mut self, intent: &OrderIntent) -> QfResult<OrderResult>;
    fn cancel(&mut self, order_id: &str) -> QfResult<OrderResult>;
    fn get_order(&self, _order_id: &str) -> QfResult<Order> {
        Err(crate::QfError::new(
            crate::ErrorCode::CapabilityUnavailable,
            "get_order",
            "订单读取未接入",
        ))
    }
    fn get_open_orders(&self) -> QfResult<Vec<Order>> {
        Err(crate::QfError::new(
            crate::ErrorCode::CapabilityUnavailable,
            "get_open_orders",
            "活动订单读取未接入",
        ))
    }
    fn emit(&mut self, _record: ResultRecord) -> QfResult<()> {
        Err(crate::QfError::new(
            crate::ErrorCode::CapabilityUnavailable,
            "record",
            "用户结果接收器未接入",
        ))
    }
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

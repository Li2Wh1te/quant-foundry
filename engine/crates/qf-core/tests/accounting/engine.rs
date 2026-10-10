// Integration tests below use a real D09 account with D03's isolated host/data
// and matcher. D06 order lifecycle and production D04 mapping are not claimed.
use super::support::*;
#[path = "../time/support.rs"]
mod clock_fixture;
use qf_core::accounting::*;
use qf_core::clock::{CalendarSession, SessionCalendar};
use qf_core::engine::*;
use qf_core::matching::Fill;
use qf_core::orders::*;
use qf_core::results::ResultRecord;
use qf_core::rules::market::{SellAvailability, TradingStatus};
use qf_core::run::{Frequency, RunOutcome};
use qf_core::types::*;
use qf_core::{ErrorCode, QfResult};
use std::collections::BTreeMap;

/// ONLY an isolated D06 order-store adapter. All funds/cost/fees/corporate
/// effects are delegated to the real Account; there is no second cash balance.
struct Execution {
    account: Account,
    orders: BTreeMap<String, Order>,
    reports: Vec<CorporateActionReport>,
    malicious_assessment: bool,
}
impl Execution {
    fn result(order: &Order) -> OrderResult {
        OrderResult {
            accepted: true,
            order_id: Some(order.order_id.clone()),
            reason_code: None,
            message: "isolated order store; real D09 account".into(),
            unchanged: false,
            requested_quantity: Some(order.quantity),
            effective_quantity: Some(order.quantity),
        }
    }
    fn active(o: &Order) -> bool {
        matches!(
            o.status,
            OrderStatus::Accepted | OrderStatus::Open | OrderStatus::PartiallyFilled
        )
    }
    fn apply_boundary(
        &mut self,
        row: &CalendarSession,
        phase: ActionPhase,
    ) -> QfResult<Vec<Order>> {
        let (report, prepared) = self.account.preview(|a| {
            if phase == ActionPhase::BeforeOpen {
                let status = if row.session.key == key("S1") {
                    TradingStatus::Trading
                } else {
                    TradingStatus::Halted
                };
                a.install_terms(terms_with(
                    row,
                    "A",
                    SellAvailability::NextSession,
                    "0",
                    "1",
                    status,
                    SaleProceedsTiming::Immediate,
                    vec![],
                ))?;
            }
            a.advance_corporate_actions(&boundary(row, phase))
        })?;
        let mut cancelled = Vec::new();
        for id in &report.cancelled_order_ids {
            let mut order = self.order(id)?;
            order.status = OrderStatus::Cancelled;
            cancelled.push(order);
        }
        self.account.commit(prepared)?;
        for order in &cancelled {
            self.orders.insert(order.order_id.clone(), order.clone());
        }
        self.reports.push(report);
        Ok(cancelled)
    }
}
impl AccountPort for Execution {
    fn validate_and_reserve(&mut self, intent: &OrderIntent) -> QfResult<OrderResult> {
        self.account.validate_and_reserve(intent)
    }
    fn assess_fill(&self, fill: &Fill) -> QfResult<Fill> {
        let mut fill = self.account.assess_fill(fill)?;
        if self.malicious_assessment {
            fill.quantity = q(2);
        }
        Ok(fill)
    }
    fn apply_fill(&mut self, fill: &Fill) -> QfResult<()> {
        self.account.apply_fill(fill)
    }
    fn release(&mut self, id: &str) -> QfResult<()> {
        self.account.release(id)
    }
    fn settle(&mut self, key: &SessionKey) -> QfResult<()> {
        self.account.settle(key)
    }
    fn value(&self, now: Nanoseconds) -> QfResult<AccountView> {
        self.account.value(now)
    }
}
impl ExecutionPort for Execution {
    fn submit_order(
        &mut self,
        intent: &OrderIntent,
        activation: &OrderActivation,
    ) -> QfResult<OrderResult> {
        let IntentValue::Quantity(quantity) = intent.value else {
            panic!("quantity fixture only")
        };
        let id = format!("o{}", self.orders.len() + 1);
        let limit_price = match intent.style {
            OrderStyle::Market => None,
            OrderStyle::Limit { price } => Some(price),
        };
        let row = clock_fixture::row(0);
        let mut request = request(
            &row,
            &id,
            intent.side,
            quantity.get(),
            "10",
            activation.submitted_ns,
        );
        request.security = intent.security.clone();
        request.effective_session = activation.effective_session.clone();
        request.tif = intent.tif;
        request.limit_price = limit_price;
        self.account.reserve_order(request)?;
        let order = Order {
            order_id: id.clone(),
            security: intent.security.clone(),
            side: intent.side,
            quantity,
            filled_quantity: q(0),
            status: OrderStatus::Open,
            submitted_ns: activation.submitted_ns,
            limit_price,
            tif: intent.tif,
            effective_session: activation.effective_session.clone(),
            eligible_interval_start: Some(activation.eligible_interval_start),
            eligible_after_event: activation.eligible_after_event.clone(),
            reason_code: None,
            message: MODEL.into(),
        };
        let result = Self::result(&order);
        self.orders.insert(id, order);
        Ok(result)
    }
    fn cancel_order(&mut self, id: &str) -> QfResult<OrderResult> {
        let mut order = self.order(id)?;
        self.account.release(id)?;
        order.status = OrderStatus::Cancelled;
        let result = Self::result(&order);
        self.orders.insert(id.into(), order);
        Ok(result)
    }
    fn order(&self, id: &str) -> QfResult<Order> {
        self.orders
            .get(id)
            .cloned()
            .ok_or_else(|| clock_fixture::failure(ErrorCode::InvalidOrder))
    }
    fn active_orders(&self, security: &SecurityKey, limit: usize) -> QfResult<Vec<Order>> {
        let orders: Vec<_> = self
            .orders
            .values()
            .filter(|o| o.security == *security && Self::active(o))
            .cloned()
            .collect();
        if orders.len() > limit {
            return Err(clock_fixture::failure(ErrorCode::ResourceLimit));
        }
        Ok(orders)
    }
    fn active_order_count(&self) -> usize {
        self.orders.values().filter(|o| Self::active(o)).count()
    }
    fn transition(&mut self, order: &Order) -> QfResult<()> {
        self.orders.insert(order.order_id.clone(), order.clone());
        Ok(())
    }
    fn mark(&mut self, event: &MarketEvent) -> QfResult<()> {
        self.account.mark(event)
    }
    fn expire_day(&mut self, session: &SessionKey) -> QfResult<Vec<Order>> {
        let mut expired: Vec<_> = self
            .orders
            .values()
            .filter(|o| {
                o.tif == TimeInForce::Day && o.effective_session == *session && Self::active(o)
            })
            .cloned()
            .collect();
        self.account.transact(|a| {
            for o in &expired {
                a.release(&o.order_id)?;
            }
            Ok(())
        })?;
        for order in &mut expired {
            order.status = OrderStatus::Expired;
            self.orders.insert(order.order_id.clone(), order.clone());
        }
        Ok(expired)
    }
    fn session_start(&mut self, session: &CalendarSession) -> QfResult<Vec<Order>> {
        self.apply_boundary(session, ActionPhase::BeforeOpen)
    }
    fn session_end(&mut self, session: &CalendarSession) -> QfResult<Vec<Order>> {
        self.apply_boundary(session, ActionPhase::SessionClose)
    }
}

fn run(
    malicious: bool,
    fractional: bool,
) -> (
    EngineReport,
    Execution,
    clock_fixture::Writer,
    clock_fixture::Host,
) {
    let r: Vec<_> = (0..3).map(clock_fixture::row).collect();
    let mut account = account("1000", &r, SellAvailability::NextSession, "1");
    if fractional {
        account
            .add_corporate_action(
                split(
                    &r,
                    ShareRatio {
                        numerator: 1,
                        denominator: 2,
                    },
                    1,
                ),
                r[0].before_open_ns,
            )
            .unwrap();
    } else {
        let mut action = dividend(&r, CashDividendTax::SyntheticFlat { rate: d("0") });
        action.ex_raw_mark = Some(raw(&r[1], "10.8", r[1].before_open_ns));
        account
            .add_corporate_action(action, r[0].before_open_ns)
            .unwrap();
    }
    let mut execution = Execution {
        account,
        orders: BTreeMap::new(),
        reports: vec![],
        malicious_assessment: malicious,
    };
    let mut host = clock_fixture::Host {
        initialize: Some(Box::new(|_, commands, _| {
            commands.submit(&clock_fixture::intent("A", TimeInForce::Day))?;
            Ok(())
        })),
        ..Default::default()
    };
    let first_close = r[0].after_close_ns;
    host.after = Some(Box::new(move |view, commands| {
        if view.now_ns() == first_close {
            commands.submit(&clock_fixture::intent("A", TimeInForce::Gtc))?;
        }
        Ok(())
    }));
    let event = clock_fixture::daily(&r[0], "A");
    let mut data = clock_fixture::Data::default();
    let mut control = clock_fixture::Control::default();
    let mut matcher = clock_fixture::Matching::default();
    let mut rules = clock_fixture::Rules;
    let mut writer = clock_fixture::Writer::default();
    let calendar = SessionCalendar::new(MODEL.into(), r, Some(key("NEXT"))).unwrap();
    let report = Engine::new(
        clock_fixture::config(Frequency::Day),
        calendar,
        vec![clock_fixture::Source::new(vec![event], 1)],
    )
    .unwrap()
    .run(EnginePorts {
        data: &mut data,
        execution: &mut execution,
        matcher: &mut matcher,
        rules: &mut rules,
        strategy: &mut host,
        control: &mut control,
        results: &mut writer,
    });
    (report, execution, writer, host)
}
#[test]
fn real_account_driver_corrects_fee_registers_close_entitlement_and_cancels_gtc_on_ex() {
    let (report, execution, writer, host) = run(false, false);
    assert!(
        matches!(report.outcome, RunOutcome::Succeeded { .. }),
        "{:?}",
        report.outcome
    );
    let trades: Vec<_> = writer
        .records
        .iter()
        .filter_map(|r| match r {
            ResultRecord::Trade(t) => Some(t),
            _ => None,
        })
        .collect();
    assert_eq!(trades.len(), 1);
    assert_eq!(trades[0].fee, d("1")); // matcher supplied zero, account assessed one.
    assert!(
        host.notices
            .iter()
            .any(|n| matches!(n,Notification::Trade(t) if t.fee==d("1")))
    );
    assert_eq!(execution.account.totals().trading_fees, d("1"));
    assert_eq!(execution.account.totals().dividend_income, d("0.2"));
    assert_eq!(execution.orders["o2"].status, OrderStatus::Cancelled);
    assert_eq!(execution.account.active_reservation_count(), 0);
    let effects: Vec<_> = execution.reports.iter().flat_map(|r| &r.effects).collect();
    assert!(
        effects
            .iter()
            .any(|e| e.kind == CorporateEffectKind::Registered && e.quantity == q(1))
    );
    assert!(
        effects
            .iter()
            .any(|e| e.kind == CorporateEffectKind::PaidDividend && e.cash_delta == d("0.2"))
    );
    let last = clock_fixture::row(2);
    let v = execution.account.value(last.after_close_ns).unwrap();
    assert_eq!(v.cash, d("989.2"));
    assert_eq!(v.receivables, d("0"));
    assert_eq!(v.total_value, Some(d("1000")));
}
#[test]
fn driver_rejects_assessment_that_changes_quantity_before_money_is_committed() {
    let (report, execution, _, _) = run(true, false);
    assert_eq!(
        clock_fixture::code(&report),
        Some(ErrorCode::InvalidContract)
    );
    assert_eq!(execution.account.held_quantity(&sec("A")), q(0));
    assert_eq!(execution.account.totals().trading_fees, d("0"));
    assert_eq!(
        execution.account.reservation("o1").unwrap().fee_paid,
        d("0")
    );
}
#[test]
fn corporate_failure_aborts_driver_and_preserves_reservation_cancelled_only_in_candidate() {
    let (report, execution, writer, host) = run(false, true);
    assert_eq!(
        clock_fixture::code(&report),
        Some(ErrorCode::RuleUnavailable)
    );
    assert_eq!(writer.aborted, 1);
    assert_eq!(writer.finalized, 0);
    assert_eq!(
        host.calls
            .iter()
            .filter(|s| s.as_str() == "after_close")
            .count(),
        1
    );
    assert_eq!(execution.account.held_quantity(&sec("A")), q(1));
    assert!(execution.account.reservation("o2").is_some());
    assert_eq!(execution.orders["o2"].status, OrderStatus::Open);
}

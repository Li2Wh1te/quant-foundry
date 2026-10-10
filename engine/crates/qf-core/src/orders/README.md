# S3-D06 订单、撤单与目标仓位

`OrderManager<F: OrderFacts>` 持有 D09 的唯一 `Account`，只另外保存订单、稳定
委托序、当前市场/命令游标，以及 D02 所需的当会话累计买入数量。没有第二份
现金或持仓，也没有调用旧 Python 算法。账户初始化、参与率、滑点均由上游已
归一化的运行配置提供；本模块不另设资本或撮合参数。

## 接入

1. 用 D03 接受的实际会话创建 `Account`，包含必要的下一结算/有效会话，再创建
   `OrderManager`。它复用账户日历，不推算周末或下一交易日。initialize 如需
   下单，调用方须先安装首会话的实际账户条款及已公开原价；未知事实拒绝。
2. `OrderFacts::session_terms` 提供 D02/D04 按当前会话解析的 `AccountTerms`；
   `admission` 提供真实身份、账户交易权限及必要的竞价/价格笼基准。不得把
   基准价猜成昨天收盘价。风险警示累计买入与挂买量由本订单层计算，包含
   当日买入后又卖出的份额；不会把供应商当前账户数字当模拟运行累计量。
3. D03 `CommandSink` 同步调用 `submit_order` / `cancel_order_at`，使用真实
   `OrderActivation` 和完整命令事件键。只处理预留、订单候选与通知，不能推进
   市场或递归回调。`command_changes` 返回原子替换中所有旧单取消及新单状态，
   不再仅通过 `OrderResult.order_id` 通知一张新单。
4. matcher 使用 `consume_with_budget`，见下节。D03 将结果交
   `apply_match_outcome`，D06 在同一个 D09 修订绑定候选中核费、提交成交与订单
   状态。独立调用本适配器的 `apply_fill` / `release` 会明确拒绝，防止绕过订单
   生命周期；实际记账仍只调用内部同一 `AccountPort`。
5. `session_start` 安装当日条款、处理盘前公司行动并复核余单；`session_end`
   处理登记/收盘支付，真实转发 `check_session_end`。D03 后置检查通过后才执行
   收盘回调。账户报告中的取消订单与订单状态原子提交；`corporate_effects`
   以带会话/纳秒的 `ResultRecord::CorporateAction` 写入既有有界 ResultSink。
6. 最后市场事件完成并关闭后才 `expire_day`。关闭回调的新 DAY 属下一实际会话，
   终点也不会强平 GTC。公司行动保守取消受影响 GTC 和下一会话 DAY，释放余量
   预留并通知，不调整价格或保留虚构优先级。

## 数量、目标及资金

`Quantity` / `Value` 使用显式 `Side`；Python 正负输入由 D10 薄绑定规范化。
target 的 side 由目标与预计净仓差量决定，目标数量/金额非负，比例 0–1。

预计基础仓 = 实际持仓 + 普通买单余量 − 普通卖单余量。目标单以目标减此基础
计算余量，替换只取消同标的 target 单；普通单永不隐式取消。重复同一目标
保留原订单/优先级，不新建记录。部分成交后实际持仓与 target 余量仍覆盖原目标。

数量指令必须满足申报单位；value/percent 使用原始十进制有理数的整数商余数
向下量化。目标价值用已公开 raw 估价，限价仅决定订单价格约束/资金预算，不
改变目标市值的换算。清仓按真实可卖量及零股规则处理，T+ 或普通卖单已冻结的
份额不可重复出售。`requested_quantity` 为金额转换后的整数数量或请求目标，
`effective_quantity` 为合法数量或有效预计净目标；0/已覆盖无动作均
`accepted=true, unchanged=true, order_id=None`。

初始估价加累计费用不能支付完整请求时返回 `INSUFFICIENT_CASH` 拒绝，并在
`effective_quantity` 明示可达数量/目标；没有合法单位时为 0 或已有基础仓。
合法单位量化与资金拒绝不会混成受理。target 替换失败丢弃候选，原订单、冻结量
和费用累计器保留。未成交卖单不给现金，其他订单的预留不可消费。

## D07/D08 撮合接缝（算法尚待所属包接入）

- `FillBudget::allowance(id, quantity, candidate_price, step)` 使用 D09
  `max_affordable` 返回当前原价/费用下可支付份额和明确原因；fill 的整数单位由
  matcher 声明，申报最低量与已受理订单的部分成交单位分别处理。
- 同事件已选择前序成交时必须用 `allowance_after(prior_fills, ...)`。前序成交
  通过同一账户临时候选核费/预览，不能给多张订单重复分配同一可用现金，也不能
  预支未选择/未成交的卖款。预览不修改活账户或累计最低费用。
- `trade_id(offset)` 给出本次候选的连续全运行 ID。按活动订单稳定委托序输出
  fills；每张发生状态变化的订单只返回一次更新，成交必须有对应份额/状态更新。
  不能为每个未成交 Tick 返回不变订单。流动性、Bar/Tick 价格、滑点、涨停队列
  等仍由 D07/D08 消费同一运行配置和 D02 事实，D06 不实现第二套撮合。
- `claim_match` / `commit_match` 可直接用于窄契约集成；claim 私有且绑定运行、
  订单修订、完整市场键及生效订单快照。同一事件、旧/跨运行 claim、未知订单、
  重复/乱序成交 ID、改变订单身份或不合法状态均在记账前拒绝。D03 driver 已
  真实接通这条链路；旧 matcher 的默认接口只是源码兼容，不能作为真实预算接入。
- 跳空后部分成交保留余单，成交/订单结果显示真实份额和 `INSUFFICIENT_CASH`
  原因；超額候选成交直接拒绝且不留下半批状态，不能无声缩量或负现金。

## 有界状态与查询

活动订单按标的索引，活动总数为 O(1)；不在每 Tick 扫全组合。已终态订单仅存
有界缓存，其详情可由 `OrderFacts::terminal_order` 接入 D13 已保存结果分页。
订单 ID 连续分配且永不复用，已淘汰的已发 ID 仍能幂等撤单，无需永久 tombstone
或完整订单历史。缓存之外的 get_order 不制造假对象；缺持久读取时明确能力不足。
撤单只释放余量，不退回已成交现金/持仓，不再收费。

`Order.updated_at` 保存完整状态变化键，serde 金额/纳秒仍按 D01 规范；现有 wire
提示由同一生成脚本同步。通知、effects、活动订单和终态缓存均有上限。正常
未成交 Tick 只推进当前防重游标，不复制账户、保存 Tick 检查记录或成功台账。

测试入口：`cargo test -p qf-core --locked --test orders`。隔离输入明确
`synthetic_business_oracles / production_executed=false`；订单、账户、费用和
driver 是真实代码，host/data/显式成交计划及结果收集器是隔离适配。正式数据
映射、生产联调、D10 SDK 执行绑定与 D07/D08 算法尚未完成，不能用此夹具冒充。

# S3-D08 基础成交/报价 Tick

`tick::TickMatcher::from_run(&RunConfig)` 只接收已归一化 tick 运行与唯一
`trade_tick_v1` / `quote_tick_v1` 依据。复用 D07 的 D02 已核定规则包
`TickRules`（`BarRules` 同类型）、D06 OrderManager 和 D09 Account，没有另一套
费用、订单、账户、数据读取或 Python 撮合。

成交打印的 quantity 是该事件**已验证增量份额**。当前 `qf.market.v1` 输入
不能把供应商累计量直接当 quantity；累计量转增量必须由 D04 正式来源语义
声明/实施，matcher 不猜测、不按值去重。零增量不成交；缺少 trade quantity
无法解码为合法 TradeTick。打印的 `floor(quantity * participation_rate)` 在
本策略所有买卖单间只分配一次。同价同量但不同完整身份的两个打印保留，
每个提供自己声明的事件量。

报价买 ask、卖 bid，本事件对应价格和数量都已知才可成交。每标的/会话两侧
分别保存价格、上次已知显示量和剩余参与量。新价格初次已知量建立
`floor(quantity * participation_rate)`；同价同量不补额；同价增加量仅添加
`floor((new - old) * participation_rate)`，减少量把余额截到新上限。每次新增
量独立向下取整，不累积小于一份的碎额。对侧价格/量变化不重置本侧。
量为 0 是已知观察，None 是 unknown；缺价/缺量间隙保留此前耗用，但不能
用此前余额匹配缺字段事件。同价恢复已知快照不会补回耗用。新会话重建额度，
必须出现本会话有效报价。交叉盘口、数量无对应价格、反序/重复完整键拒绝；
locked bid=ask 可作为基础报价，仍受全部方向/量/账户约束。

两类 Tick 可合并发布给策略，真实回调数和 D03 总序不变；只有 run 所选种类
进入匹配，另一类不分配量或刷新余额。D03 还会拒绝 matcher 在非所选事件上
返回撮合结果。没有流对应关系时不尝试重建成交与盘口活动关联。

新单不能回填触发它的事件；资格比较完整 `eligible_after_event` 和时间，
同纳秒后续合法总序事件仍可成交。D03 的日历、DAY 归属/到期和非递归通知，
D06 的稳定自有订单优先级/原子 target，D09 的结算/公司行动保持为唯一实现。

原价先检查 tick 和当日约束；不利滑点按原始十进制比例一次量化，买向上、
卖向下。Tick 滑点违反限价、当日价格带或无正价格时不成交，不造另一个
打印价；涨停方向买、跌停方向卖保守不成交。已知停牌不成交。未成交原因
只有变化时才输出订单记录，不逐 tick 保存成功日志。

每个候选包括真实 ns/ID，通过 `candidate_allowance_after(prior, candidate, 1)`
预览唯一累计账户。只能成交实际可支付量；最低佣金、税款、持仓、冻结和
卖款可用性均由 D09 核定。未知费用/规则及非法/重复 trade_id 拒绝；候选错误
不提交半批，不改变 matcher cursor/余额和实际账户。D03/D06 原子提交是下一步，
提交失败终止 run，不承诺失败执行现场恢复。

quote 不等于最后打印价，不从 bid/ask/中间价制造 raw 估值。市价预留需要
D06 已知有效 raw 估算价；无该价可使用显式合法 limit，金额/权益目标仍遵守
D09 不可估值规则。隔离 SDK 小夹具明确提供此前 synthetic print，并在
合并流中发布研究用真实 TradeTick，后者不增加 quote 模型流动性。

`model_description` 经现有有界 ResultSink 在 initialize 前保存一次模型、
精度、份额、滑点、流动性和上述假设。状态只有一个完整键及每授权标的两侧
余额，输入长度不扩张内存；D04 惰性 Arrow、D03 有界 chunk、D10 查询视图/
结果预算不变。不实现逐单订单簿、交易所队列位置/补单还原、精确撤单延迟、
真实成交保证、行情复制或逐行情台账。

验证使用 `cargo test -p qf-core --locked --test tick_matching --test time` 和
`QF_S3_PYTHON=python3.12 bash scripts/test_s3_engine.sh`。Python 必须安装新 wheel
后运行，不以源码导入或替身账户冒充跨语言链路。生产正式映射、D12 隔离、
D13 持久结果及 D18 千万 Tick 最终负载仍分别验收；小夹具不是这些验收。

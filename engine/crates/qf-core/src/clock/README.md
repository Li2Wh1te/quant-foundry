# S3-D03 时钟与事件循环接缝

实现入口为 `qf_core::engine::Engine`；正常集成使用 `Engine::from_run` 投影
D01 已验证的 `RunConfig`，检查日历引用和日期范围。`Engine::new` 是隔离测试及
底层适配入口。运行消耗 driver，不能重入或恢复。D01 `TradingSession`、完整
`EventKey`、订单、成交、错误、`RunOutcome` 和结果信用协议保持原身份。

这是一份实际实现接缝说明。真实网关、撮合、账户、Python host、分析和持久化
尚需相应模块接入；SDK/能力接口继续拒绝未实现的完整回测入口。

## 数据与日历

`EventSource::next_chunk(max_events)` 返回按 D01 完整事件键严格递增的有界块。
块长度和容量不超过请求上限，空块拒绝，`None` 是永久 EOF。来源会话、通道、
来源序号和稳定输入序由上游给定，不能由文件迭代顺序生成。完整键冲突报
`INVALID_CONTRACT`；同时间、同值而身份不同的合法事件全部保留。
读取、取消或契约错误是该合并器的终态，后续 peek/pop 返回原错误，不能忽略
错误后继续读取造成漏事件。

`StreamingMerge` 每源最多保留一个块和一个堆头，统一限制源数、缓冲事件数及
编码字节。首次读取、补块及逐事件均有取消检查点。编码字节预算不是进程 RSS
测量；D04 仍负责解码前的 Arrow/IPC 分配、owner 和实际内存预算。

`SessionCalendar` 是权威参考日历的有界小型输入，不生成周末/节假日。
`CalendarSession` 必须提供 UTC 纳秒边界、本地午夜映射、时区、盘前/盘后时点、
交易窗口、Bar 分桶窗口，以及**完整交易周/月**中的序号/总数。截取 run 不得
把首日改成第 1 交易日。最后交易日用 `-1`；不存在的正 N 不执行。
下一会话来自后续行或 `next_after_run`；未知时下单报 `RULE_UNAVAILABLE`。

`with_template` 仅将已选择的 D02 `SessionTemplate` 接到数据契约提供的
UTC/本地映射；有效日期选择仍由 D02/D04 完成。分钟桶不跨午休，尾桶用
`full_interval=false` 明示；此模块不聚合或插值行情。日线盘中定时拒绝；
分钟/小时本地定时须在相应桶边界，Tick 本地定时允许声明的竞价/连续窗口。
`scheduled_phase_at(template, time)` 逐标的使用已选择的有效模板，不从参考
Bar 窗口猜竞价阶段；相接边界取后一个阶段，旧 ETF 模板仍可在同刻为连续交易。
该接口仅返回常规日程，临时停牌竞价必须由 RulesPort 的权威状态事实覆盖；
Bar 的确认时点阶段不证明该区间内部的触价顺序。

## 单运行顺序

1. initialize 一次，封存调度和订阅；同刻定时按注册次序执行，重复注册保留次数。
   未调用 subscribe 时使用 run universe；显式调用后仅使用显式集合，同类标的去重。
2. 每会话先 `AccountPort::settle`，再 `ExecutionPort::session_start` 处理
   应生效规则/公司行动/GTC，再盘前生命周期及盘前定时。
3. 先筛选已生效订单，调用 D01 `Matcher`，校验输出身份/数量/时点，应用成交及
   订单状态，调用 D09 `mark`，再发布策略行情。同刻全部 Bar 完成以上动作，
   随后通知、同刻定时、一次 `handle_bars`。Bar 回调给 B 的订单不能进 B 同根 Bar。
4. Tick 逐事件通知/回调；订单资格使用最后市场键，后续同纳秒序号可以生效。
   同时点定时在该时点全部 Tick 之后执行。一个 run 只回放配置选定的撮合源；
   其他研究流通过受限数据视图读取，不能多算一份流动性。
5. 最后市场事件撮合后，DAY 余单到期，再盘后定时及生命周期。收盘同纳秒仍有
   后续 Tick 时保留当前会话资格；最后事件完成的收盘边界及
   盘后提交的订单归下一会话；到期通知内新订单也不能归已关闭会话。
   收盘本地定时在该时点没有剩余行情时同样归下一会话。

通知是迭代队列，不递归驱动时钟。新通知追加至尾部；命令、通知、同刻 Bar、
活动订单、总事件、待发结果都有限额。策略吞掉命令资源/取消错误仍以统一终态
结束。数据一致性在初始化、会话、市场边界及成功前检查；不重写底座质量判断。

## 适配 trait

| 接缝 | 负责模块与义务 |
|---|---|
| `EventSource` / `StrategyDataPort` | D04/D05：预先授权并打开网关；有界解码/读取，跨块一致性检查；只发布已完成事件；read 按 `Visibility` 逐行裁剪，close 释放资源 |
| `VisibleFrame` | D05/D10：报告实际最大知识时间/市场键；driver 再检查请求时间、帧可见上界及请求行情终点。首事件前可读已完成历史，同纳秒不得读后续未发布 Tick |
| `ExecutionPort: AccountPort` | D06/D09：唯一账户，原子提交/预留、撤销、状态/到期、GTC/公司行动、行情估值；复制 `OrderActivation` 到 D01 订单生效字段；按标的返回有界、稳定优先级的活动订单 |
| `Matcher` / `RulesPort` | D07/D08/D02：真实价格、流动性、费用与适用规则。driver 只传合格订单，不实现费用或真实撮合算法 |
| `EngineStrategy: StrategyHost` | D10：initialize 内注册，聚合 Bar、Tick、定时、生命周期、通知和 finish；Python 异常映射为 D01 错误并保留函数/行定位 |
| `RunControl: Checkpoint` | D12：取消/截止检查和有界可打断进度发送；用户 Python/I/O 的硬超时由 supervisor/worker 隔离落实 |
| D01 `ResultSink` | D11/D13：必要结果按信用分块、连续序号发送。最终 writer 协调依赖校验/终态原子提交；取消必须能打断信用/写入阻塞 |

回调使用短期只读 view 和窄命令 sink。账户借用只覆盖单个同步方法，不在调用
host 时持有账户借用；同一回调下单/撤销后 `view.account()` 可看到更新的预留。
D10 须另外落实跨语言视图过期，不能把 Python private 属性视作安全隔离。

driver 对每个端口调用返回统一错误，不启动异步任务网。成功前完成结果写入、
finish 和依赖检查。失败/取消调用 abort，所有路径 finish/close 各一次；清理
错误最多三个，保留原终态。可信端口本身的阻塞/进程崩溃由 D12 的可打断 I/O、
墙钟及资源限制解决，普通 Rust 测试不构成生产隔离验收。
结果信用按实际编码头部及记录字节分块；开始写入后若确认中断，partial 保守
为真，实际已写范围由可信 writer 保留。不可将未知写入确认当成空结果。

## 可运行的隔离接入示例

`tests/time/support.rs` 以七个独立适配对象实现上述 trait；
`Harness::run` 展示 `EnginePorts { data, execution, matcher, rules, strategy,
control, results }` 的实际接线。`tests/time/engine.rs` 的
`cross_symbol_bar_barrier_matches_all_accounts_before_one_callback` 展示初始化、
同步下单、多源 Bar、统一回调及结果信用接收：

```sh
cd engine
cargo test -p qf-core --locked --test time cross_symbol_bar_barrier
cargo test -p qf-core --locked --test time same_nanosecond_ticks
cargo test -p qf-core --locked --test time
```

这些替身全部只存在测试目录，使用 `synthetic_clock_only` 标签；账户仅模拟
时序所需的预留，撮合仅返回合成全量成交。它们没有生产数据、真实会计/费用、
真实 Python 或真实持久化，不作为完整产品闭环或性能验收。

旧 Python 时间轴仍有旧生产入口消费者，本模块未迁移控制面/共享业务，故没有
提前删除旧入口或算法；最终调用退出与物理删除由 D12/D13/D17/D18 集中完成。
未改 R01/S2 公共代码、D02 SOURCES、费用/分红税资料缺口，也未复制原任务包。

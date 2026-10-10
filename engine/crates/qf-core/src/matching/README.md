# S3-D07 Bar 撮合

`bar::BarMatcher::from_run(&RunConfig)` 实现 `bar_next_interval_v1`，直接读取
D01 已归一化、只读的参与率和滑点；没有另设默认值。支持日线、1/5/15/30
分钟与 60 分钟，频率和实际日历桶由 D03 校验。

## 价格、时间与可用量

- 资格同时要求 `submitted_ns <= interval_start_ns` 和
  `eligible_interval_start <= interval_start_ns`；资格缺失不成交。D03/D06
  检查实际有效会话，D03 保持同 close 的全部标的屏障。新命令不能倒用已经
  开始的 Bar 开盘代理，before_open 的单可以进入随后第一根 Bar。
- 市价参考合格 Bar 的 open。限价买入在 open 不高于限价时用 open，否则仅在
  low 触及时用限价；卖出完全对称。确认时间统一为 `interval_end_ns`，价格
  来源写在订单变化说明中；不标为实际开盘成交。
- 不利滑点按原始十进制比例计算，一次量化到 tick：买向上、卖向下。价格截至
  委托限价和 OHLC 区间边界。这是明示的模型约束；边界截断时实际滑点会小于
  配置值。D01 的 `ExactDecimal::slipped_price` 用已有受限宽整数计算整个比例，
  极小 bps、非十进制幂 tick 和大价格不会因先舍入比例而选错一格。
- 同标的同 Bar 的所有自有买卖委托共享 `floor(volume * participation_rate)`，
  按 D06 传入的稳定委托序分配。已受理订单的部分成交单位为整数 1 份；申报
  整手、最低量与零股仍由 D02/D06 校验，不能把余单再当新申报量化。
- 已知停牌、零可用量、限价未触及不成交，订单保持活动并输出原因。成交代理
  在涨停价不撮合买入，在跌停价不撮合卖出。一字板不保证成交；另一方向仍受
  实际量和账户预算约束。不会从 OHLC 推导真实队列、开盘可得量或盘中触价次序。
- 缺成交量、身份、日期规则、交易状态、涨跌限制或单位事实继续拒绝；所有
  可交易 OHLC 必须符合 tick 和当日价格范围。缺价不能构造合法 D01 Bar。
  区间重复或重叠拒绝，不允许通过换来源序号重复刷新成交量。

## 接缝与提交

`BarRules::new(book, instrument, day_facts, usage)` 只保存 D02 解析的一条当前
适用规则及小型日事实，没有复制规则目录或行情。D02 的历史规则/费用缺项保持
原有拒绝，模型不从代码前缀、昨收或生产当前状态猜测历史事实。

D04 的 `RulesPort<BarRules>` 必须按事件标的和实际会话提供正式事实，Bar 必须
是未复权 CNY 原价及已验证份额量。正式映射仍由 D04/底座接入；隔离事实不能
启用市场能力。撮合没有数据读取、数据库、第二份账户或费用累计器。

调用链为 D03 `consume_with_budget` → 每个候选
`FillBudget::allowance_after(prior_fills, ...)` → D06 `claim_match/commit_match`
→ 唯一 D09 账户核费及提交。后序候选先预览已选择的前序成交，不能重复消费
可用现金或预支未选择的卖款。候选失败不返回半批，D06 成交/订单/累计费用批次
失败全部不提交。matcher 的 `consume` 无预算入口明确拒绝。

滑点跳空后只成交真实可支付量，保留余单；最低佣金按订单累计核定。DAY 到期
继续由 D03/D06 在最后市场事件后处理，after_close 新 DAY 属下一实际会话。
`OrderManager::check_session_end` 继续真实转发唯一账户，driver 关闭后置检查
通过才运行收盘回调。本模块仅为同状态未成交原因变化补充 D06 原子更新，不
另建状态机；相同原因不反复输出记录。

`Matcher::model_description` 返回小型 `ExecutionModelDescription`。D03 用
已有有界 ResultSink 在 initialize 前写一次 `ResultRecord::ExecutionModel`，
无成交或初始化异常也保留模型、频率、参与率、滑点、成交单位及价格/量/路径/
队列假设。
`Engine::from_run` 检查描述和同一归一化配置一致，信用不足使运行失败。该
serde 变体和 Python wire 类型由同一生成器同步；D11/D13 应接收至结果摘要。

matcher 仅保留授权集合和每标的最后区间终点，内存与 universe 有界。不保存
Bar 历史、账户快照、逐行情成功台账，也不支持失败运行现场恢复。

## 验证与保留边界

```sh
cd engine
cargo test -p qf-core --locked --test bar_matching --test numeric
cargo test -p qf-core --locked
cd ..
QF_S3_PYTHON=python3.12 bash scripts/test_s3_engine.sh
```

`tests/bar_matching/cases.json` 是独立手算预期，标记
`synthetic_business_oracles / production_executed=false`。价格表、共享量、
累计最低费用、跳空前序预算及失败原子性用真实 D02/D06/D09；driver 用例再接
真实 D03/D07。host、数据来源和结果收集器为隔离适配，没有供应商或正式数据。
六种 Bar 频率、chunk 1/2/17、来源置换、单记录结果信用得到同语义成交；同一
频率的完整结果记录一致。

旧 `backend/app/backtesting/bar_matching.py` 仍被旧 runtime 消费，旧
`execution.py` 也仍有活依赖，因此未提前删除。新模型不调用旧 Python 算法。
生产联调、D10 绑定、D11/D13 摘要接入、D12/D13/D17 最终旧依赖切断和
P01/C01 发布/清理继续由所属模块完成。本次不改 CurrentStore、采集器、公共
UI、D05 `data/views.rs` 或 `analysis/indicators.rs`。

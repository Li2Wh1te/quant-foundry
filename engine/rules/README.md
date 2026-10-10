# D02 规则与费用

`qf_core::rules` 是离线定义及校验模块，复用 D01 的 `ExactDecimal`、`Price`、`Quantity`、`QuantityStep`、`SecurityKey`、`SessionKey`、`FeeConfig`、`CostOverrides`、`QfError`。规则依据及未核覆盖见 [SOURCES.md](SOURCES.md)。这些定义不声明生产行情、交易日历、撮合或账户服务已经可用。

- `market::RuleBook::resolve(Instrument, TradeDayFacts, RuleUse)` 按市场、产品、日期和来源选择。静态规则不携带标的当日状态；D04 提供身份、上市/退市、当前模拟时点停牌、风险及实际价格上下限，D03 提供实际会话和上市会话序号。消费方必须随状态事件刷新事实，不将开盘状态冻结为全日状态。未知、冲突、越界日期明确拒绝。
- `ResolvedTradingRule` 校验数量、权限、价格、连续竞价价格笼子、无涨跌停日集合竞价及市价申报阶段，计算可卖量和目标买入向下量化。数量订单不隐式取整；零股出售不拆分。成交校验拒绝停牌，停牌中的限价申报与成交分别处理。
- 会话模板仅引用交易所本地竞价阶段和 `Asia/Shanghai`，不生成自然日交易日历。盘后固定价格、大宗、一级申赎未提供执行能力。市价阶段校验也不模拟交易所保护限价、盘口或排队；D06/D07 声明模拟市价代理、滑点、参与率和受理/成交时点。
- `fees::CommissionConfig` 只保存已选账户的佣金、最低收费、包含项及明确结算约定。`catalog::FeeCatalog::compose` 将其与按产品、市场、投资者、方向和日期核验的四类事实合成既有 `FeeConfig`，以 `ScopedFeeConfig` 绑定执行范围。佣金覆盖仅允许既有 `CostOverrides` 两字段。
- `OrderFeeAccumulator` 每订单累计佣金及各费用未舍入金额；本次应收为累计应收减已收，最低佣金只收一次。跨日部分成交按各成交日费率累计，不重算历史成交。已含项目显示法定应收但不另扣；佣金不能低于其包含项的应收合计。撤单不再生成收费，未成交订单没有费用。
- D09 用 `preview_fill` 取得费用和下一份状态，先核对资金并原子应用成交，再采纳该状态。费用错误不改变原状态。该小状态不能跨订单重用；没有账户 CRUD、数据库、行情查询或逐行台账。
- `dividends` 定义已核验的投资者/登记日/持有期税率和支付/处置时点。D09 负责真实 FIFO 批次、登记/除权/支付、后续处置扣缴与防止双扣；本模块不是公司行动执行器。

`Official` 与带名称的 `Synthetic` 规则只能在对应 `RuleUse` 中解析。合成零成本费用必须有 `synthetic_model` 和相同来源名称；真实配置的缺失费率不可变为零。`RuleOrigin` 是计算说明及隔离标签，不是审批平台或第三方数据真实性认证。

内置交易/费用组合包含普通交易日期：2026-07-06 起沪深北正常状态股票及沪深各 ETF 细分；深股票从 2023-04-10、深 ETF 从 2025-05-01 起另有已核历史规则和完整费用。完整费用的较长区间及未核历史、北风险/退市、分红税边界以 [SOURCES.md](SOURCES.md) 为准。收费项目不适用依正式收费业务范围声明，当前表的核验日不作为费率启用日。规则与费用可解析仍需要消费方提供真实会话和当日事实。

结算精度与舍入是小型运行配置。默认会计规则仍为 D01 half-even；新增 half-up 用于已核验的沪深价格范围及显式佣金约定，`away_from_zero` 保留旧账户显式 ROUND_UP 含义。没有将货币小数位误称为各项法定收费的统一舍入规定。

## D13 账户适配契约

`backend/app/backtest_service/account_config.py::commission_from_account_fee_schedule` 接收已鉴权、已选定的现有 `fee_schedule` JSON。读取原佣金费率/最低收费；`currency` 可由已选账户明确传入，条件费率需要 `applicability_context` 的已核身份事实。缺币种、包含项、条件身份、佣金或舍入拒绝。已有 `half_up/down/up` 分别映射同一 Rust 数值类型的 `half_up/toward_zero/away_from_zero`。

包含项必须显式传入（无包含项也传 `[]`）；亦可从调用方提供的 `metadata.s3_commission` 读取。已含项目与单列旧税费重复时拒绝。适配器不采用旧市场税率、不导入旧 `fees` 计算、不写账户；旧配置保持原值。受理方捕获其小型结果，排队中原配置修改不会改变已接受结果；D13 负责持久化和生产接线。

安装 wheel 提供有界 `validate_commission_config_json` 和仅真实目录可调用的 `compose_official_fee_config_json`。后者请求包含 `scope`（Instrument/side/investor/origin）、`effective`（含首尾日期）、`commission`、可选 `cost_overrides`，返回 `scope` 与原 `fee_config` 结构。未知 JSON 字段和来源混用拒绝，错误保留 D01 安全的操作/标的/日期/字段范围。

验证：`bash scripts/test_s3_engine.sh` 包含 Rust、共享契约和真实安装 wheel 测试；D02 Rust 案例在 `qf-core/tests/{market_rules,fees,dividend_tax}.rs`，跨语言/账户适配案例在 `sdk/python/tests/test_rules.py`。市场规则断言使用明确的合成当日事实，未冒充真实行情或生产账户。

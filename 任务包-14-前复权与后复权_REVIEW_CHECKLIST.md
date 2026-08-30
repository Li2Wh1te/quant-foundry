# 任务包 14：前复权与后复权——验收检查表

本表依据 `docs/architecture/backtesting-engine-features/任务包-14-前复权与后复权.md` 冻结，作为唯一审查基线。除状态文件中的状态字段外，不在审查过程中增删检查项。

## 14-01 基线核查与契约冻结

| 编号 | 可验证验收标准 / 红线 |
| --- | --- |
| 14-01-01 | 已核查并复用 `PriceBasis.RAW/QFQ/HFQ`、`AdjustedSeriesQuery`、`AdjustedSeriesPoint`（或等价能力）、`DataProvider.adjusted_series()`、`etf_adjustment_factors`、`EtfFactsAdapter`、原始 ETF Bar、稳定 `instrument_id`/PIT 映射、预检/运行记录字段及策略视图隔离；缺失能力仅列为依赖阻断，不重复实现。 |
| 14-01-02 | 已输出基线核查记录、实际修改文件清单和依赖缺失项清单。 |
| 14-01-03 | `raw` 链路独立于复权因子；未指定口径的现有调用行为保持不变，默认口径为 `raw`。 |
| 14-01-04 | 没有新增公司行动、账户、交易日历、交易规则、PIT 映射、分钟线/Tick、候选集或通用多供应商/多资产复权实现。 |

## 14-02 真实源复权语义验算

| 编号 | 可验证验收标准 / 红线 |
| --- | --- |
| 14-02-01 | 验算制品包含真实数据源原始复权因子行和同一真实标的对应的源原生 qfq/hfq 输出，不能仅使用内存 fixture，且不含凭证。 |
| 14-02-02 | 至少覆盖一个真实标的、多个因子有效日期，以及 `data_cutoff` 位于因子边界前和边界后的样本。 |
| 14-02-03 | 记录源字段到规范化字段映射，源日期规范化为 `effective_date`，并确认 qfq/hfq 公式标识、各自锚点、精度和舍入语义。 |
| 14-02-04 | 适配器输出与源原生 qfq/hfq 逐点比较，所有必需样本在源声明精度内一致；源为空、字段含义不明、输出无法对应或精度/锚点无法确认时验算失败关闭。 |
| 14-02-05 | 验算输入、输出、证据摘要、policy key/version、适配器版本和来源批次信息已版本化，输入 hash、输出 hash、证据 hash 可复现。 |
| 14-02-06 | 不使用经验公式、公司行动事件或原始 Bar 重新推导缺失复权公式；验算失败时 policy 保持 `inactive`。 |

## 14-03 policy 描述与激活门禁

| 编号 | 可验证验收标准 / 红线 |
| --- | --- |
| 14-03-01 | 已注册唯一首版 policy `tushare_adj_factor_native@1`，状态仅允许 `inactive/active`，默认 `inactive`。 |
| 14-03-02 | policy 是不可变描述，包含 adapter_version、source、factor_field、effective_date、cutoff_rule、qfq/hfq 公式标识、qfq/hfq 锚点、precision、rounding、verification 证据摘要及 hash。 |
| 14-03-03 | 构造 `active` policy 时强制校验全部证据字段、key/version、适配器版本和 hash；缺证据、错误 key/version 或未发布验算制品时构造失败。 |
| 14-03-04 | 删除或限制运行时 `adjustment_active=True` 等绕过式激活；兼容参数只能映射到已验证固定 policy，不能绕过证据校验。 |
| 14-03-05 | policy 对象与策略视图只读，策略运行时不能修改 policy。 |
| 14-03-06 | qfq/hfq 在 policy 未激活、key/version 不匹配、证据不完整时预检结果为 `blocked`，且不得调用备用公式；raw 不受该门禁影响。 |

## 14-04 因子规范化与截止读取

| 编号 | 可验证验收标准 / 红线 |
| --- | --- |
| 14-04-01 | 读取 `etf_adjustment_factors` 当前权威记录，不新增历史修订表；保留源值与规范化 `effective_date` 的可审计关系。 |
| 14-04-02 | 严格按请求边界对应的市场本地日期执行 `effective_date <= data_cutoff`；不使用系统当前时间推断 cutoff。 |
| 14-04-03 | 不以 `updated_at` 作为历史认知时间过滤条件，不根据公司行动事件重算因子。 |
| 14-04-04 | 因子执行正数、有限数值、日期、源代码、身份和重复性校验；缺失、重复、越界、错误代码或错误身份时整次查询阻断，不返回缩短序列。 |
| 14-04-05 | 复用现有 PIT 代码映射分段读取；因子读取不改变 raw Bar，且不会把 policy 标记为 `non_strict_pit`。 |

## 14-05 qfq/hfq 研究价格序列

| 编号 | 可验证验收标准 / 红线 |
| --- | --- |
| 14-05-01 | 研究层读取所需 raw Bar 和 cutoff 内因子，由 ETF 适配器内部使用冻结的源原生算法生成价格，并按源精度/舍入规则输出。 |
| 14-05-02 | qfq 与 hfq 分别使用自己的公式和锚点，结果显式标记 `price_basis=qfq` 或 `price_basis=hfq`，不把因子数组直接当价格序列。 |
| 14-05-03 | qfq 缺失不回退 hfq/raw，hfq 缺失不回退 qfq/raw；研究价格覆盖不完整时阻断。 |
| 14-05-04 | 策略数据视图显式支持 `raw/qfq/hfq` 选择，未指定仍为 raw；研究价格不写回 raw 表。 |
| 14-05-05 | 因子证据和 policy 版本随研究结果可供预检、运行记录和审计使用；通用回测引擎不读取或解释 qfq/hfq 公式。 |

## 14-06 预检、hash 和运行记录

| 编号 | 可验证验收标准 / 红线 |
| --- | --- |
| 14-06-01 | qfq/hfq 预检检查 policy key/version、active 状态、适配器版本、验算证据摘要、请求窗口因子/研究价格覆盖和 cutoff 边界样本。 |
| 14-06-02 | 预检机器数据和 hash 包含 adjustment_series_policy、policy_status、adapter_version、formula_version、qfq_anchor、hfq_anchor、factor_cutoff_rule、verification_input_hash、verification_output_hash、verification_evidence_hash、factor_coverage。 |
| 14-06-03 | 同一复权契约摘要写入运行记录；改变 policy、适配器版本、验算证据或因子覆盖时 hash 变化，相同输入 hash 稳定。 |
| 14-06-04 | hash 不包含生成时间、凭证或敏感源数据；复权因子状态不会自动填充 `non_strict_pit_capabilities`。 |
| 14-06-05 | 状态矩阵正确：raw+inactive/active 允许；qfq/hfq+inactive blocked；qfq/hfq+active 且证据完整允许；key/version 不匹配或因子覆盖不完整 blocked。 |

## 14-07 价格口径隔离

| 编号 | 可验证验收标准 / 红线 |
| --- | --- |
| 14-07-01 | 运行级 engine price basis 保持 raw；目标数量换算、订单撮合、手续费、账户估值和会计链路均只读取 raw。 |
| 14-07-02 | qfq/hfq 数据进入 engine-only 入口时直接报告 provider contract violation 或等价阻断；raw 与研究口径在对象、查询或边界校验上可区分。 |
| 14-07-03 | 未新增或修改公司行动、会计、账户、成交、撮合、手续费和估值算法。 |

## 14-08 自动化测试和验收

| 编号 | 可验证验收标准 / 红线 |
| --- | --- |
| 14-08-01 | 自动化测试覆盖 policy 激活门禁：未激活 qfq/hfq 阻断、缺证据/错误 key-version 失败、policy 不可变、raw 仍可读。 |
| 14-08-02 | 自动化测试覆盖真实源验算输入/输出、真实标的多日期、cutoff 前后边界、精度/舍入不一致失败和 hash 可复现。 |
| 14-08-03 | 自动化测试覆盖 cutoff/覆盖校验：截止前可见、截止后不可见、缺失/重复/非法因子/错误源代码/错误身份阻断、不使用 updated_at、不触发 non_strict_pit。 |
| 14-08-04 | 自动化测试覆盖 qfq/hfq 输出：basis 标记、各自源原生输出、不互相/向 raw 回退、不写回 raw、不把因子点当价格点。 |
| 14-08-05 | 自动化测试覆盖策略视图显式口径选择、默认 raw、raw 请求不触发因子读取，以及成交/数量换算/费用/估值入口拒绝 qfq/hfq。 |
| 14-08-06 | 自动化测试覆盖预检/运行记录字段、hash 变化与稳定性、敏感信息不进入记录。 |
| 14-08-07 | 全部相关自动化测试通过；代码无语法错误；测试未引入超出任务文档边界的实现。 |


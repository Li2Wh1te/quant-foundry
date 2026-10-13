# 默认真实成功的最小后续范围

本清单说明尚缺的生产者和源语义，不授权采集、迁移、生产写入或费用。现有身份、日历、ETF 规则包、停牌覆盖、公司行动和佣金桥接已在 `b7963c0b56317c4cf7b05affd7f3cc6eea0c66c5` 完成真实 SQL 验证。续修已实现 S3 最终装配及原 Rust 会话/费用组合；默认 resolver 仍缺源/执行生产者，不能真实成功。普通行缺失、未定义的源语义与仍缺的非零行动适配分开；不重开 R01、不建治理平台。

## 最小范围选择

先选一个已核官方规则覆盖期内的市场、明确 ETF Product 细分和少量标的，接通 **raw 日线默认执行**；研究功能逐项接通有事实的范围。原 S2 第一批和大基准范围不变。这是可交用户的范围选择，并非本 PR 已完成或已获新增权限。

| 必要内容 | 最小生产者/解释 | 主要文件 | 迁移、采集、费用 |
|---|---|---|---|
| E50/E70 执行单位及 raw 基础 | 版本化、代码声明且有来源依据的源解释：币种、raw 价格基础、精确 quantity→shares；依标的/日期有效。E70 的 yuan/lots/千元不等于已证明 raw 或固定 ×100；ETF 最小申报量不是供应商 vol 的定义 | `data_ingestion/record_schemas.py`、相应 schema/adapter registry、`backtest_service/formal_runtime.py`、`formal_resolver.py` | 若已有原件支持纯解释可先不新增采集/迁移；若原件不支持需限定补采。本次没有调用供应商，无法承诺费用 |
| Product、上市和当日执行事实 | 稳定 identity 关联的强类型窄事实：Product、真正 first/last trading date、verified_through、listing kind/session ordinal、风险/盘中状态、实际 lower/upper 或明确 no-limit、fund_price_band/underlying_same_session、退市序号。复用已定义停牌事实，不从零事件证明推导其他维度 | `instruments/domain.py`/事实生产边界、`data_ingestion/models/trading_calendar.py` 及对应 schemas/services；S3 `formal_resolver.py` → 原 Rust TradeDayFacts | 现有字段可直接桥接，额外字段需要一个窄 companion fact schema/迁移或经确认的既有承载；不是审批表。源没有历史事实时需限定补采，费用待源确认 |
| 日历覆盖行 | 现有 CalendarSessionFact 生产者提供全周/月每个自然日及下两开放会话，known_at/quality/version/evidence 完整。续修提供与 next_session 同键的完整 following_session DTO；跨月的下一会话也读取其全周/月证明，不能拿第二个会话字符串替代 | `calendars/repository.py`、现有 ingestion 边界；S3 `formal_resolver.py::_assemble_calendar` | 不需要新契约/迁移；是否要补采取决于既有正式事实行，本次不核验或改生产 |
| 法定费用、权限及公司行动税务 scope | 续修已调用原 Rust RuleBook/FeeCatalog，按实际 Product/日期/投资者组成两侧费用，保留已捕获佣金及其覆盖项/舍入。仍需真实权限、sale cash timing、适用投资者/税务/结算依据；不能默认权限 True 或缺失费率为零 | `backtest_service/account_config.py`、既有账户/权限事实边界、`formal_assembly.py`；`qf-python/src/formal_assembly.rs`、原 `qf-core/src/rules`/D09 | 原目录和原佣金无需新采集。未知 scope 需强类型窄输入；新字段是否迁移由选定承载决定，不另建费用引擎 |
| 公司行动执行范围 | 已实现逐开放会话、逐标的、逐适用类型的完整零事件投影。仅 fund_div/cash_dividend 零行不证明股份分配、split 或 rights_issue 也为零。非零事件仍缺真实 public_ns、record/ex/payment 的原 Rust phase、同边界 sequence、raw mark 与投资者税务/份额条款；原 after_open_match 不可改写成 before_open | 原 `corporate_action` repository/schema；S3 `formal_assembly.py::zero_action_projection` → D09 DTO | 全类型零证明可复用原覆盖行，无新契约/迁移。非零映射需要最小源语义补充或窄事实承载；本次未新增生产者、采集或迁移 |
| 研究/因子历史语义 | 逐能力生产可靠 public/effective ns、typed unit、稳定 identity、完整成员集合证明；因子须有累计公式及 `{factor,effective_ns,public_ns}` 锚点。复用 D05 校验和因子计算。E69 created_at/updated_at、当前财务/估值/成分不能充历史公开时间 | 相应 `data_ingestion` record schemas/生产者、adapter registry；`research_gateway.py`、`formal_resolver.py` → 原 D05 ResearchBinding | 已有字段不能证明上述语义；需要窄 source schema/解释及可能补采/迁移。新采集或许可费用均未获验证，不承诺免费 |

## 已完成的代码与尚缺的默认正例

`formal_assembly.py::assemble_run_plan` 已将原 MarketBinding、有界日历、稳定身份、捕获佣金和 ExecutionSessionInput 交给原 Rust 校验，并形成 AcceptedRunPlan；会话事实按最多 256 项分页，资源预算取已捕获 WorkerLimits，同一小事实 token 在缓存读取和最终事务复核。输入只能来自代码中的生产者投影，不能由 REST/参数/账户 metadata 选择，也不接受调用方算好的 fees/origin。缺研究语义只关闭对应 route；其余研究绑定及受限参考指数仍走原 D05。

当前默认 `_resolve` 不产生 `market` 或 `execution_inputs`；完整零行动则已直接从原覆盖行推导，且缺证明时保留 None。`execution_plan_assembly=fact_missing` 的 `scope.adapter=implemented` 只说明最终装配代码存在，missing 逐项列明实际依赖；它不声明源/执行事实生产者已经接入或默认运行可用。非零行动仍单独拒绝。真实默认成功仍未关闭。

默认 `create_app/create_services` 必须重跑预检→提交→实际固定镜像 runner→受限 worker→SDK→PG/CurrentStore→结果；不能注入 AcceptedRunPlan、替换成功 provider、传接受标志或用缺数据关闭来代替正例。逐项缺事实、不可靠公开时间/锚点、越界、运行中质量/数据及小事实版本变化仍要拒绝；仅重测受影响场景。

## 交父确认的最小可选范围

1. **raw 日线最小闭环**：一个已核日期区间、一个明确 ETF Product、1–3 标的；只支持普通交易状态及有完整全适用类型零公司行动证明的区间。补有来源依据的 CNY/raw/精确份额解释，以及原 Instrument/TradeDayFacts/TradePermissions、投资者、sale cash timing/settlement 的窄生产者输出；复用原身份、日历、规则、佣金、覆盖 repositories。研究能力继续按实际事实逐项开关。若原件已能证明单位，只加版本化代码解释；否则需确认窄源补采。上市/实际限价/权限事实的承载缺失时，由父选择一个 S3 窄 companion schema 或确认既有承载；会涉及局部字段/迁移代码，但不改 R01 存储/迁移、不做生产 DML、不增加 S2 批次。
2. **再开一个研究能力**：在上项之外，仅选一个需要的 kind，补该 kind 的历史 public/effective 时间、typed unit 和稳定 identity。adjustment 额外需要累计公式及真实锚点；index_stocks 额外需要完整成员集合证明。可能需要窄 source schema/解释和历史补采；许可及费用需先核源。本次不执行此选项，也不承诺无需付费。

非零公司行动及更广市场/产品/频率不属于上项隐含授权。这里请求的是窄事实生产者/源解释范围选择；不是允许凭借现有记录数量、日历空洞或账户 metadata 生成成功事实。

## 已有契约与拒绝证据

- 身份有效期/knowledge/evidence：`instruments/domain.py::InstrumentIdentityFact` 与 `identity_repository.py`、代码映射 repository；这些没有上市首日或 Product 细分。
- 严格日历及 hash：`calendars/facts.py`、`models.py`、`repository.py`；能力声明只承载三种交易状态，不能塞进 raw/因子准入。
- 原 ETF 字段及 hash/fixture gate：`InstrumentRuleFactRecord`、`RulePackageResolver`；字段并不完整表达 S3 ListingWindow/实际上下限/fund_price_band。
- 公司行动现金/数量/phase/currency/timing/version/Coverage：`data_ingestion/models/corporate_action.py` 和原 repository/service。完整零事件与没有证明不同；原件的登记/除权有效区间也不能伪作每日价格或研究发布时间。
- 官方规则与费用范围：原 `engine/rules/README.md`、`SOURCES.md` 和 `rules/catalog.rs`；只读导出原 RuleBook，未重写计算或宣称未核历史已支持。
- E50/E70/E69 和财务/估值/成分的语义缺口：原 record schemas、registry 和 `market_projection.py`。本 PR 未把可选字段 None、provider_reported 或观测时间改成成功事实。

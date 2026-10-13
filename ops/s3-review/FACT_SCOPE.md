# 默认真实成功的最小后续范围

本清单说明尚缺的生产者与代码，不授权采集、迁移、生产写入或费用。现有身份、日历、ETF 规则包、停牌覆盖、公司行动和佣金桥接已在 `b7963c0b56317c4cf7b05affd7f3cc6eea0c66c5` 完成真实 SQL 验证。普通行缺失与契约不存在分开；不重开 R01、不建治理平台。

## 最小范围选择

先选一个已核官方规则覆盖期内的市场、明确 ETF Product 细分和少量标的，接通 **raw 日线默认执行**；研究功能逐项接通有事实的范围。原 S2 第一批和大基准范围不变。这是可交用户的范围选择，并非本 PR 已完成或已获新增权限。

| 必要内容 | 最小生产者/解释 | 主要文件 | 迁移、采集、费用 |
|---|---|---|---|
| E50/E70 执行单位及 raw 基础 | 版本化、代码声明且有来源依据的源解释：币种、raw 价格基础、精确 quantity→shares；依标的/日期有效。E70 的 yuan/lots/千元不等于已证明 raw 或固定 ×100；ETF 最小申报量不是供应商 vol 的定义 | `data_ingestion/record_schemas.py`、相应 schema/adapter registry、`backtest_service/formal_runtime.py`、`formal_resolver.py` | 若已有原件支持纯解释可先不新增采集/迁移；若原件不支持需限定补采。本次没有调用供应商，无法承诺费用 |
| Product、上市和当日执行事实 | 稳定 identity 关联的强类型窄事实：Product、真正 first/last trading date、verified_through、listing kind/session ordinal、风险/盘中状态、实际 lower/upper 或明确 no-limit、fund_price_band/underlying_same_session、退市序号。复用已定义停牌事实，不从零事件证明推导其他维度 | `instruments/domain.py`/事实生产边界、`data_ingestion/models/trading_calendar.py` 及对应 schemas/services；S3 `formal_resolver.py` → 原 Rust TradeDayFacts | 现有字段可直接桥接，额外字段需要一个窄 companion fact schema/迁移或经确认的既有承载；不是审批表。源没有历史事实时需限定补采，费用待源确认 |
| 日历覆盖行 | 现有 CalendarSessionFact 生产者提供全周/月每个自然日及下两开放会话，known_at/quality/version/evidence 完整。已有窗口、序号和 callback 投影代码已完成 | `calendars/repository.py`、现有 ingestion 边界；S3 `formal_resolver.py::_assemble_calendar` | 不需要新契约/迁移；是否要补采取决于既有正式事实行，本次不核验或改生产 |
| 法定费用、权限及公司行动税务 scope | 在已选产品/日期/投资者及权限事实下调用原 Rust RuleBook/FeeCatalog；佣金仍取受理捕获的现有明确包含项/舍入。补 sale cash timing、适用投资者/税务/结算依据，不能默认权限 True 或未适用项目零费率 | `backtest_service/account_config.py`、既有账户/权限事实边界、`formal_resolver.py`；`qf-core/src/rules`/D09 | 原目录和原佣金无需新采集。未知 scope 需强类型窄输入；新字段是否迁移由选定承载决定，不另建费用引擎 |
| 研究/因子历史语义 | 逐能力生产可靠 public/effective ns、typed unit、稳定 identity、完整成员集合证明；因子须有累计公式及 `{factor,effective_ns,public_ns}` 锚点。复用 D05 校验和因子计算。E69 created_at/updated_at、当前财务/估值/成分不能充历史公开时间 | 相应 `data_ingestion` record schemas/生产者、adapter registry；`research_gateway.py`、`formal_resolver.py` → 原 D05 ResearchBinding | 已有字段不能证明上述语义；需要窄 source schema/解释及可能补采/迁移。新采集或许可费用均未获验证，不承诺免费 |

## 尚缺的代码验收

新增生产者范围确定后，由同一 resolver 将上述受限 DTO、已有 calendar/rules/commission/action 事实和 CurrentStore 原节点组成 AcceptedRunPlan；将有事实的研究 mapping/受限参考指数送至原 D05 网关。当前 `execution_plan_assembly=adapter_missing` 公开表示这一步还没有实现。

默认 `create_app/create_services` 必须重跑预检→提交→实际固定镜像 runner→受限 worker→SDK→PG/CurrentStore→结果；不能注入 AcceptedRunPlan、替换成功 provider、传接受标志或用缺数据关闭来代替正例。逐项缺事实、不可靠公开时间/锚点、越界、运行中质量/数据及小事实版本变化仍要拒绝；仅重测受影响场景。

## 已有契约与拒绝证据

- 身份有效期/knowledge/evidence：`instruments/domain.py::InstrumentIdentityFact` 与 `identity_repository.py`、代码映射 repository；这些没有上市首日或 Product 细分。
- 严格日历及 hash：`calendars/facts.py`、`models.py`、`repository.py`；能力声明只承载三种交易状态，不能塞进 raw/因子准入。
- 原 ETF 字段及 hash/fixture gate：`InstrumentRuleFactRecord`、`RulePackageResolver`；字段并不完整表达 S3 ListingWindow/实际上下限/fund_price_band。
- 公司行动现金/数量/phase/currency/timing/version/Coverage：`data_ingestion/models/corporate_action.py` 和原 repository/service。完整零事件与没有证明不同；原件的登记/除权有效区间也不能伪作每日价格或研究发布时间。
- 官方规则与费用范围：原 `engine/rules/README.md`、`SOURCES.md` 和 `rules/catalog.rs`；只读导出原 RuleBook，未重写计算或宣称未核历史已支持。
- E50/E70/E69 和财务/估值/成分的语义缺口：原 record schemas、registry 和 `market_projection.py`。本 PR 未把可选字段 None、provider_reported 或观测时间改成成功事实。

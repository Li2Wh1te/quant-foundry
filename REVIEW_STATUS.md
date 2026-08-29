# 任务包 13 审查状态

状态枚举：待检查 / 通过 / 失败 / 阻塞。

本文件是审查进度唯一权威记录；初始状态全部为“待检查”。

| 编号 | 状态 | 结论/证据 | 失败原因 | 复审记录 |
| --- | --- | --- | --- | --- |
| A1 | 通过 | `PYTHONPATH=. .venv/bin/python -m unittest ...` 联合套件 273/273；Provider 完整路径构造 InstrumentSpec |  |  |
| A2 | 通过 | Provider 测试 `test_missing_exchange_is_structured_block` 通过，返回 `identity_exchange_missing` |  |  |
| A3 | 通过 | Provider 测试 `test_missing_identity_is_structured_block` 通过；无 PIT 身份不生成正式规格 |  |  |
| A4 | 通过 | `InstrumentDisplay`/Provider 缺失展示测试通过，未从交易代码伪造名称 |  |  |
| A5 | 通过 | `resolve_code_mappings` 与分段历史测试通过，历史代码变更返回同一稳定身份 |  |  |
| A6 | 通过 | 映射缺口测试通过，稳定错误码为 `identity_mapping_incomplete` |  |  |
| A7 | 通过 | `ResolveCodeMappingsTestCase`/PIT 冲突测试通过并返回结构化冲突码 |  |  |
| A8 | 通过 | 无 evidence 映射测试通过，返回 `identity_mapping_evidence_missing` |  |  |
| A9 | 通过 | Repository/PIT/Provider cutoff 测试通过，无 latest 或未来事实回退 |  |  |
| A10 | 通过 | Registry/Provider/预检测试通过，精确版本失败码为 `RULE_PACKAGE_MISMATCH` |  |  |
| A11 | 通过 | 缺少必填字段测试通过，`lot_size` 缺失返回 `RULE_REQUIRED_FIELD_MISSING` |  |  |
| A12 | 通过 | 规则预检与规则包测试覆盖 `price_tick` 缺失并确认无默认值 |  |  |
| A13 | 通过 | `test_missing_calendar_blocks` 与预检阻断测试通过，缺 session template 不放行 |  |  |
| A14 | 通过 | Provider 与预检测试覆盖 currency 缺失/冲突，结果为 `blocked` |  |  |
| A15 | 通过 | 规则预检与 `InstrumentSpec` 数值测试通过，非 lot 倍数不构造规格 |  |  |
| A16 | 通过 | `FactSelectionTestCase.test_incomplete_quality_blocks` 及联合套件通过 |  |  |
| A17 | 通过 | `ModeGateTestCase` 通过，formal 读取 fixture 被稳定阻断 |  |  |
| A18 | 通过 | 预检分段与快照测试通过，普通事实变更产生多段区间 |  |  |
| A19 | 通过 | `NamedExceptionTestCase.test_exception_overrides_fields_via_referenced_fact` 通过 |  |  |
| A20 | 通过 | `ExceptionFactCompletenessTestCase` 通过，例外缺字段整体阻断 |  |  |
| A21 | 通过 | 例外 schema 与 append 边界测试通过，未提供生产规则值列 |  |  |
| A22 | 通过 | `ContentHashTestCase.test_entry_input_order_does_not_change_the_hash` 通过 |  |  |
| A23 | 通过 | 例外重叠与普通/例外双重冲突测试通过，冲突参与者进入审计 hash |  |  |
| A24 | 通过 | 首期结算类别测试通过，`t1_before_open_match` 可生成正式结果 |  |  |
| A25 | 通过 | `test_unknown_settlement_class_is_reported_as_unknown` 通过 |  |  |
| A26 | 通过 | same-day 与 T+1 非首期类别测试通过，稳定阻断码为 `RULE_SETTLEMENT_UNSUPPORTED` |  |  |
| A27 | 通过 | capability required 缺失测试通过，返回 `RULE_CAPABILITY_FACT_MISSING` |  |  |
| A28 | 通过 | `test_not_applicable_declaration_is_preserved_into_the_snapshot` 通过 |  |  |
| A29 | 通过 | `DataRequest.from_admission`/固定集合测试通过，静态标的缺规则不能创建运行 |  |  |
| A30 | 通过 | `test_fixed_instrument_union_includes_non_zero_positions` 与准入绑定测试通过 |  |  |
| A31 | 通过 | `test_single_instrument_path_never_queries_universe` 等测试通过，返回完整规格或结构化不合格结果 |  |  |
| A32 | 通过 | Provider 资格失败测试通过，错误码/来源可审计且无默认规格 |  |  |
| A33 | 通过 | `BlockedPathTestCase` 与报告不变式测试通过，blocked 不生成快照 |  |  |
| A34 | 通过 | READY 路径/快照 round-trip 测试通过，bundle 含完整事实引用与 provenance |  |  |
| A35 | 通过 | `RuntimeSnapshotAcceptanceTests.test_runtime_uses_frozen_lot_and_returns_snapshot_identity` 通过 |  |  |
| A36 | 通过 | snapshot persistence 与 runtime 篡改测试通过，hash 不匹配直接失败 |  |  |
| A37 | 通过 | 规则包、例外、快照及数据请求顺序稳定性测试通过 |  |  |
| A38 | 通过 | adapter 测试覆盖无 Provider/None 返回阻断；源码无 `_ETF_SPEC_DEFAULTS`/`_ETF_CAPABILITIES` |  |  |
| A39 | 通过 | `CrossCodeAdapterTestCase`、`NoNetworkTestCase` 通过，读取路径无外部网络 |  |  |
| A40 | 通过 | 范围静态扫描及联合测试通过，未扩展任务包明确排除的能力 |  |  |
| R1 | 通过 | 领域、PIT 映射、Provider 与 adapter 测试确认稳定身份主键语义 |  |  |
| R2 | 通过 | 领域数值/不可变投影测试与 Provider 完整性测试通过 |  |  |
| R3 | 通过 | 身份/映射/规则事实/快照统一执行 PIT 时间语义，相关测试通过 |  |  |
| R4 | 通过 | 规则事实、例外集合、快照 hash 漂移与 formal/fixture 分离测试通过 |  |  |
| R5 | 通过 | 规则注册、字段契约、结算门禁与 Provider 测试通过；未发现生产数值默认 |  |  |
| R6 | 通过 | 例外 schema、Repository 与 Resolver 顺序/hash 测试通过 |  |  |
| R7 | 通过 | Provider/预检阻断矩阵及准入绑定测试通过，formal 失败关闭 |  |  |
| R8 | 通过 | `DataPreflightRequest` 固定并集及 dynamic 初始持仓测试通过 |  |  |
| R9 | 通过 | 快照 Repository/runtime/hash 测试通过，write-once 且不查询 live facts |  |  |
| R10 | 通过 | adapter `NoNetworkTestCase` 与历史映射测试通过，网络客户端未引入 |  |  |
| R11 | 通过 | 变更范围扫描与任务包测试确认无后续功能扩展 |  |  |
| R12 | 通过 | `test_task13_runtime_rule_snapshots` 与身份迁移测试 48/48（4 skip）通过，未覆盖用户值 |  |  |
| R13 | 通过 | runtime 快照验收测试验证 snapshot identity、resolution hash 与实际 segment 审计字段 |  |  |
| R14 | 通过 | 联合定向套件 273/273，迁移/快照套件 48/48（4 skip），compileall 与 diff check 通过 |  |  |
| R15 | 通过 | Provider/preflight 单标的接口测试通过，复用 Resolver/版本/hash，未扩展候选编排 |  |  |

# 任务包 13 审查检查表

来源：`docs/architecture/backtesting-engine-features/任务包-13-标的规格.md`

本表冻结后作为唯一审查基线。状态记录见 `REVIEW_STATUS.md`。

## 验收项

| 编号 | 检查内容 | 通过标准 |
| --- | --- | --- |
| A1 | 完整 ETF 身份、规则事实和日历事实 | 成功生成完整 `InstrumentSpec`。 |
| A2 | 身份缺少 exchange | Formal 规格解析/预检为 `blocked`。 |
| A3 | 只有当前 ETF 目录、无 PIT 身份事实 | 不生成正式规格。 |
| A4 | 展示字段缺失 | 保留稳定 ID，展示字段为 `None`，不伪造名称。 |
| A5 | 交易代码历史变更 | PIT 映射按日期分段，仍返回同一 `instrument_id`。 |
| A6 | 映射缺口 | 返回 `identity_mapping_incomplete` 并阻断。 |
| A7 | 映射重叠 | 返回 `identity_mapping_conflict` 并阻断。 |
| A8 | 映射无 evidence | 返回 `identity_mapping_evidence_missing` 并阻断。 |
| A9 | cutoff 后才知道的映射或事实 | 对查询不可见，不得使用。 |
| A10 | 规则包不存在或版本错误 | 返回 `RULE_PACKAGE_MISMATCH`（或等价稳定阻断码）。 |
| A11 | 缺 `lot_size` | 返回 `RULE_REQUIRED_FIELD_MISSING`，不使用默认值。 |
| A12 | 缺 `price_tick` | 返回 `RULE_REQUIRED_FIELD_MISSING`，不使用默认值。 |
| A13 | 缺 session template | 规则预检 `blocked`。 |
| A14 | 缺 currency | ETF 规则预检 `blocked`。 |
| A15 | `minimum_order_quantity` 非 lot 整数倍 | 返回 `RULE_FIELD_INVALID`。 |
| A16 | 事实质量为 incomplete | 返回 `RULE_FACT_NOT_COMPLETE`。 |
| A17 | formal 读取 fixture | 返回 `RULE_FIXTURE_SOURCE_FORBIDDEN`。 |
| A18 | 普通事实中途变更 | 生成多个规则快照分段。 |
| A19 | 具名例外覆盖普通事实 | 有效区间使用 exception fact。 |
| A20 | 例外事实缺字段 | 阻断，不从普通事实补齐。 |
| A21 | 例外集合项存生产值 | Domain/Repository 拒绝。 |
| A22 | 例外集合顺序变化 | hash 不变。 |
| A23 | 普通事实和例外事实均冲突 | 整个标的阻断。 |
| A24 | settlement=`t1_before_open_match` | 首期正式允许。 |
| A25 | settlement 为未知类别 | 返回 `RULE_SETTLEMENT_UNKNOWN`。 |
| A26 | settlement 为已知但非首期类别 | 返回 `RULE_SETTLEMENT_UNSUPPORTED`。 |
| A27 | required 状态事实缺失 | 返回 `RULE_CAPABILITY_FACT_MISSING`。 |
| A28 | 状态声明为 not_applicable | 不读取该事实，但声明进入报告和快照。 |
| A29 | 静态指定标的缺规则 | 运行创建被拒绝。 |
| A30 | 非零初始持仓缺规则 | 运行创建被拒绝。 |
| A31 | 任务包 15 请求单标的规格资格 | 返回完整 `InstrumentSpec` 或结构化不合格结果，不查询动态候选集。 |
| A32 | 任务包 15 请求规则缺失标的 | 返回稳定资格失败码和 provenance，不提供默认规格。 |
| A33 | blocked 规则预检 | 不生成规则快照。 |
| A34 | READY 规则预检 | 生成完整 bundle，包含所有事实引用和 provenance。 |
| A35 | 运行创建后修改 live rule fact | 既有运行仍使用原快照。 |
| A36 | 快照内容被篡改 | hash 校验失败，运行失败。 |
| A37 | 输入标的、事实、例外顺序变化 | resolution/report/snapshot hash 不变。 |
| A38 | ETF adapter 正式路径 | 不使用任何生产规格默认常量。 |
| A39 | ETF adapter 读取历史行情 | 只通过稳定 ID 和 PIT 映射读取，不触发网络。 |
| A40 | qfq/hfq、公司行动、分钟线、Tick 等请求 | 本任务不新增行为，不纳入本任务实现。 |

## 红线与边界项

| 编号 | 红线检查 | 通过标准 |
| --- | --- | --- |
| R1 | 稳定主身份 | 策略、持仓、订单、成交和历史行情关联只使用稳定 `instrument_id`；交易代码仅作 PIT 映射或展示。 |
| R2 | InstrumentSpec 完整性 | 交易关键字段无默认值或 `None` 半规格；字段、Decimal、精度、tick、lot、乘数、区间和不可变能力均强校验。 |
| R3 | PIT 时间语义 | `effective_at` 与 `data_cutoff` 分离；事实使用半开区间和 `known_at <= data_cutoff`。 |
| R4 | 事实来源与质量 | 正式仅接受完整、可审计、有证据事实；fixture 仅显式测试模式可用；读取重新校验 content hash。 |
| R5 | 规则包边界 | 正式路径精确加载 `china_listed_etf_rules@1`；规则包不含 ETF 生产数值默认；首期只允许 `T+1-before-open-match`。 |
| R6 | 例外集合边界 | 例外集合只保存稳定 ID、事实引用和区间，不保存生产规则值；普通事实→例外事实顺序固定。 |
| R7 | 预检阻断 | 身份、映射、规则、例外、能力、会话、质量、版本或快照任一缺失/冲突/过期时 formal 整体 `blocked`，不得 degraded 放行。 |
| R8 | 固定集合 | 预检集合为 static、mandatory 与非零初始持仓 ID 的并集，dynamic 模式也不得跳过非零初始持仓。 |
| R9 | 快照不可变 | READY 与运行创建同事务写入；runtime/execution policy 只读快照，不查询 live facts；hash 不依赖 run_id、写入时间、数据库 ID、输入顺序或展示消息。 |
| R10 | 数据访问边界 | 回测读取不触发外部网络；adapter 不实现新交易规则、结算行为或日历算法。 |
| R11 | 后续功能隔离 | 不新增/扩展复权、公司行动、分钟线、Tick、撮合、覆盖度、修订链、Runner/Supervisor、策略工作台、动态候选编排、策略终止或其他非 13 号任务能力。 |
| R12 | 迁移安全 | Migration 仅补齐本任务字段/约束，不覆盖用户值、不猜测回填历史事实、不新增后续功能表，空库和已有数据可执行并可 downgrade。 |
| R13 | 运行审计 | 运行配置、运行记录和结果审计保存规则包、例外、事实版本、来源、content/resolution/snapshot hash 及实际使用的分段。 |
| R14 | 测试覆盖 | 领域、Repository、PIT、规则解析、预检、快照、runtime、策略边界、迁移、formal/fixture 分离及读取前失败顺序均有自动化验证。 |
| R15 | 任务包 15 接口边界 | 单标的资格接口复用同一解析顺序、版本和 hash 语义；不生成动态候选、不做排序/合并、不校验策略目标、不终止运行。 |


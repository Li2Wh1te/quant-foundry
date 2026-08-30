# 任务包 15：PIT 候选集审查检查表

> 基线来源：`任务包-15-PIT候选集.md`
>
> 冻结时间：2026-08-30
>
> 冻结规则：本文件是任务包 15 后续审查的唯一验收基线；冻结后不得增加、删除、合并或拆分检查项。审查状态只记录于同目录的 `任务包-15-PIT候选集_REVIEW_STATUS.md`。

## 检查项

| 编号 | 类别 | 验收要求 | 必须核验的证据或方法 |
| --- | --- | --- | --- |
| DEP-01 | 依赖与职责 | 任务包 13 仅提供单标的规格/资格能力，动态候选编排与最终复检职责唯一归属任务包 15。 | 接口调用链、范围检查。 |
| DEP-02 | 依赖与职责 | 任务包 14 不是 raw 候选资格硬依赖，qfq/hfq 只按请求消费。 | 依赖图、导入与调用检查。 |
| DEP-03 | 依赖与职责 | 缺失 G15-1～G15-4 或 G15-5A 所需能力时结构化 blocked/过滤，不复制数据底座或用默认值补齐。 | 缺能力测试、代码检查。 |
| DEP-04 | 依赖与职责 | G15-5B、G15-6、G15-7、G15-8 未满足时 formal 路径不得标记 ready；仅允许显式 `internal_link_acceptance@1`。 | formal/internal profile 测试。 |
| DEP-05 | 依赖与职责 | 任务包 15 只消费 16A 覆盖资格契约，不实现 16B 正式准入、18/19 的事实生产或 17 的 token 签发。 | 变更范围与模型检查。 |
| DEP-06 | 依赖与职责 | 不修改业务代码的 T15-00 契约核查结果须记录实际修改范围、依赖缺口与 blocked 行为。 | 追溯表与 diff。 |
| DOM-01 | 领域契约 | `UniverseScopeResolution`、`CandidateEligibilityContext`、`CandidateEligibility` 为不可变契约，字段覆盖任务包规定的范围、证据、原因和快照信息。 | 类型定义与不可变性测试。 |
| DOM-02 | 领域契约 | 固定、动态、混合模式沿用现有 `InstrumentScopeMode`、`UniverseQueryPolicy`，不存在第二套同义模型。 | 类型/导入检查。 |
| DOM-03 | 领域契约 | `effective_date` 与 `data_cutoff` 分离且均为必需 PIT 边界，不合并为 `as_of`。 | 契约与边界测试。 |
| DOM-04 | 领域契约 | 候选级过滤与请求级阻断有不同结果语义；候选不合格不误报 Provider 内部异常。 | 单元测试。 |
| DOM-05 | 领域契约 | 同样输入与事实产生同样资格结果；输入顺序变化不改变结果或 hash。 | 确定性测试。 |
| DOM-06 | 领域契约 | 候选资格评估无 SQL、网络或策略调用。 | mock/静态检查。 |
| DOM-07 | 领域契约 | 错误码至少稳定区分 scope unresolved、capability missing、calendar not preflighted、PIT violation、outside scope、selected ineligible、hash mismatch、provider contract violation。 | 错误枚举与测试。 |
| DOM-08 | 领域契约 | 错误详情 JSON 安全：UUID/date/time/Decimal 规范化，禁止 ORM、连接、客户端和凭证序列化。 | 序列化测试。 |
| SCOPE-01 | 动态范围 | 动态范围预检能识别 `market_scope`、query policy、rule/exception、qualification policy 和 Provider 能力。 | scope resolution 测试。 |
| SCOPE-02 | 动态范围 | dynamic/hybrid 在运行前解析有限、具名、canonical `calendar_id` 集合，不调用策略且不靠枚举全部候选发现日历。 | 测试与调用检查。 |
| SCOPE-03 | 动态范围 | 无法解析有限日历、超资源上限或能力 unknown 时以 `universe_scope_unresolved` 阻断。 | 失败测试。 |
| SCOPE-04 | 动态范围 | Provider 缺 PIT Universe/必要资格能力时以 `universe_capability_missing` 请求级阻断。 | 失败测试。 |
| SCOPE-05 | 动态范围 | 日历集合合并 fixed、mandatory、所有非零 initial positions 和 dynamic scope 的 PIT 日历。 | 集合测试。 |
| SCOPE-06 | 动态范围 | 所有参与日历在正式区间逐自然日严格比较开市日、完整时段和时区，兼容后仅生成一个不可变 `TimeAxis`。 | strict compatibility 集成测试。 |
| SCOPE-07 | 动态范围 | 不兼容、缺失、歧义日历在策略前 blocked 并保留差异证据。 | 失败证据测试。 |
| SCOPE-08 | 动态范围 | 会话内复检 scope snapshot；集合/hash 变化失败且不更新、不扩展、不重建 TimeAxis。 | 会话边界测试。 |
| QUERY-01 | PIT 查询 | `UniverseQuery` 同时绑定当前 `effective_date`、`QueryBoundary.data_cutoff` 及冻结范围、规则、例外、资格政策和日历集合。 | 契约与调用测试。 |
| QUERY-02 | PIT 查询 | 身份、代码、名称、展示名、规则和日历均按 effective date 与 known-at cutoff 解析；无当前目录、今日代码或当前上市状态回退。 | 历史 PIT 测试。 |
| QUERY-03 | PIT 查询 | 动态资格按固定顺序组合：身份、映射/展示、市场范围、日历权限、规则、raw 行情/覆盖、公司行动/数量覆盖、适用交易状态、结果规范化。 | 评估器测试。 |
| QUERY-04 | PIT 查询 | 复用任务包 12/13/16A/18/19 已有资格端口，不复制 Bar、规则、覆盖、公司行动或交易状态逻辑。 | 依赖与 diff 检查。 |
| QUERY-05 | PIT 查询 | 单候选任何必需身份、映射、规则、行情、公司行动或状态资格缺失时只过滤该候选并累计稳定 reason code/count。 | 参数化测试。 |
| QUERY-06 | PIT 查询 | 空动态候选集在能力、范围和资格证明有效时合法返回，不自动 blocked。 | 空集测试。 |
| QUERY-07 | PIT 查询 | 候选结果以稳定 `instrument_id` 去重并排序，代码变化不产生重复身份。 | 排序/去重测试。 |
| QUERY-08 | PIT 查询 | 底层保留 `InstrumentSpec` 或等价完整规格，不创建第二套规格；策略适配层仅投影既有不可变 `InstrumentCandidateDTO`。 | 类型与 DTO 测试。 |
| QUERY-09 | PIT 查询 | 策略 DTO 只暴露 instrument_id、trading_code、name、display_name、asset_class、exchange，不暴露 source code、内部事实 ID/规则 key/原始表。 | DTO schema 测试。 |
| QUERY-10 | PIT 查询 | Universe Provider 查询不访问外部网络；正式读取中 Tushare 等网络调用被测试禁止。 | network mock 测试。 |
| QUERY-11 | PIT 查询 | 固定模式不扫描动态市场，只返回已完成固定预检且当前时点仍有效的固定候选。 | fixed 查询测试。 |
| QUERY-12 | PIT 查询 | hybrid 合并固定与动态候选时去重、稳定排序；mandatory 只有属于候选时才返回，但始终保留强制对象语义。 | hybrid 测试。 |
| PRE-01 | 预检编排 | 强制固定集合严格等于 static ∪ mandatory ∪ non-zero initial positions。 | 集合测试。 |
| PRE-02 | 预检编排 | 任一固定对象缺身份、映射、规则、行情、日历或 formal 必需事实时整体 blocked。 | 参数化失败测试。 |
| PRE-03 | 预检编排 | dynamic 仅对范围能力、资格能力与有限日历做请求级预检，单候选缺失只过滤。 | dynamic 测试。 |
| PRE-04 | 预检编排 | hybrid 同时执行固定完整预检和动态范围能力预检；任一固定部分失败整体 blocked。 | hybrid 测试。 |
| PRE-05 | 预检编排 | dynamic-only 不伪造 fixed rule snapshot；mandatory/非零初始持仓仍有完整 fixed snapshot/preflight。 | snapshot 测试。 |
| PRE-06 | 预检编排 | blocked 报告不得进入运行创建或调用策略；degraded hash 不一致同样在策略前失败。 | admission/runtime 测试。 |
| PRE-07 | 预检编排 | 报告保存 scope mode、market scope、query/qualification policy、calendar IDs、capability status、过滤原因计数及 scope snapshot hash。 | report schema 测试。 |
| VIEW-01 | 步骤视图 | 每个 decide 步骤按当前 effective_date/data_cutoff 新建 bound query，禁止 Runner 初始化时一次查询并缓存整段运行。 | 多步骤 Runtime 测试。 |
| VIEW-02 | 步骤视图 | 同一步骤重复查询为只读且结果/hash 一致；下一步骤允许按新 PIT 时点重算。 | 缓存/跨日测试。 |
| VIEW-03 | 步骤视图 | 策略过滤只能缩小范围，不能覆盖 cutoff、effective-date 语义、market scope、policy、规则、例外、资格政策或 calendar IDs。 | 越权参数测试。 |
| VIEW-04 | Session 授权 | Session/Chunk 分离 fixed authorized IDs 与当前步骤 candidate authorized IDs。 | 授权状态测试。 |
| VIEW-05 | Session 授权 | 动态 ID 必须先经当前步骤 universe.query() 才可访问策略数据，其他 Bar/规则入口不可绕过。 | 越权读取测试。 |
| VIEW-06 | Session 授权 | 动态授权不得扩大 fixed scope 或冻结日历；未预检日历以稳定错误失败。 | 授权/日历测试。 |
| VIEW-07 | Session 授权 | 持仓独立于候选集；不在动态候选集不得导致持仓被自动删除。 | 持仓行为测试。 |
| FINAL-01 | 最终复检 | 策略 decision 解析目标后、加载订单事实和创建任何订单前，对每个目标执行统一最终资格复检。 | 调用顺序测试。 |
| FINAL-02 | 最终复检 | 动态目标按当前决策时点重新验证身份、映射、权限、日历、规则/例外/结算、状态、行情/覆盖、公司行动/数量覆盖。 | 失效目标测试。 |
| FINAL-03 | 最终复检 | 固定与动态目标复用同一资格评估与结构化错误格式，不存在第二套过滤规则。 | 调用与结果测试。 |
| FINAL-04 | 最终复检 | 不在 fixed/current dynamic 权限内的目标以 `universe_target_outside_scope` 失败。 | 越权目标测试。 |
| FINAL-05 | 最终复检 | 未预检日历以 `universe_calendar_not_preflighted` 失败，不添加日历或改变 TimeAxis。 | 日历失败测试。 |
| FINAL-06 | 最终复检 | 任一目标失败则整次回测终止，不静默删除、不继续、不产生该 decision 的任何部分订单或成交。 | 原子失败测试。 |
| FINAL-07 | 最终复检 | 失败证据至少含 instrument_id、session_date、decision_time、data_cutoff、calendar_id、failed_check、reason_codes、expected、actual、evidence_summary。 | 错误详情测试。 |
| AUDIT-01 | 报告审计 | 优先复用现有 preflight、decision validation issues、结果详情 JSON；不新增候选专用表。 | 模型与迁移 diff。 |
| AUDIT-02 | 报告审计 | 保存动态范围 hash、资格政策版本、解析日历、实际候选数、过滤原因计数、target IDs 和最终复检结果。 | 持久化/投影测试。 |
| AUDIT-03 | 报告审计 | 相同资格事实产生相同 hash；hash 至少覆盖任务包列出的策略/规则/日历/session/capability/cutoff 语义。 | hash 测试。 |
| AUDIT-04 | 报告审计 | hash 不覆盖 run_id、generated_at、自增 ID、中文文案和运行时候选列表排序。 | 变体测试。 |
| AUDIT-05 | 报告审计 | 机器判断只依赖稳定 code；操作人员可见摘要为简洁中文，且不以第三方英文或内部 key 作为主文本。 | schema/展示字段检查。 |
| AUDIT-06 | 报告审计 | 选中后失败可定位到标的、日期、截止点和具体检查，且不序列化敏感凭证或 ORM。 | 结果测试。 |
| AUDIT-07 | 报告审计 | 若现有结果字段足够则无 DB 迁移；若确需最小 JSON 字段，则 SQLite/PostgreSQL 迁移与回滚测试齐备。 | diff 与迁移测试。 |
| RED-01 | 红线 | 不新增交易日历算法、组合日历、隐式并/交集或自动拆分回测。 | 范围关键词与 diff 检查。 |
| RED-02 | 红线 | 不新增原始 Bar 采集/存储/清洗/OHLC 规则，且不以缩短历史窗口掩盖缺口。 | 范围与行为测试。 |
| RED-03 | 红线 | 不新增 InstrumentSpec 字段、规则事实、规则包语义或具名例外机制。 | 类型/diff 检查。 |
| RED-04 | 红线 | 不新增 qfq/hfq 公式、复权因子验算或复权序列。 | 范围检查。 |
| RED-05 | 红线 | 不新增公司行动采集、日期派生、现金入账、拆并会计处理；空事件表不作否定证明。 | 范围与资格测试。 |
| RED-06 | 红线 | 不新增交易状态采集算法，不强制读取规则声明 not_applicable 的状态事实。 | 范围与测试。 |
| RED-07 | 红线 | 不新增策略排名、因子、选股、目标权重算法。 | 范围检查。 |
| RED-08 | 红线 | 不新增订单类型、撮合、账户、持仓、结算或费用语义。 | 范围检查。 |
| RED-09 | 红线 | 不签发 consistency token，不实现块协议或源数据修订链。 | 范围检查。 |
| RED-10 | 红线 | 不新增策略工作台、候选管理页面、独立 Universe API 或候选 CRUD。 | 路由/前端/diff 检查。 |
| RED-11 | 红线 | 不以 SSE、exchange、代码前缀或资产类型默认/推导日历；不在运行中追加日历。 | 代码与失败测试。 |
| RED-12 | 红线 | 不把单候选缺事实升级为整个 dynamic 宇宙 blocked，也不把 Provider 能力失败降级为候选过滤。 | 双向错误语义测试。 |
| RED-13 | 红线 | 不通过跳过不合格目标、减少窗口、切换当前代码、增加临时日历或 degraded 默认值继续 formal 运行。 | 失败路径检查。 |
| RED-14 | 红线 | 不新增独立运行事件体系；只作现有报告/结果模型最小接线。 | 模型/diff 检查。 |

## 场景验收矩阵

| 编号 | 场景 | 冻结预期 |
| --- | --- | --- |
| A01 | fixed 模式固定对象全部完整 | 预检 ready，固定候选可查询。 |
| A02 | fixed 一个固定标的缺 PIT 身份 | 整体 blocked。 |
| A03 | fixed 一个固定标的缺 raw Bar | 整体 blocked。 |
| A04 | dynamic 范围可解析有限日历 | 预检通过。 |
| A05 | dynamic 范围无法解析有限 calendar_id | `universe_scope_unresolved`。 |
| A06 | Provider 无 Universe/资格能力 | `universe_capability_missing`。 |
| A07 | dynamic 一个候选规则不完整 | 过滤该候选，保留其他合格候选。 |
| A08 | dynamic 一个候选行情覆盖不完整 | 过滤该候选，不阻断宇宙。 |
| A09 | dynamic 一个候选公司行动覆盖不完整 | 过滤该候选，空表不作否定证明。 |
| A10 | dynamic 候选未来才存在 | 历史查询不返回。 |
| A11 | dynamic 候选带未预检日历 | 不返回并记录 `universe_calendar_not_preflighted`。 |
| A12 | hybrid 固定和动态均合格 | 合并、去重、稳定排序。 |
| A13 | hybrid 固定部分失败 | 整体 blocked。 |
| A14 | dynamic 存在非零初始持仓 | 逐一完整预检，不被过滤跳过。 |
| A15 | 初始持仓缺身份/规则/估值/必要事实 | 创建运行前 blocked。 |
| A16 | 相同决策时点重复查询 | 候选与 hash 相同。 |
| A17 | 不同历史决策日期 | 仅返回对应 PIT 候选，不读取未来身份。 |
| A18 | 策略尝试改 data_cutoff 或市场范围 | 拒绝请求，冻结边界不变。 |
| A19 | 策略选中查询后已失效动态标的 | 订单前复检失败，整次终止。 |
| A20 | 策略提交权限外标的 | `universe_target_outside_scope`，无部分订单。 |
| A21 | 策略提交未预检日历标的 | `universe_calendar_not_preflighted`，不扩展 TimeAxis。 |
| A22 | 运行中候选引入新日历 | 运行失败，不重建时间轴。 |
| A23 | fixed/dynamic/hybrid 输入顺序变化 | 结果与 hash 不变。 |
| A24 | DTO 暴露 source code 作为主键 | Provider/DTO 测试失败。 |
| A25 | Provider 尝试访问网络 | 测试失败，正式读取被禁止。 |
| A26 | dynamic 候选全部过滤 | 能力与范围有效时合法返回空集。 |
| A27 | dynamic-only 无 fixed rule snapshot | 允许动态资格快照；mandatory 仍完整预检。 |
| A28 | 会话内预检 hash 变化 | 策略加载前失败。 |
| A29 | 页面准入与会话均 degraded 但 hash 不一致 | 运行失败，不调用策略。 |
| A30 | 过滤摘要包含候选级原因 | 可定位原因与数量，不暴露内部原始表。 |

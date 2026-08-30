# 任务包 16A：覆盖度与质量基础契约及内部链路验收——审查状态

> 唯一基线：`任务包-16A-覆盖度与质量基础契约_REVIEW_CHECKLIST.md`
>
> 状态枚举：待检查 / 通过 / 失败 / 阻塞。收到全部子代理“开发完成”前禁止修改本文件。

| 编号 | 状态 | 审查轮次 | 证据或失败原因 |
| --- | --- | --- | --- |
| B01 | 通过 | 第一轮 | 复用既有 DataCoverageReport、DataPreflightReport、DataCapabilityManifest 及公共协议；未发现第二套同义模型。 |
| B02 | 通过 | 第一轮 | formal@1 在 Phase 2a 预读阶段固定 blocked，且 profile 明确拒绝 fixture_only；未交付能力不以默认值放行。 |
| B03 | 通过 | 第一轮 | 保留任务开始前 errors.py/memory.py/requests.py/views.py 等既有修改；无 branch switch、reset 或 commit，追溯表记录增量交付。 |
| F01 | 通过 | 第一轮 | frozen/slots DataCoverageFact 含全部最小字段；details 深度冻结、issue_codes 稳定排序、规则版本精确。 |
| F02 | 通过 | 第一轮 | 构造器拒绝非 UUID、datetime、空字段、latest、无证据 complete 及非法适用性组合；负向测试覆盖。 |
| F03 | 通过 | 第一轮 | not_applicable 强制精确 validation_rule 且质量必须 complete；缺少事实仍由聚合器计 unavailable。 |
| F04 | 通过 | 第一轮 | complete 要求 COMPLETE evidence；unavailable 不得携带其他质量证据，缺失事实由聚合器表达无法证明。 |
| F05 | 通过 | 第一轮 | invalid 要求非空审计 details；JSON 深冻结拒绝 ORM/非 JSON 值并递归拦截 token/secret/credential 等敏感键。 |
| F06 | 通过 | 第一轮 | logical_key 仅含 UUID、date、DataCapability、field 与可空 rule key/version；hash 不使用 DB ID/时间/对象身份。 |
| C01 | 通过 | 第一轮 | evaluate_coverage 仅消费已解析输入并返回既有 DataCoverageReport；模块无 Provider/ORM/网络/文件 I/O。 |
| C02 | 通过 | 第一轮 | 缺失键/无法证明计 unavailable，partial/invalid 按来源质量计数，complete 仅来自已校验事实；无 0/前值填充。 |
| C03 | 通过 | 第一轮 | 同内容按 canonical materialization 去重；同键冲突与范围外事实均产生稳定 Provider contract violation 并令报告 invalid。 |
| C04 | 通过 | 第一轮 | _missing_ranges 遍历规范化 resolved sessions 合并缺失段，不插入自然日，休市间隔不被猜测。 |
| C05 | 通过 | 第一轮 | instrument/session/field/fact/issue/revision 均 canonical 排序；A01 测试验证顺序与相同重复不改变报告/hash。 |
| C06 | 通过 | 第一轮 | preflight 仅对 qfq/hfq 强制 active adjustment policy；raw 路径无复权依赖，聚合测试覆盖 raw。 |
| Q01 | 通过 | 第一轮 | CoverageQualificationRequest 绑定 instrument/effective date/window/formal-warmup-history/query boundary/profile/capabilities/calendar IDs。 |
| Q02 | 通过 | 第一轮 | InstrumentCoverageQualification 字段完整、证据深冻结、报告稳定排序并自动计算/校验 qualification_hash。 |
| Q03 | 通过 | 第一轮 | 请求仅接受 UUID；CoverageQualificationPort/Memory 实现只评估指定标的，不访问策略、不生成候选、不改 scope。 |
| Q04 | 通过 | 增量复审1 | 联合测试通过：候选级质量失败返回过滤结果，Provider 范围/资格能力缺失保持请求级 blocked。 |
| Q05 | 通过 | 第一轮 | machine_content 排除 run_id/generated_at/message/derived hash；测试验证运行元数据和中文文案变化不改 qualification_hash。 |
| Q06 | 通过 | 第一轮 | CoverageQualificationPort 位于公共 protocols.py，未导入 DataPreflightService；任务包 15 通过结构协议/DTO 接线。 |
| P01 | 通过 | 第一轮 | registry 仅接受 exact formal@1/internal_link_acceptance@1；内部 run kind 固定且请求不匹配即拒绝。 |
| P02 | 通过 | 第一轮 | internal profile 构造即禁止 allow_degraded；运行报告若 degraded 增加稳定错误并转 blocked。 |
| P03 | 通过 | 第一轮 | InternalFixtureCapability 精确限定 quantity_action_coverage、trading_status、source_revision_audit、transitional_repeatable_read 四类。 |
| P04 | 通过 | 第一轮 | InternalFixture 强制 exact key/version/capability、instrument/scope、date range、proof_summary、source、fixture_only、SHA-256 content_hash。 |
| P05 | 通过 | 增量复审1 | fixture.covers 校验完整 envelope；fixture_sources 冻结到 DataPreflightReport 并纳入 report/qualification hash。 |
| P06 | 通过 | 第一轮 | fixture 必须显式 InternalFixture + registry exact ref；空/缺失能力保持 blocked，Memory/ETF 不默认制造替代事实。 |
| P07 | 通过 | 第一轮 | formal@1 allow_fixture_only=false，request/profile/provider 三层均拒绝 fixture_only，定向测试覆盖。 |
| P08 | 通过 | 第一轮 | _fixture_substitution_removals 仅匹配 actions/status 结构化能力；其余硬门禁保留，初始持仓也仅移除这两类缺失事实。 |
| P09 | 通过 | 第一轮 | MAX_LOOKBACK_SESSIONS=512 在请求、资格协议、服务预读和 session lookback 均校验；超限抛错/blocked，不裁剪。 |
| P10 | 通过 | 第一轮 | CALENDAR_AXIS_POLICY 固定 strict_compatible@1；calendar IDs 来自请求/任务包 15 解析，无 SSE 默认、并集或拆分实现。 |
| P11 | 通过 | 第一轮 | preflight/memory/ETF adapter 未导入或调用网络客户端；读取只消费注入 Provider/fixture，A21 将以网络阻断测试再验证。 |
| S01 | 通过 | 第一轮 | DataPreflightService 仅编排 profile/fixed/scope/calendar/provider/qualification/report；无 ORM/FastAPI/网络/策略算法并复用既有初始持仓服务。 |
| S02 | 通过 | 第一轮 | admission.allowed 仅在 ready；blocked 决策不可 open_session，服务自身无运行创建副作用。 |
| S03 | 通过 | 第一轮 | AuthoritativeDataSession 在 preflight 内用冻结请求调用 validate_session；before_strategy 仅在权威决策通过后调用 loader。 |
| S04 | 通过 | 第一轮 | 页面 blocked/hash 变化/session blocked 均 failure_phase=data_preflight，保留 admission/session hash 和 diff；blocked 不调用 loader。 |
| S05 | 通过 | 第一轮 | _fixed_ids 确定性合并 static、mandatory、request/spec non-zero positions；_qualification_issues 对并集中每个标的调用统一端口。 |
| S06 | 通过 | 第一轮 | non-zero positions 缺 spec/gateway 在 Provider 读前 blocked；估值/账户/费用/结算由 InitialPositionPreflightService 的既有门禁传播。 |
| S07 | 通过 | 第一轮 | 直接调用现有 InitialPositionPreflightService/BacktestPreflightGateway，未建立第二套账户或费用规则。 |
| S08 | 通过 | 增量复审1 | task15 provider 联合测试通过：缺失 Bar 候选被过滤，缺失 Universe/资格能力请求级 blocked。 |
| S09 | 通过 | 增量复审1 | 新增 hybrid 固定失败测试通过，动态 scope ready 不能绕过 fixed_ids 硬门禁。 |
| S10 | 通过 | 增量复审1 | 联合测试确认候选生成/过滤/最终复检仍由 task15 universe/runtime 负责，16A 仅消费摘要。 |
| R01 | 通过 | 增量复审1 | DataPreflightReport 本体已补齐 16A run/profile/resolved/coverage/fixture 等字段，完成冻结、序列化与 hash 绑定；116 项关联回归通过。 |
| R02 | 通过 | 增量复审1 | 报告本体与 outcome 双层 hash 均覆盖 run/profile/scope/fixture/coverage 业务证据，fixture 变化测试通过。 |
| R03 | 通过 | 增量复审1 | 新增报告字段均为业务证据；generated_at/run_id/message/凭证/token 上下文仍排除，既有稳定性回归通过。 |
| R04 | 通过 | 增量复审1 | 新字段通过既有 BacktestDataPreflightRecord/backtest_data_preflight JSON 投影持久化，无新表或第二真相源。 |
| R05 | 通过 | 增量复审1 | 持久化敏感键拒绝与有界分页回归通过，I01 修复未放宽 JSON/分页限制。 |
| I01 | 通过 | 增量复审1 | read_page 已移除 query_context 自动提权，仅显式 include_internal 可开启；伪造 visibility/run_kind/include_internal 测试通过。 |
| I02 | 通过 | 增量复审1 | 59 项结果仓储/API 测试覆盖 data_preflight/equity/metrics/analysis-summary 正式拒绝矩阵；仓库无 comparison 路由。 |
| I03 | 通过 | 第一轮 | PreflightOutcome/BacktestDataPreflightItem 对内部记录显示“内部链路验收”、profile 与 fixture_sources，正式文案不用于内部结果。 |
| E01 | 通过 | 第一轮 | errors.py 注册文档第 12 节实际列出的 10 个 16A code，异常层级兼容既有 Provider contract 分支。 |
| E02 | 通过 | 第一轮 | 覆盖/fixture/profile/hash 错误 details 经 freeze/canonical JSON 校验，并按场景携带 instrument/date/capability/field/rule/expected/actual/scope。 |
| E03 | 通过 | 增量复审1 | _scope_issue 固定中文摘要，原始上游 message 写入 details.upstream_message；中英文文案测试通过。 |
| A01 | 通过 | 第一轮 | test_input_order_and_duplicate_equal_facts_do_not_change_hash 通过，报告与 canonical hash 对输入顺序稳定。 |
| A02 | 通过 | 第一轮 | 冲突事实测试通过：invalid_count=1，并含 coverage_fact_conflict 与 coverage_provider_contract_violation。 |
| A03 | 通过 | 第一轮 | 聚合缺失计 unavailable；Memory Provider mandatory coverage gap 测试通过并在预检阶段 blocked。 |
| A04 | 通过 | 第一轮 | ETF invalid OHLC 原值保留、不修复，资格结果 invalid/ineligible 且覆盖读取 blocked，相关测试通过。 |
| A05 | 通过 | 第一轮 | initial-position 规则包缺失/结算规则缺失与冲突门禁测试通过，均保持 blocked。 |
| A06 | 通过 | 第一轮 | data-session 日历不兼容测试通过：blocked、保留差异、无 master/SSE 降级。 |
| A07 | 通过 | 第一轮 | initial-position 原始估值缺失/非法/越界及动态范围跳过场景测试通过，均在运行前阻断。 |
| A08 | 通过 | 增量复审1 | 新增 named_quantity_fixture 端到端测试通过：范围完整时 ready，fixture key/version/source 写入冻结报告。 |
| A09 | 通过 | 第一轮 | initial-position test_missing_corporate_action_facts_block 证明空/缺失公司行动事实且无覆盖 fixture 时 blocked。 |
| A10 | 通过 | 增量复审1 | 新增 unnamed_status_mapping 测试通过：预检读 Provider 前返回 internal_preflight_fixture_missing/blocked。 |
| A11 | 通过 | 第一轮 | test_formal_profile_rejects_fixture_only_fact 通过，formal@1 读取 fixture_only 被拒绝。 |
| A12 | 通过 | 第一轮 | test_degraded_internal_report_is_converted_to_blocked 通过，增加 internal_preflight_degraded_forbidden 并返回 blocked。 |
| A13 | 通过 | 增量复审1 | dynamic missing bar 测试通过：候选被任务包 15 过滤，范围报告保持 ready。 |
| A14 | 通过 | 第一轮 | test_missing_universe_capability_is_request_level_block 通过，Provider 缺资格/Universe 能力为请求级 blocked。 |
| A15 | 通过 | 增量复审1 | 新增 hybrid_fixed_object_failure 测试通过：固定对象失败令整体 blocked，动态范围不绕过固定门禁。 |
| A16 | 通过 | 第一轮 | test_session_hash_change_blocks_before_strategy_loader 通过：会话变化在 loader 前阻断并保存 admission/session hash。 |
| A17 | 通过 | 第一轮 | ETF inactive adjusted-series 测试证明 qfq/hfq 未激活阻断；raw 聚合测试证明 raw 不受影响。 |
| A18 | 通过 | 第一轮 | qualification hash 元数据测试及 DataPreflightReport 既有测试均证明生成时间/中文文案变化不改 hash。 |
| A19 | 通过 | 增量复审1 | fixture content hash 变化同时改变 DataPreflightReport.report_hash 与 PreflightOutcome qualification hash，测试通过。 |
| A20 | 通过 | 增量复审1 | 结果仓储/API 59 项矩阵测试通过；正式列表、equity、metrics、analysis-summary 均拒绝内部 ID，query_context 不可提权，且无比较 API。 |
| A21 | 通过 | 第一轮 | ETF adapter 无外部源导入测试通过；16A preflight/memory 模块静态检查无网络客户端。 |
| A22 | 通过 | 第一轮 | data-session test_blocked_preflight_never_calls_strategy_hook 通过；服务 before_strategy 也在 blocked 时直接返回 None。 |
| X01 | 通过 | 第一轮 | diff/关键词审查未发现 token 签发、失效、覆盖向量或块前校验算法新增。 |
| X02 | 通过 | 第一轮 | 未新增公司行动采集、cash effective date 派生、账户入账或数量类生产数据源。 |
| X03 | 通过 | 第一轮 | 未新增交易状态生产、源修订链/向量或历史回放；仅存在具名 fixture 契约。 |
| X04 | 通过 | 第一轮 | 16A 变更未实现新日历/Bar/规则/复权/账户算法或分钟/Tick；并行任务包 15 文件不归 16A 追溯分区。 |
| X05 | 通过 | 第一轮 | 未新增正式创建入口、队列、Runner Supervisor、工作台或第二套运行模型。 |
| X06 | 通过 | 第一轮 | 未新增 data_coverage_facts 或公司行动/状态/修订/token 表；复用 backtest_data_preflight JSON 元数据。 |
| X07 | 通过 | 增量复审1 | 全部 316 项 16A/任务包 15/数据契约/结果 API 关联测试通过；compileall、导入冒烟、git diff --check 均通过。 |

# 任务包 14：前复权与后复权追溯表
 
各代理仅维护自己负责的章节，不删除或覆盖其他章节。每项记录检查表编号、代码位置、测试用例、测试输出和实现说明。

## T14-01：基线核查与契约冻结

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-01-01 | `backend/app/backtesting/data/requests.py:PriceBasis/AdjustedSeriesQuery`；`backend/app/backtesting/data/facts.py:AdjustedSeriesPoint`；`backend/app/backtesting/data/protocols.py:DataChunkSession.adjusted_series`；`backend/app/data_ingestion/models/etf_adjustment.py:EtfAdjustmentFactor`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter`；`backend/app/backtesting/data/pit_history.py:resolve_pit_mappings`；`backend/app/backtesting/data/reports.py:DataPreflightReport`；`backend/app/backtesting/data/views.py:ChunkStrategyDataView` | `Task14BaselineContractTestCase.test_price_basis_and_adjusted_query_facts_are_existing_contracts`；`test_adjusted_series_protocol_is_exposed_by_the_chunk_session`；`test_etf_storage_and_adapter_surfaces_are_available`；`test_preflight_and_strategy_view_keep_explicit_basis_surfaces` | `Ran 6 tests in 0.008s ... OK` | 已核查并复用现有口径、调整查询/因子事实、Provider 分层、ETF 因子表、适配器、原始 Bar、稳定身份/PIT、预检和策略/engine 视图。检查表中的 `DataProvider.adjusted_series()` 在现有分层中对应 `DataChunkSession.adjusted_series(query)`，不新增平行入口。 |
| 14-01-02 | `docs/architecture/backtesting-engine-features/任务包-14-前复权与后复权_14-01_BASELINE.md`；本追溯表 T14-01 | `Task14BaselineContractTestCase` 全部测试 | `Ran 6 tests in 0.008s ... OK` | 核查记录第 4 节列出本步骤实际修改文件；第 3 节列出真实源验算、policy 证据、effective date、研究价格和详细 hash 等后续缺口及门禁。 |
| 14-01-03 | `backend/app/backtesting/data/adapters/etf.py:project_bar/resolve/bars`；`backend/app/backtesting/data/facts.py:Bar` | `Task14BaselineContractTestCase.test_raw_bar_read_does_not_touch_adjustment_factors` | `Ran 6 tests in 0.008s ... OK` | 使用因子端口故意失败的假端口读取 raw Bar，读取成功且 `price_basis=raw`，证明基线 raw 路径不依赖复权因子；未指定口径的请求默认 raw 的现有行为保持不变。 |
| 14-01-04 | 本步骤变更集（无生产业务代码）；`backend/app/backtesting/data/adapters/etf.py` 无网络导入 | `Task14BaselineContractTestCase.test_etf_adapter_has_no_network_import` | `Ran 6 tests in 0.008s ... OK` | 本步骤仅新增基线测试和核查记录；没有新增公司行动、账户、日历、交易规则、PIT 映射、分钟线/Tick、候选集或通用多供应商/多资产复权实现。 |

## T14-03：policy 描述与激活门禁

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-03-01 | `backend/app/backtesting/data/adjustment_policy.py:AdjustmentPolicyStatus/AdjustmentSeriesPolicy`; `registered_adjustment_policies()` | `Task14PolicyTestCase.test_only_one_registered_inactive_policy_exists`；`test_unknown_key_or_version_is_rejected` | `Ran 8 tests in 0.001s ... OK` | 仅注册 `tushare_adj_factor_native@1`，状态枚举只有 `inactive/active`，注册默认对象为 inactive；未知 key/version 在构造和解析时失败。 |
| 14-03-02 | `backend/app/backtesting/data/adjustment_policy.py:AdjustmentSeriesPolicy` | `Task14PolicyTestCase.test_active_requires_published_complete_artifact` | `Ran 8 tests in 0.001s ... OK` | 不可变 policy 保存适配器、源/字段映射、effective_date、cutoff、qfq/hfq 公式/锚点、精度、舍入、验算摘要和三类 hash；`as_dict()` 输出只读审计描述。 |
| 14-03-03 | `backend/app/backtesting/data/adjustment_policy.py:AdjustmentSeriesPolicy.active/from_verification_artifact/validate_activation` | `Task14PolicyTestCase.test_active_requires_published_complete_artifact`；`test_adapter_accepts_only_an_evidence_backed_policy` | `Ran 8 tests in 0.001s ... OK` | active 构造强制检查完整公式语义、证据摘要、verified/passed 状态、published 标志、适配器版本和 SHA-256 input/output/evidence hash；缺失或未发布制品失败。 |
| 14-03-04 | `backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.__post_init__` | `Task14PolicyTestCase.test_adapter_legacy_boolean_cannot_bypass_policy_evidence` | `Ran 8 tests in 0.001s ... OK` | `adjustment_active=True` 不再直接激活；适配器要求传入证据绑定的 immutable policy，兼容字段只能反映 policy 状态。 |
| 14-03-05 | `backend/app/backtesting/data/adjustment_policy.py:AdjustmentSeriesPolicy`；`backend/app/strategy_protocol/data_view.py:AdjustmentPolicyGate` | `Task14PolicyTestCase.test_policy_and_serialized_description_are_read_only`；`test_strategy_gate_can_only_be_opened_from_policy` | `Ran 8 tests in 0.001s ... OK` | policy、序列化描述和策略 gate 均拒绝属性写入；生产 gate 可由 policy 构造并保留只读 policy 引用。 |
| 14-03-06 | `backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.adjusted_series`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.__post_init__` | `tests.test_etf_data_adapter.AdjustmentPolicyTestCase.test_inactive_policy_blocks_adjusted_series`；`Task14PolicyTestCase.test_adapter_accepts_only_an_evidence_backed_policy` | `Ran 2 tests in 0.001s ... OK`（门禁路径） | qfq/hfq 读取只接受已验证 active policy，key/version/证据不匹配在构造或读取时失败；raw 仍走独立 bars 路径。 |

## T14-02：真实源复权语义验算

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-02-01 | `backend/app/backtesting/data/verification_artifacts/tushare_adj_factor_native@1.json`；`backend/app/backtesting/data/adjustment_verification.py:verify_artifact_file` | `RealSourceArtifactTestCase.test_checked_in_artifact_is_real_source_and_fails_closed_without_native_rows` | `Ran 5 tests in 0.007s ... OK` | 版本化制品记录 Tushare `fund_adj` 原始 `513100.SH` 因子行、来源批次、源端点和 qfq/hfq 输出槽位；当前公开 ETF 源未提供可对应的双原生输出，制品明确标记 failed，保持 policy inactive，不以 fixture 或猜测值冒充源输出。 |
| 14-02-02 | `.../tushare_adj_factor_native@1.json:input.factor_rows/cutoff_cases`；`adjustment_verification.py:_validate_cutoffs` | `RealSourceArtifactTestCase.test_checked_in_artifact_is_real_source_and_fails_closed_without_native_rows` | `Ran 5 tests in 0.007s ... OK` | 同一真实标的覆盖 2019-09-24、25、26 三个有效日期，并验证 2019-09-24（边界前）与 2019-09-26（边界后）cutoff 的可见日期集合。 |
| 14-02-03 | `adjustment_verification.py:_validate_mapping/_validate_semantics`；制品 `mapping/semantics` | `RealSourceArtifactTestCase.test_native_outputs_are_compared_for_both_bases`；`test_precision_mismatch_fails_even_when_hashes_are_recomputed` | `Ran 5 tests in 0.007s ... OK` | 固定记录 `ts_code/trade_date/adj_factor` 到规范化字段、日期归一化、qfq/hfq 各自公式/锚点、价格与因子精度和舍入声明；未知语义直接失败。 |
| 14-02-04 | `adjustment_verification.py:_compare_native_outputs/verify_artifact` | `RealSourceArtifactTestCase.test_native_outputs_are_compared_for_both_bases`；`test_precision_mismatch_fails_even_when_hashes_are_recomputed`；`test_checked_in_artifact_is_real_source_and_fails_closed_without_native_rows` | `Ran 5 tests in 0.007s ... OK` | 对 qfq/hfq 每个日期及 OHLC 字段逐点按源声明精度比较；缺失源原生输出、日期无法对应或精度不一致均返回 failed，禁止激活。 |
| 14-02-05 | `adjustment_verification.py:artifact_hashes/build_artifact`；制品 `verification.hashes` | `RealSourceArtifactTestCase.test_checked_in_artifact_is_real_source_and_fails_closed_without_native_rows`；`test_stored_hash_tampering_fails` | `Ran 5 tests in 0.007s ... OK` | 输入、输出和证据摘要使用统一 canonical SHA-256；policy、适配器、映射、语义和来源批次参与 hash，捕获/生成时间键被排除，制品不保存凭证。 |
| 14-02-06 | `adjustment_verification.py:assert_verified/verify_artifact`；`.../tushare_adj_factor_native@1.json:verification.status=failed` | `RealSourceArtifactTestCase.test_checked_in_artifact_is_real_source_and_fails_closed_without_native_rows` | `Ran 5 tests in 0.007s ... OK` | 验算器只比较调用方提供的源原生输出与适配器输出，不实现经验公式、公司行动或 raw Bar 推导；当前缺双输出时显式失败关闭，policy 保持 inactive。 |

## T14-04：因子规范化与截止读取

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-04-01 | `backend/app/backtesting/data/etf_adjustment.py:NormalizedAdjustmentFactor/normalize_adjustment_factor(s)`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.project_factor/adjusted_series`；现有 `etf_adjustment_factors` 存储模型 | `FactorNormalizationTestCase.test_source_date_is_normalized_and_retained` | `Ran 25 tests in 0.018s ... OK`（Task14 组合测试） | 因子继续来自当前权威表；源 `trade_date` 归一化为 `effective_date`，同时保留 `source_trade_date`、`source_code`、instrument 身份和观测证据，不新增历史修订表。 |
| 14-04-02 | `backend/app/backtesting/data/etf_adjustment.py:cutoff_local_date`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.resolve/adjusted_series`；`backend/app/backtesting/data/pit_history.py:PITMappingResolution.session_cutoff_date` | `FactorNormalizationTestCase.test_cutoff_uses_market_local_date`；`Task14BaselineContractTestCase.test_raw_bar_read_does_not_touch_adjustment_factors` | `Ran 25 tests in 0.018s ... OK` | cutoff 从请求的 timezone-aware instant 转为市场本地日期，严格执行 `effective_date <= cutoff_local_date`；不读取系统当前时间，并在 PIT 分段和因子读取中复用同一边界。 |
| 14-04-03 | `backend/app/backtesting/data/etf_adjustment.py:_factor_evidence/normalize_adjustment_factor`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.adjusted_series` | `FactorNormalizationTestCase.test_source_date_is_normalized_and_retained` | `Ran 25 tests in 0.018s ... OK` | `updated_at` 只进入 `observed_at` 观测证据，不参与历史可见性过滤；适配器不根据公司行动重算因子。 |
| 14-04-04 | `backend/app/backtesting/data/etf_adjustment.py:normalize_adjustment_factor(s)`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.require_row_code` | `FactorNormalizationTestCase.test_invalid_identity_factor_and_cutoff_rows_fail_closed`；`test_duplicate_or_incomplete_batch_does_not_shorten_sequence` | `Ran 4 tests in 0.001s ... OK`（因子规范化/边界测试） | 严格校验正数、有限数值、日期、源代码、来源、instrument 身份、cutoff、重复和完整覆盖；缺失、重复、越界或错误身份/代码直接失败关闭，不返回缩短序列。 |
| 14-04-05 | `backend/app/backtesting/data/pit_history.py:resolve_pit_mappings/read_segmented_adjusted_series`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.resolve/adjusted_series/pit_status` | `Task14BaselineContractTestCase.test_raw_bar_read_does_not_touch_adjustment_factors`；`tests.test_etf_data_adapter.ResultRecordIntegrationTestCase.test_payloads_round_trip_through_record_and_api_schema` | `Ran 25 tests in 0.018s ... OK`（Task14 组合测试） | 因子按现有稳定 instrument/PIT 映射分段读取；raw 读取完全不触发因子端口，因子 cutoff 声明不是 `non_strict_pit` 能力，不改变 raw Bar。 |

## T14-05：qfq/hfq 研究价格序列

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-05-01 | `backend/app/backtesting/data/etf_adjustment.py:build_research_price_series`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.research_price_series` | `ResearchPriceSeriesTestCase.test_qfq_and_hfq_are_separate_price_bases_and_do_not_mutate_raw` | `Ran 7 tests in 0.001s ... OK`（研究价格测试） | 研究层只接收完整 raw Bar 与 cutoff 内因子，由适配器按 active policy 的源原生公式、锚点、精度和舍入规则生成新的研究 Bar。 |
| 14-05-02 | `backend/app/backtesting/data/etf_adjustment.py:_formula_kind/_anchor_index/build_research_price_series`；`backend/app/backtesting/data/facts.py:Bar.price_basis` | `ResearchPriceSeriesTestCase.test_qfq_and_hfq_are_separate_price_bases_and_do_not_mutate_raw` | `Ran 7 tests in 0.001s ... OK` | qfq 使用 `tushare_qfq_native_v1` 与 `latest-visible-close`，hfq 使用 `tushare_hfq_native_v1` 与 `first-visible-close`；输出显式标记 qfq/hfq，因子点不会直接作为价格点。 |
| 14-05-03 | `backend/app/backtesting/data/etf_adjustment.py:build_research_price_series`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.research_price_series` | `ResearchPriceSeriesTestCase.test_missing_factor_and_cross_basis_factor_are_blocked`；`test_missing_native_semantics_do_not_fallback_to_another_basis` | `Ran 7 tests in 0.001s ... OK` | 因子缺失、跨 basis、公式/锚点/精度/舍入语义缺失均失败关闭；qfq 不回退 hfq/raw，hfq 不回退 qfq/raw。 |
| 14-05-04 | `backend/app/backtesting/data/views.py:ChunkStrategyDataView`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.research_price_series` | `Task14BaselineContractTestCase.test_preflight_and_strategy_view_keep_explicit_basis_surfaces`；`ResearchPriceSeriesTestCase.test_qfq_and_hfq_are_separate_price_bases_and_do_not_mutate_raw` | `Ran 25 tests in 0.018s ... OK` | 策略数据视图显式选择 `raw/qfq/hfq`，未指定仍为 raw；研究价格通过复制的 Bar 返回，不写回 raw 表或 raw Bar。 |
| 14-05-05 | `backend/app/backtesting/data/adjustment_policy.py:AdjustmentSeriesPolicy`；`backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.preflight_summary`；`backend/app/backtesting/data/views.py:ChunkEngineDataView` | `Task14PolicyTestCase.test_strategy_gate_can_only_be_opened_from_policy`；`Task14BaselineContractTestCase.test_preflight_and_strategy_view_keep_explicit_basis_surfaces` | `Ran 13 tests in 0.009s ... OK`（policy/基线组合） | 研究结果携带 policy key/version、公式/锚点和证据摘要供预检与审计；engine view 仅允许 raw，并对 provider 返回的 adjusted Bar 直接报告 contract violation，通用引擎不解释复权公式。 |

## T14-06：预检、hash 和运行记录

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-06-01 | `backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.preflight_summary` | `PreflightContractTestCase.test_adjusted_basis_requires_active_policy_and_complete_coverages`；`test_adjusted_basis_blocks_missing_research_coverage_and_cutoff` | `Ran 5 tests ... OK` | qfq/hfq 预检校验固定 policy key/version、active 状态、适配器版本、验算摘要、因子/研究价格覆盖和市场本地 cutoff；缺任一证据或越界边界即加入阻断 issue。 |
| 14-06-02 | `backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.preflight_summary/build_data_preflight_payloads` | `PreflightContractTestCase.test_raw_is_allowed_while_inactive_and_payload_contains_contract`；`test_adjustment_contract_and_coverage_are_hash_relevant_but_credentials_are_not` | `Ran 5 tests ... OK` | 预检摘要和现有 `capabilities` 记录包含 adjustment policy、状态、适配器/公式/锚点、cutoff 规则、三类验算 hash 及因子/研究价格覆盖。 |
| 14-06-03 | `backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.preflight_summary` | `PreflightContractTestCase.test_adjustment_contract_and_coverage_are_hash_relevant_but_credentials_are_not` | `Ran 5 tests ... OK` | `canonical_hash` 绑定 policy/证据摘要和 coverage；改变因子覆盖会改变 hash，相同契约即使 verification 映射中的凭证形状字段不同也保持稳定。 |
| 14-06-04 | `backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.pit_status`；`build_data_preflight_payloads` | `PreflightContractTestCase.test_raw_is_allowed_while_inactive_and_payload_contains_contract` | `Ran 5 tests ... OK` | adjustment factor cutoff 作为契约标记，不自动加入 `non_strict` 家族；运行记录只复制显式 allow-list 的契约字段，不保存原始 verification 映射或凭证。 |
| 14-06-05 | `backend/app/backtesting/data/adapters/etf.py:EtfFactsAdapter.preflight_summary` | `PreflightContractTestCase.test_raw_is_allowed_while_inactive_and_payload_contains_contract`；`test_adjusted_basis_requires_active_policy_and_complete_coverages`；`test_adjusted_basis_blocks_missing_research_coverage_and_cutoff` | `Ran 5 tests ... OK` | raw+inactive/active 可继续；qfq/hfq 在 inactive、缺 coverage、缺 cutoff 或 policy/adapter 不匹配时 blocked；active 且完整 coverage 时 ready。 |

## T14-07：价格口径隔离

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-07-01 | `backend/app/backtesting/data/reports.py:DataPreflightReport.engine_price_basis`；`backend/app/backtesting/data/views.py:ChunkEngineDataView.bars` | `EnginePriceBasisIsolationTestCase.test_engine_accepts_raw_and_rejects_adjusted_queries_or_rows` | `Ran 1 test ... OK` | 报告契约固定 engine price basis 为 raw；engine view 在委托前校验查询口径，返回结果再次校验 Bar 口径，避免研究价格进入成交/估值链路。 |
| 14-07-02 | `backend/app/backtesting/data/views.py:ChunkEngineDataView.bars` | `EnginePriceBasisIsolationTestCase.test_engine_accepts_raw_and_rejects_adjusted_queries_or_rows` | `Ran 1 test ... OK` | qfq/hfq 查询或 provider 泄漏的 adjusted Bar 直接抛出 `ProviderContractViolationError`；raw 查询和 raw Bar 正常通过。 |
| 14-07-03 | 本任务变更集 | `Task14BaselineContractTestCase.test_preflight_and_strategy_view_keep_explicit_basis_surfaces` | `Ran 13 tests ... OK`（policy/基线组合） | 未新增或修改公司行动、账户、成交、撮合、手续费及估值算法；隔离只在 engine 数据视图边界完成。 |

## T14-08：自动化测试和验收

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-08-01 | `backend/tests/test_task14_policy.py`；`backend/tests/test_task14_preflight_isolation.py` | policy 激活门禁、不可变性、raw 可读及 inactive 阻断用例 | `Ran 32 tests ... OK` | 覆盖证据缺失/错误 key-version/policy 只读及 raw 不受门禁影响。 |
| 14-08-02 | `backend/tests/test_task14_adjustment_verification.py` | 真实源制品、双口径 native 比较、边界、精度和 hash 用例 | `Ran 32 tests ... OK` | 真实 Tushare 因子制品在缺少 native 输出时失败关闭，伪造或精度不一致均拒绝。 |
| 14-08-03 | `backend/tests/test_task14_factor_series.py`；`backend/tests/test_task14_preflight_isolation.py` | cutoff、缺失/重复/非法因子、身份/代码和 PIT 状态用例 | `Ran 32 tests ... OK` | 验证 effective_date 截止和完整覆盖阻断，不使用 updated_at 或 non_strict PIT 回退。 |
| 14-08-04 | `backend/tests/test_task14_factor_series.py` | qfq/hfq basis、独立源语义、无回退、raw 不变用例 | `Ran 32 tests ... OK` | 研究价格是新 Bar，因子点不当价格点，缺失语义不回退。 |
| 14-08-05 | `backend/tests/test_task14_baseline_contract.py`；`backend/tests/test_task14_preflight_isolation.py` | 策略口径选择、默认 raw、engine adjusted 拒绝用例 | `Ran 32 tests ... OK` | 策略/引擎视图边界和默认口径保持稳定。 |
| 14-08-06 | `backend/tests/test_task14_preflight_isolation.py`；`backend/tests/test_task14_adjustment_verification.py` | 预检/运行字段、hash 稳定性和敏感信息排除用例 | `Ran 32 tests ... OK` | verification 仅保留脱敏摘要与三类 hash，authorization/bearer 等敏感字段不会进入记录或 hash。 |
| 14-08-07 | `backend/tests/test_task14*.py`；`backend/tests/test_etf_data_adapter.py` | Task14 专项及 ETF 适配器完整相关测试 | `Ran 32 tests ... OK`；`Ran 31 tests ... OK`；compileall/diff-check 通过 | 所有相关自动化测试通过，修复旧自由字符串激活调用后未超出任务边界。 |

## 修复复审：14-08-07

| 检查表编号 | 代码位置 | 测试用例 | 测试输出 | 实现说明 |
| --- | --- | --- | --- | --- |
| 14-08-07 | `backend/tests/test_etf_data_adapter.py:make_verified_adjustment_policy` 及 adjusted-read 用例 | `python -m unittest discover -s backend/tests -p 'test_etf_data_adapter.py'` | `Ran 31 tests ... OK`；Task14 专项 `Ran 32 tests ... OK` | 将旧的自由字符串激活调用迁移为不可变、证据绑定的 `AdjustmentSeriesPolicy`。仅修改与失败项直接相关的测试调用，未改变其他模块契约。 |

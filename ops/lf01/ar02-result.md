# AR-02 全量完成判定与局部重试隔离

## 实现

- `data_store/coverage.py` 为 `finish_rebuild` 和默认全量审计提供同一判定：60 个业务入口集合齐全，封存输入确实扫描到终点，顺序分区游标耗尽，无未决操作，契约和当前 generation 匹配，文件契约兼容且当前问题已解决。
- 在既有 `data_store_entry_status.summary_json` 内分开保存最近操作、当前完整范围依据及最近完整依据；未决操作最多保留 16 组明确范围，超过上限要求一次完整扫描。输入摘要和范围摘要来自真实来源遍历及封存工作，不从 current 文件数倒推，不要求输入数等于输出数。
- 完整任务沿用 `pipeline.<entry>` 暂存身份，局部重试使用独立的 `.selected` 身份。不同封存选择冲突时拒绝覆盖；完成分区立即删除临时对象，完成依据落盘后才回收暂存。没有新增逐行成功台账或历史发布。
- `finish` 在同一事务内锁定状态、目录和质量表，检查后再写入 ready。审计在只读快照外复核 generation 和操作状态，覆盖只有状态变化但 generation 不变的交错情况。
- 审计新增 `full_coverage.json`，来源 CSV 保留操作模式、实际分区、状态、原因和本次输入计数；当前键数与 API 抽查范围继续独立列出。`--audit-local-only` 明确标识局部检查，永不输出全局验收通过。

## 自动化验收

主测试：`backend/tests/test_data_store_full_coverage.py`。所有场景使用专用 PostgreSQL 16 容器、随机测试 schema 和合成输入；Parquet、DuckDB、门控、审计及 API 路由均走仓库实现，没有替换 SQL 查询结果。测试文件系统使用 Docker Linux 数据卷，未注入文件系统探测绕过。

| 编号 | 测试位置及断言 |
| --- | --- |
| F01 | `test_F01_F02_F03_F04_full_range_survives_partial_retry_and_finishes`：真实本地表 300 个自然分区，前 256 个提交后局部 retry 成功，44 个剩余范围仍保留，finish 拒绝。 |
| F02 | 上述测试及 `test_F02_last_entry_only_selected_never_qualifies`：其余 59 个入口通过 NativeSources 实际完整空扫描建立依据，最后入口仅局部成功或空选择仍拒绝 ready。 |
| F03 | 上述组合测试：现有 256 个文件完整检查通过，全量审计仍定位 `FULL_RANGE_PENDING`。 |
| F04 | 上述组合测试：续跑仅提交剩余 44 个分区；前 256 个文件路径/哈希不变；60 个业务入口满足后 finish 真正转为 ready。 |
| F05 | `test_F05_failed_scan_is_not_empty_and_preserves_last_proof`（读取失败、超时、中断）；`test_F05_disabled_source_still_reads_local_history`；59 个真实空扫描。失败保留最近依据并明确未决，不把停用来源的本地历史视为空。 |
| F06 | `test_F06_correction_preserves_coverage_and_unrelated_files`、`test_F06_quality_repair_and_F07_rule_change`：局部修正保留完整依据，只改一个文件；未解决质量问题拒绝，真实修复后恢复。 |
| F07 | `test_F07_different_sealed_range_cannot_destroy_pending_work`、`test_F07_changed_input_selection_rejected_and_old_proof_not_borrowed`、规则变更测试；既有 `test_data_store_local_pipeline.py` 的 v1/v2 不兼容重建测试继续执行。 |
| F08 | `test_F08_F09_legacy_sealed_work_and_crash_recovery`、`test_F08_finish_fences_concurrent_status_writes`、`test_F08_audit_detects_status_change_without_generation_change`：提交后退出恢复幂等，真实并发写不能越过检查至 ready 的事务，审计检测状态变化。 |
| F09 | 上述旧封存恢复测试实际去掉新状态字段和封存字段后续接；`test_F09_old_complete_without_sealed_evidence_needs_readback` 不采信旧 complete，经本地来源重读恢复依据且不重写无变化文件。 |
| F10 | `test_F10_local_audit_cannot_claim_full_acceptance`：相同现有文件可通过显式局部审计，默认全量审计因缺少范围依据拒绝；局部结果明确 `global_acceptance=false`。 |

最终相关回归为 **483 passed in 84.40s**，覆盖全部 `test_data_store_*.py` 及 `test_legacy_reset.py`；`git diff --check` 和 release version 检查通过。完整测试输出见同目录 `ar02-tests.txt`。PR 的 required CI 通过后合入 main，CI 的完整记录以该 PR 自动记录为准。

## R01 衔接及升级

无需新增迁移、reset 或一次性初始化命令。由 R01 执行者在安全检查点应用合入后的 main，停止旧处理代码并按既有方式重启，确认一次运行版本已经更新。

继续使用原来的完整入口命令、mode、来源集合及重建选项即可。程序从保留的封存分区列表、顺序游标和实际提交 checkpoint/basis 补建依据，保留已经正确提交的 v2 文件。没有足够封存证据的旧完成摘要标记为 `FULL_RANGE_UNPROVEN`，需要对相应入口运行完整本地扫描；无变化文件走现有 no-op 路径，测试确认不重新生成 Parquet。不同未完成封存范围返回 `SOURCE_CONFLICT`，不能删除暂存以绕过；需先续接原范围，或另行处理冲突范围。

本次没有连接内网生产、调用供应商、部署、生产 finish 或清退数据。生产 R01 的实际进度与应用修复后的验收由现有执行者确认。AR-03 的源端实际变更发现及低成本增量仍不属于本包；不能由本包声称新增本地输入已自动纳入当前扫描边界。原有能力限制继续保留，API 验证仅为抽样。

# AR-01 持续更新失败隔离

## 修改摘要

- 删除跨调用的 `_local_batch`/`reused_batch_result` 完成门控。`run_local` 每次只给每个去重后的入口一个有界处理片段；已封存的入口续作，已完成的入口重新读取本地来源。导入目标在进入队列前展开、去重。
- 新增 `updates.py`，CLI 与默认 `data_store.update_local` 调度器共用逐入口失败隔离。来源读取、转换、领域预算与可恢复冲突记录为入口失败，其他可运行入口继续；调度器在处理后汇总失败，CLI 返回非零退出码。共享根目录/目录数据库/磁盘故障可停止新工作，并保留此前结果。未预期异常只记录类型和入口后使任务失败；KeyboardInterrupt/SystemExit 不被吞掉，CLI 收到取消信号返回 130。
- 用现有 `summary_json.refresh` 保存当前重试状态：`updated`、`unchanged`、`continued`、`backoff`、`deferred`、`failed`；实际尝试、获得工作槽、最后成功、最后完整来源扫描分别计时。退避、延后、恢复封存工作都不会伪造新的来源扫描时间。旧数据的可读性与本次刷新失败分开。
- 入口写锁覆盖处理和重试状态写入；状态补丁在数据库锁定最新行后只合并负责的字段，不覆盖 AR-02 完整覆盖依据。update/retry 可复用同一范围、同一规则的封存工作；不同来源选择或不同重建语义的冲突仍拒绝覆盖。

## 有界默认策略

- 失败退避：30 秒起，按连续失败指数增长，最大 1800 秒。手动 `retry` 立即尝试，但仍遵守入口锁、排序、质量、完整性与容量检查。
- 每次分派采用 900 秒协作式总工作预算；单入口最多使用本次 `pass_seconds`（默认 300 秒）工作片段，同时保留原来的 pass 数上限。底层来源和数据库 IO 仍受各自超时限制；预算不是强杀进程。
- 优先处理上次因总预算延后的入口，其次推进已有封存工作，再按实际获得工作槽的时间安排新扫描。没有获得槽位的入口不会因为一次失败 admission 被排到所有已刷新入口之后。
- 同一入口连续三次失败且没有推进游标时，先持久记录 `REPEATED_ENTRY_FAILURE` 中止状态，再在 admission/slot 锁下只释放它的暂存；current 文件与提交 checkpoint 不删除。进度仍在前进的工作不按此策略回收。不同封存范围的选择冲突不自动丢弃另一范围的依据，需要用原范围续接或另行处理冲突。
- 保留原共享暂存计费和写者预留；没有新增服务、永久运行轮次表或逐行成功记录。

## 验收

主测试文件为 `backend/tests/test_data_store_failure_isolation.py`。使用专用 PostgreSQL 16、随机 schema、真实 NativeSources、Parquet/DuckDB 和 current reader；只生成合成输入，不替换 SQL 查询结果。退避时钟在测试中显式前进，未等待实际长退避。

| 编号 | 测试及结果判据 |
| --- | --- |
| A01 | `test_A01_A02_default_scheduler_four_failed_rounds_refresh_healthy`：真实 `update_local` 处理链连续四轮，B 的本地表不可读，A/C 每轮新增本地观察并从 current 读到新值；恢复 B 的来源表后继续运行两轮默认调度，验证 B 恢复和下一轮新值刷新。 |
| A02 | 同一测试分别将 B 放在首位、中间、末尾；每轮每入口来源只调用一次，汇总失败发生在健康入口完成之后。 |
| A03 | `test_A03_sealed_300_partition_work_does_not_freeze_siblings`：300 个自然分区、每片段 64 个分区，五轮持续推进；B 来源只读一次，A/C 每轮读到新值，B 完成后再次进入正常扫描。 |
| A04 | `test_A04_A06_backoff_recovery_and_manual_retry_preserve_clocks`、`test_A04_sealed_failure_resumes_without_read_and_retry_bypasses_backoff`、`test_A04_source_file_error_is_entry_local_not_store_failure`：封存前失败不删除 current，封存后失败恢复不重读来源，缺失来源文件只影响本入口。 |
| A05 | `test_A05_stalled_continuations_release_only_scratch_after_bounded_failures`、`test_A05_finite_call_budget_defers_without_forging_attempt`，以及调整后的既有六入口/三槽位回归：写者仍有容量、续作仍计费、健康入口最终获得机会，调用预算延后不伪造尝试。 |
| A06 | 退避恢复测试验证到期前不调用来源、最后成功与来源扫描时间不变；手动 retry 可立即恢复。多轮真实调度测试显式前进时钟，验证到期后再次尝试。 |
| A07 | `test_A07_shared_fatal_conditions_stop_after_committed_work`、`test_A07_signals_and_unexpected_errors_are_not_swallowed`、`test_A07_cancellation_callback_preserves_completed_entry`：共享错误/中途取消停止后续入口，已提交 A 的数据仍在；异常负载不写入状态。 |
| A08 | `test_A08_cli_isolates_failures_and_deduplicates_import_target` 与默认调度测试：相同业务入口失败隔离一致，CLI 增加导入与显式目标后目标仍只执行一次，无供应商调用。 |
| A09 | `test_A09_retry_update_and_full_work_share_entry_lock_not_batch_state`：真实线程交错，局部 retry 不能越过已有入口锁，其他入口仍刷新；后续局部成功不覆盖全量未完依据，全量继续耗尽原封存工作。 |

原有顺序、同值确认、质量限制、整报告、no-op、完整可变表缺失删除、D06 schema 和 AR-02 F01–F10 回归均纳入测试。旧的“所有入口必须在同一轮共同完成且不重新读取健康入口”断言正是本包需要移除的约束，已改为验证每个入口独立完成和后续刷新、公平性及真实 current 值。

本地相关回归：529 项测试、2 个子测试通过；随后补强的默认调度器恢复测试三种入口顺序全部通过（3 passed）。唯一 warning 为既有 `TestTaskParameters` 构造函数导致的 pytest 收集提示。

自动化原始输出：`ops/lf01/ar01-tests.txt`。required CI 通过后通过 PR 合入 main；CI 的日志与具体提交关联由 PR 自动保留。

## 应用与边界

无需新增数据库迁移、环境变量或一次性初始化命令。R01 执行者在安全检查点更新到合入后的 main，停止旧写者并按现有方式重启，确认一次新逻辑已运行。原有封存工作、v2 正式文件、来源启用开关和 AR-02 门控保留；不再次 reset。

没有连接内网生产、调用供应商、部署、生产清退或 finish。尚未验证生产 R01 的实际状态。来源实际变更边界与低成本增量仍属于 AR-03，本包不声称具备该能力；既有供应商语义限制不作放宽。

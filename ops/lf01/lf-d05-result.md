# LF-D05 最终开发验收结果

当前复核状态：`partial / code_ready=false`。2026-09-27 main 复核发现 D02 的多分区续作、可变表无变化扫描、完整快照删除三项缺口；本轮最终复验完成前撤回 code_ready。修复与最新证据见 [本轮结果](lf-d02-d05-review-fixes.md)。

以下为固定旧提交的历史验收记录：C01–C18、B01–B05、共享回归及最终镜像命令链均已通过；实际环境仅为隔离开发测试。生产部署、停写、reset、真实重建、生产清理和逐领域生产验收均未执行。

## 输入与固定身份

- 任务包：`Quant_Foundry_LF-D05_v1.1.zip`，SHA-256 `a6b59fb0355c4d03252edd8186169f8114dbeaca799882b6bce062c24369d05d`。包内文字作为任务范围与验收依据，不覆盖用户和仓库的操作要求。
- 前序代码：LF-D04 `0cd1fe39f9ce1cb51135159187e9fcbf0c0d291d`；开发分支 `codex/lf-d05`。固定代码提交：`1d902395d33be2a41e30849222efbfa07e039f30`，文件树 `9f443f54327afbe27ae967f182515d1541c45ddb`，已包含最新 `origin/main`。证据提交为本结果文件所在提交；PR 链接在交付消息中记录。
- 依赖锁：`backend/uv.lock` SHA-256 `ee9e37f7315d9329cb4ca9b5358ca89892e56a577651a68cb965330555853c80`。
- 最终后端镜像：由 `backend/Dockerfile`、上述锁和固定代码提交构建，Linux arm64，本机标签 `qf-lfd05:1d90239`，镜像 ID `sha256:d1d555b962afaaa82034f3c2afab6e51213fe2a086eb1decdb9b457cbcc03f62`，本机大小 1,468,061,429 字节；固定基镜像摘要与构建命令见 [`lf-d05-artifact.json`](evidence/lf-d05-artifact.json)。构建脚本和镜像身份均不包含生产数据。
- 开发验收：Docker 内 Linux ext4、4 vCPU / 8 GiB 限额，Python 3.12.2、PyArrow 23.0.1、DuckDB 1.4.3、PostgreSQL 17；无文件系统探针注入。所有数据库名均为隔离 `_test`，未连接生产。

## 实现与可复核证据

新增 `app.data_store audit-export`。它在 PostgreSQL `REPEATABLE READ READ ONLY` 快照内核对目录绑定、71 个入口最新输入处理摘要、60 个业务领域当前键/限制/问题、控制库、垃圾与日志/暂存字节，对当前目录引用的每个文件核对哈希、Parquet contract、全部键与分区边界。快照前后核对 generation；调用真实 HTTP API 的领域描述、当前键查询和旧 release 拒绝断言；只输出键哈希，不输出原始业务行、Bearer token 或 DSN。预算、超时、缺 API、文件错误均标为未完成。`source_disposition.csv` 的处理行数与 `current_domains.json` 的当前键数分列；处理行数不是唯一历史观察数。

隔离最终命令链运行于同一后端镜像的全新 `lfd05_stage5_test` 库：`alembic upgrade head`、`legacy_reset status/enter/plan/export/verify/apply/status`、`data_store rebuild/update/retry/cleanup`、`legacy_reset finish/restore`、真实 HTTP API 与 `audit-export` 均实际执行并成功。`plan` 的 blocker 为 0、`verify` 为 true、`apply` 到 `reset_done`；救回包为 0 行，因为夹具原件在本演练中属于受保护共享来源，唯一原件救回由 C18 测试覆盖。71 个入口的 rebuild/update/retry 均 complete；60 个业务入口 cleanup complete。审计 135 项检查全通过，覆盖 1 个当前文件、1 个当前键；HTTP 仅抽样该键，不代表全库 HTTP 验收。原始证据在 [`evidence/lf-d05-stage/`](evidence/lf-d05-stage/)，其 audit SHA256SUMS 已复核；71 行处置表见 [`source_disposition.csv`](evidence/lf-d05-stage/audit/source_disposition.csv)。D05 的 39 个证据文件及 1 个行尾保真规则的哈希见 [`lf-d05-evidence-sha256.json`](evidence/lf-d05-evidence-sha256.json)。

跨容器 C12 在独立 Docker ext4 卷运行 [`check_current_store_containers.py`](../../scripts/check_current_store_containers.py) 的 `--docker-volume` 模式，真实执行初始化、争锁、强制终止、读锁下提交与清理、最终一致性共 8 步，全部通过，见 [`lf-d05-cross-container-ext4.json`](evidence/lf-d05-cross-container-ext4.json)。macOS 主机绑定目录不满足当前存储的本机文件系统准入，因此改用隔离 Docker 卷；没有放宽产品文件系统检查。

隔离审计的 60 个业务领域中，合成夹具覆盖 1 个可用领域，另外 59 个有明确的本地空状态；这不是生产领域验收。空间分项：当前正式文件 7,091 字节；控制库 614,400 字节；本地结构化日志 871 字节；当前暂存 74 字节；垃圾队列 0 文件/0 字节；救回压缩包 20 字节。原始业务源字节、峰值 spill 和其他系统空间没有从开发夹具推算，审计中为 `null`，需 R01 在目标环境另行采集。

## C01–C18 正确性结果

以下场景均在最终集成代码树上以真实隔离 PostgreSQL、Parquet 和 DuckDB 测试；详情见 [`lf-d05-backend-junit.xml`](evidence/lf-d05-backend-junit.xml)。未把早期包的声明直接算作本次通过。

| 场景 | 状态 | 本次实际证据 |
| --- | --- | --- |
| C01 同一输入 100 次 | 通过 | `test_C01_replay_100_current_only_no_row_ledger`；当前文件/逻辑行不增长。 |
| C02 乱序及同值确认 | 通过 | `test_C02_C07_new_confirmation_old_conflicting_value_and_missing_key`。 |
| C03 旧坏输入 | 通过 | `test_C03_C04_old_bad_does_not_block_new_good_and_new_bad_is_scoped`。 |
| C04 新坏输入 | 通过 | 同上；受影响范围受限，健康范围可读。 |
| C05 可解析源转换失败与修复 | 通过 | `test_C05_conversion_rule_failure_is_not_completion_and_can_retry`；保留原件后 retry。 |
| C06 报告成员错误 | 通过 | `test_C06_complete_report_bad_member_stays_blocked_when_only_sibling_changes`。 |
| C07 旧输入补缺失键 | 通过 | `test_C02_C07_new_confirmation_old_conflicting_value_and_missing_key`。 |
| C08 撤回、稀疏、空语义 | 通过 | `test_C08_explicit_withdrawal_never_resurrected_by_old_input` 及 local-adapters 稀疏/合法空回执测试。 |
| C09 tick 身份与精度 | 通过 | `test_C09_events_decimal_ns_timezone_and_HTTP_transport`、`test_C09_real_tick_file_query_precision_and_distinct_sequence`。 |
| C10 报告跨文件完整性 | 通过 | `test_C10_report_crosses_physical_files_before_any_member_is_exposed` 及非法成员测试。 |
| C11 generation 变化及历史拒绝 | 通过 | `test_C11_signed_cursor_continuation_then_changed_and_no_history`；审计真实 API 旧 release 参数断言。 |
| C12 锁与清理并行 | 通过 | 内核读写者测试，加上独立 ext4 跨容器 8 步实测。 |
| C13 各持久化点故障 | 通过 | `test_C13_process_crashes_all_durability_boundaries`、提交结果不明与删旧文件重试测试。 |
| C14 离线本地重处理 | 通过 | `test_C14_native_disabled_source_rescan_sees_late_commits_without_supplier`。 |
| C15 超时/磁盘/内存/输出预算 | 通过 | `test_C15_real_memory_disk_timeout_output_and_cancellation` 等有界测试。 |
| C16 schema 与规则变更 | 通过 | `test_C16_changed_rule_requires_explicit_bounded_rebuild` 及不兼容类型测试。 |
| C17 旧库与新空库 | 通过 | `test_C17_old_schema_plan_apply_resume_and_protected_source` 等旧库测试；stage5 全新空库迁移、原件保留及命令链。 |
| C18 reset 保护 | 通过 | `test_C18_wrong_database_unknown_object_fk_and_active_writer`、唯一原件救回/变更拒绝、归档软链、动态依赖等测试。 |

## B01–B05 资源与增长结果

[`lf-d05-b01-b04.json`](evidence/lf-d05-b01-b04.json) 是固定最终镜像上复验的 11 项实际负载，全部 passed，`complete=true`。B05 第一次全量运行只记录 `OperationalError`，未保存失败阶段；第二次完整写入 1000 万条后在热分片修正时诊断为 `sqlite3.OperationalError: database or disk is full`。两次都不计为通过，分别保留 [`attempt1`](evidence/lf-d05-b05-attempt1.json) 和 [`attempt2`](evidence/lf-d05-b05-attempt2.json)。原因是旧当前文件的 Parquet 联合 schema 含大量空列和可从归并赢家重建的元数据，临时 SQLite 文件因重复保存而触及原有上限。固定提交在归并时省略这些列，没有调高资源上限；[`10 万条热分片修正复现`](evidence/lf-d05-b05-hot-100k.json) 和 [`完整 B05`](evidence/lf-d05-b05.json) 均已通过。B06 是 R01 的真实全量项目，未执行。

| 场景 | 状态 | 实测关键值 |
| --- | --- | --- |
| B01 10 万/100 万/1000 万条 bar | 通过 | 1/4/40 个当前文件，840,321 / 8,269,553 / 82,695,558 当前压缩字节；分别 1.428 / 12.481 / 123.328 秒。1,000 万条的六个控制库关系共 352,256 字节（文件目录关系 147,456），日志 3,050 字节，结束时暂存 38 字节、峰值 15,214,090 字节，垃圾 0，进程 RSS 峰值 506,626,048 字节。 |
| B02 1/10/100 次同值更新 | 通过 | 100 万条始终 4 文件、8,269,553 字节，3 组均零正式文件写入；100 次后日志 29,831 字节。 |
| B03 追加 1% 与修正 0.1% | 通过 | 追加读取 0、写入 100,603 字节；修正只读 1 个文件 2,166,749 字节、写 1 个文件 2,192,918 字节。 |
| B04 10/100/1000 个背景分区 | 通过 | 三组均只读 1 文件 4,235 字节、写 1 文件 4,511 字节；当前文件分别 11/101/1001。 |
| B05 1000 万条 tick | 通过 | 1000 万条流式范围用时 2,423.631 秒，最终 10,000,010 行/108 当前文件；111 个原子范围而非逐事件事务。全次累计读当前文件 7,632,227 字节，热修正后 107 个未触及分区文件不变；重复纳秒时间戳的不同序号及大 Decimal 样本精确。输入 4,244,544,320 字节、正式写入 769,737,213 字节、峰值暂存 15,263,010 字节、峰值 RSS 646,004,736 字节；真实 ext4，无探针注入。 |

共享回归：后端/数据存储 `2684 passed, 1 skipped, 236 subtests passed`，见 [`JUnit`](evidence/lf-d05-backend-junit.xml)；唯一跳过项是既有 runner 迁移锁测试，未设置其可选 `QF_TEST_POSTGRES_DSN`，与 D05 当前存储验收无关。合入最新 main 后受影响的 73 项测试再次通过，见 [`postmerge`](evidence/lf-d05-postmerge-tests.txt)。根目录运维/版本测试 17 passed，见 [`root tests`](evidence/lf-d05-root-tests.txt)；前端 Node 测试 85 passed、TypeScript 构建与 Vite 生产构建通过，见 [`frontend tests`](evidence/lf-d05-frontend-tests.txt)、[`typecheck`](evidence/lf-d05-frontend-typecheck.txt)、[`build`](evidence/lf-d05-frontend-build.txt)；`scripts/release_version.py check` 为 0.3.0，见 [`release check`](evidence/lf-d05-release-check.txt)。前端存在既有 chunk 大小提示，未涉及本包代码。

## 交付命令与权限边界

最终实际命令与影响、退出、恢复见 [`lf-d05-runbook.md`](lf-d05-runbook.md)，隔离可复跑的准确顺序见 [`lf-d05-stage-rehearsal.sh`](lf-d05-stage-rehearsal.sh)。`audit-export` 的输出为新目录；检查不完整返回 2。`apply` 才会按已核计划删除旧数据，`rebuild/update/retry` 会写新当前数据，`cleanup` 仅清当前目录登记到期垃圾，`plan/export/verify/status/audit-export` 对业务与控制库只读。隔离演练真实执行过这些命令，但其结果不构成生产授权。

生产访问：未执行；部署：未执行；停写：未执行；删除：未执行；业务写入：未执行；生产清理：未执行。P01/R01 需要使用该固定提交与镜像，并在目标环境复核原件、旧限制、空间、领域业务区间和 B06。生产结果不得从合成负载或抽样 API 推断。

`code_ready`：当前为 false；以下历史通过仅限本结果记录的旧提交，不能覆盖新增反例；`ready_for_review`：是。P01/R01 仍需在目标环境完成部署授权、原件与旧限制复核、生产维护和真实全量验收。

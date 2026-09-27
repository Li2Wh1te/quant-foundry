# LF-P01 内网部署与维护就绪

状态：`ready_for_review`；`deployed_maintenance_ready=true`。

## 固定输入与实际部署

用户明确将 P01 输入改为 D06 修复合入后的最新 main。本次固定 `1da211611d559f603037c262d8f10cdc6a92972d`（PR #122 merge），运行代码与 D06 代码提交 `d52e5455bae8386acb86f54aadf1f3ba744c60ed` 完全一致。D06 最终 PR head `ea4ad8cb5757fd54a96547d7146f3cd712b66aa3` 的 Validate / Current store isolated acceptance 均成功。

P01 分支 `codex/lf-p01` 仅新增部署配置与交接证据，不修改产品运行代码。最初准备的 D05 构建在用户纠正后停止，未部署；原运行服务在此之前未停止、未迁移。

目标为已有内网 Linux amd64 主机。D06 原验收镜像是 arm64，本次从固定 main、相同 Dockerfile 和锁文件构建 amd64 同源制品，不宣称跨架构二进制相同。362 个 backend/app Python 文件逐项 SHA-256 一致，D06 清单 54 项一致；PyArrow 23.0.1、DuckDB 1.4.3，离线 CLI 导入通过。

- backend/runner：`qf-lfp01-backend:1da2116`，`sha256:41c8341a9c5e9e7ab2296f9c09ff02c06e571fb09922392bd96bf8bc3e229f95`。
- frontend：`qf-lfp01-frontend:1da2116`，`sha256:4a3f7d615468bd6039ed79ee9379e6a043946f40520a7dc4612c76ab9ee37caa`。
- 维护开始：2026-09-27 23:56:12 +08:00；新后端启动：23:57:30 +08:00。部署后检查跨至 2026-09-28。
- Alembic：`20261003_01` 经既有完整升级链至 `20261007_01`，包含非破坏性 schema/索引安装；未 stamp、未 reset。
- 访问入口保持原内网地址和端口。新 Compose 项目 `quant-foundry-p01` 仅管理三个应用服务，通过既有 `quant-foundry_default` 网络连接原 PostgreSQL；没有第二个数据库服务。

## 验收结果

| 检查 | 实际结果 |
| --- | --- |
| 健康、页面和鉴权 | backend / frontend healthy，runner running；页面 HTTP 200，readyz 200，无令牌 401。 |
| 维护门控 | status 为 DATA_STORE_REBUILDING；71 个入口完整返回且未完成；60 个领域为 rebuilding。只读 query 返回 503 DATA_STORE_REBUILDING，旧 API 返回 410。 |
| 旧写者 | 70 个旧正式化任务原本已暂停，enter 原样记录；活动旧 run、lease 均为 0。旧 backend、runner、frontend 及 6 个旧 campaign 容器保留且 restart=no。未发现旧正式化 systemd 服务或用户 crontab。 |
| 数据保护 | 原有所有表 OID 保持，旧发布数 93,398 与最后创建时间保持；原 PG 和日志卷保留。未替换/删除任何旧应用容器可写层或原件目录。 |
| 新业务未执行 | data_store_datasets / files / entry_status 均 0，data_store.update_local run 为 0；维护 completed_groups=[]、files_started=false。 |
| 共享目录与锁 | backend/runner 同一 quant-foundry-p01_current_store 卷、同一路径 /app/data/current-store、相同设备/inode；UID/GID 999 可写，主机 ext4，跨容器排他锁阻止第二容器取锁。 |
| 预算 | 余量约 878.9 GB；原有限预算保持：scratch 4 GiB、最低余量 1 GiB、进程 2 GiB、DuckDB 512 MiB/2线程、单写者，完整数值见 artifact.json。 |
| 环境与任务 | 初始化器仅补 QF_DATA_STORE_ROOT；已有环境值逐项不变。177 个任务的 state/version 保持，105 active / 70 paused / 2 completed；数据源启用状态不变，Tushare=false。 |

证据在 [evidence/lf-p01](evidence/lf-p01/)，其中 before/after 保留完整任务原状态和表身份；API 提供完整入口/领域摘要而非截断列表。运行镜像与预算见 artifact.json，查询门控见 query-gate.json，挂载与锁见 mounts-locks.json。证据不含 .env、令牌、完整连接串或付费原始业务行。

## 共享影响与未解决问题

应用切换短暂中断共享采集：新调度器把 4 个原运行标为中断，随后按原有 105 个 active 任务继续调度，未创建采集任务、未变更供应商设置。启动后基金净值采集出现 invalid_data / CollectionError，不能宣称共享采集全部成功；没有据此删除数据或修补生产代码。账户、策略和回测结果只验证保护边界，未执行真实业务操作验收。

既有 quant-foundry-backup.service 在本次停服前已于 2026-09-27 23:42:43 超时（22:42:43 开始），当前 auto-restart；本次未停止、修改或重跑备份，也未删除旧备份。需运维另行诊断，P01 不把备份健康标为通过。

存储目录没有正式业务文件；保留一个本次锁核验创建的零字节 `.p01-lock-probe` 控制文件，未初始化业务目录或运行 rebuild。原件唯一性、精确删除计划、B06 和逐领域真实数据验收留给 R01。

## R01 交接与权限自述

执行位置、固定命令和恢复边界见 [lf-p01-runbook.md](lf-p01-runbook.md)。R01 不恢复已弃用的 70 个旧任务，不启用 Tushare。P01 没有将共享任务改为 paused，因此没有需要通过 legacy_reset restore 恢复的新增共享任务清单；须另行处理 4 个中断运行和既有采集错误。

生产访问、部署、停旧写者、非破坏性迁移、只读检查：已执行。
旧数据删除、reset apply、业务 rebuild/update/retry、生产清理、生产故障注入、真实领域验收：未执行。

`reset_applied=false`；`rebuild_complete=false`；`domains_accepted=false`。未自动执行 LF-R01。

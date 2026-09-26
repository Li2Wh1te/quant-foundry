# LF-D05 交付命令与 P01/R01 执行手册

本手册对应 LF-D05 最终提交与同源 backend 镜像。D05 仅在隔离数据库和本地合成夹具执行；目标环境的原件、旧限制、磁盘余量、计划摘要与业务日期范围必须重新核对。`ops/lf01/lf-d05-stage-rehearsal.sh` 固定了隔离演练的实际命令顺序和输出文件，不能对生产数据库运行。

## 身份和固定输入

维护者先核对交付结果中的提交、`backend/uv.lock` SHA-256、镜像 ID 或 OCI 归档 SHA-256，再按目标环境设置已有 `QF_DATABASE_*`、`QF_CURSOR_SIGNING_KEY`、`QF_DATA_STORE_ROOT`。`QF_DATA_STORE_ROOT` 必须是 backend/runner 共享的受信任本机 ext4/XFS/Btrfs 挂载，两个容器内路径一致。DB 名只作为 `--expect-database` 参数传递；完整连接串、密钥和原始付费数据不写进命令、结果包或 PR。

下面用 `DB` 表示目标数据库名，`ROOT` 表示容器内已有绝对当前目录，`OUT` 表示新建的私有证据目录，`ARCHIVES` 表示存在时的旧专属归档目录。审计 HTTP URL 使用同容器回环地址或 HTTPS；Bearer token 只通过指定的环境变量传入。命令均在对应 backend 镜像的 `/app` 目录执行。仓库 `compose.yaml` 的 backend/runner 共用 `current_store` 卷，容器路径固定为 `/app/data/current-store`；`postgres_data`、`current_store` 和 `server_logs` 必须分别保留，不运行 `down --volumes`。

现有自托管 `.env` 用 `python3 scripts/selfhost_env.py --env .env --template backend/.env.example` 更新缺失的非密钥默认项并保留已有值。先在隔离副本核对目标环境 `.env`、Compose 渲染和镜像 ID；不要把 `.env`、`docker compose config` 的明文输出或完整 DSN 放入结果。下文表中 `python -m` 命令可通过 `docker compose --env-file .env -f compose.yaml run --rm --no-deps --volume "$HOST_EVIDENCE:/evidence" backend ...` 执行，其中 `HOST_EVIDENCE` 是已存在且仅维护者可读写的主机目录，容器内 `OUT=/evidence`。`audit-export` 应通过已启动的 backend 容器内 `docker compose exec -T backend ...` 运行，以回环 HTTP 检查同一进程的真实 API；其输出先写到当前目录之外的新私有目录，再复制到 `HOST_EVIDENCE`。

## 命令、影响与恢复

| 命令 | 影响与退出 | 失败后动作 |
| --- | --- | --- |
| `alembic upgrade head` | 增量安装 schema；不触发旧底座 reset、原件删除或业务重建。0 完成，非 0 停止部署。 | 检查当前 revision 和迁移错误，修正后重跑；不 `stamp head`。 |
| `python -m app.legacy_reset status --expect-database "$DB"` | 只读维护阶段与精确进度。0 完成，2 为拒绝。 | 核对库名与阶段，不跳过拒绝。 |
| `python -m app.legacy_reset enter --expect-database "$DB" [--pause-task-id ID]` | 写任务/维护元数据，暂停两个精确旧类型和显式共享任务；不删数据。 | 删除前放弃维护时用 `restore --include-legacy`；删除开始后不得恢复旧写者。 |
| `python -m app.legacy_reset plan --expect-database "$DB" [--archive-root "$ARCHIVES"] --out "$OUT/plan.json"` | 只读目标库系统目录，另写新计划文件。0 表示无 blocker，2 表示拒绝或有 blocker。 | 解决未知同前缀对象、外部依赖、活动写者和路径问题，重新生成计划；不放宽为 `CASCADE`。 |
| `python -m app.legacy_reset export --expect-database "$DB" --plan "$OUT/plan.json" --rescue "$OUT/rescue.jsonl.gz" --manifest "$OUT/rescue-manifest.json"` | 只读原件与计划；另写有界救回文件。 | 保留旧库，修正救回边界后用新路径重做。 |
| `python -m app.legacy_reset verify --expect-database "$DB" --plan "$OUT/plan.json" --rescue "$OUT/rescue.jsonl.gz" --manifest "$OUT/rescue-manifest.json"` | 只读复核目标库、计划和救回文件。 | 任何不符均停在清退前，重新取证。 |
| `python -m app.legacy_reset apply --expect-database "$DB" --plan "$OUT/plan.json" --sha256 "$PLAN_SHA256" --rescue "$OUT/rescue.jsonl.gz" --manifest "$OUT/rescue-manifest.json"`（旧专属归档存在时加 `--archive-root "$ARCHIVES"`） | **唯一旧数据清退入口**：按计划精确删除旧派生对象、已保全的唯一原件及计划内旧专属文件；保护共享数据。0 完成，2 拒绝。仅 R01 授权执行。 | 保留同一计划/救回文件，运行 `status` 和 `verify` 后以同一摘要重试；已完成组幂等复核，不恢复旧发布。 |
| `python -m app.data_store rebuild --root "$ROOT" --initialize [--rescue "$OUT/rescue.jsonl.gz"] --output "$OUT/rebuild.json"` | 显式从本地原生/已保全来源建立当前正式数据、状态与问题；不调用供应商。`--initialize` 只用于第一次新目录。0 全部合格，2 未完成或限制。 | 按入口/分区检查原因，保留原件并用 `retry` 或有界局部重建；不把失败当空。 |
| `python -m app.data_store update --root "$ROOT" --output "$OUT/update.json"` | 写本地增量当前值、状态和问题。 | 读取状态、确认来源依据后重试；提交结果不明时先核对 current generation。 |
| `python -m app.data_store retry --root "$ROOT" [--entry E07] --output "$OUT/retry.json"` | 对指定入口复走完整本地校验与有界正式提交。 | 依据明确问题修复原件/规则，不能通过清空问题记录冒充成功。 |
| `python -m app.data_store cleanup --root "$ROOT" --output "$OUT/cleanup.json"` | 只删当前目录已登记且到期的未引用垃圾；不删当前文件、活动暂存或原件。 | 查看 pending 和背压，确认无活动读写后重复。 |
| `python -m app.data_store audit-export --root "$ROOT" --output "$OUT/audit" --api-base-url http://127.0.0.1:8000 --api-token-env QF_AUDIT_API_TOKEN` | PostgreSQL `REPEATABLE READ READ ONLY` 快照；只读当前文件并抽样调用真实 API；只在新输出目录写脱敏证据。0 是本次边界内完整检查，2 为失败/超时/预算截断/未配置 API。 | 查看 `checks.jsonl`，修复后换新输出目录重跑；不要把未完成记为零或通过。 |
| `python -m app.legacy_reset finish --expect-database "$DB"` | 只在全部业务入口完整合格且旧限制定位后将维护元数据标为 ready。 | 保持维护态，处理入口或旧限制，不手工改 receipt。 |
| `python -m app.legacy_reset restore --expect-database "$DB"` | 恢复本次暂停的共享任务原状态；默认不恢复旧正式化类型。 | 查 `status` 与任务原状态，避免让旧 writer 重启。 |

`audit-export` 的 `--audit-seconds`、`--audit-files`、`--audit-bytes`、`--audit-rows`、`--audit-api-samples` 均为有限预算；超限返回 2，必须明确扩展测试边界后重新执行。导出的 `source_disposition.csv` 包含 71 个静态入口的实际最新处理摘要；`current_domains.json` 单列当前键数，不能把重复 pass 的处理行数说成唯一原始观察数。`checks.jsonl` 的 API 断言只覆盖标记的抽样业务键，哈希化范围身份，不代表全库 HTTP 读取。文件核对覆盖预算内的全部当前目录引用；不足预算即未完成。

## P01：部署到维护就绪态

1. 固定并核对提交/镜像身份；将证据与镜像归档保留在私有目录。执行 `docker compose --env-file .env -f compose.yaml stop frontend backend runner`，确认旧正式化 writer 已停且无活动事务。不得清空数据库、共享卷或归档。
2. 执行上述 `selfhost_env.py` 更新、`docker compose --env-file .env -f compose.yaml build backend`，核对 `docker image inspect quant-foundry-backend:local --format '{{.Id}}'` 与交付镜像身份；`docker compose --env-file .env -f compose.yaml up -d --wait postgres` 后用 `docker compose --env-file .env -f compose.yaml run --rm --no-deps backend alembic upgrade head` 增量安装。这一步只新增当前目录和维护 schema。用表中的 `status --expect-database "$DB"` 核对现状；旧库应保持明确维护态，新空库可直接 ready。
3. 用表中的 `enter --expect-database "$DB"` 暂停精确旧任务及现场确定的共享任务 ID，再执行 `docker compose --env-file .env -f compose.yaml up -d --no-build --wait backend runner frontend`。核对 `/readyz`、当前 `/api/admin/data-store/status` 与旧路由的 410。P01 不执行 `apply`、业务 `rebuild`、生产清理或逐领域完成声明。不要用 `make selfhost-deploy-backend` 代替此顺序，因为该目标会在 `enter` 前启动 runner。
4. 记录部署后的实际镜像 ID、Alembic revision、容器共享挂载身份和可用空间；交给 R01 的维护者。若在任何删除前放弃，按 `restore --include-legacy` 与既有部署回滚步骤处理。

## R01：目标环境原件保全、精确清退与重建

1. 在维护态重新核对目标 DB 指纹、旧限制、全部原件容器与活跃写者。运行 `plan`，要求 blocker 为空；对唯一原件运行 `export`、`verify`，保留计划 SHA-256 与私有救回文件。
2. 使用该次计划的 SHA-256 执行 `apply`。中断时复查 `status`、计划及救回文件后重试同一组；不使用全局 `CASCADE` 或卷清理。把实际删除对象、保护对象和空间结果写入 `reset_result.json`。
3. 从本地原件和必要救回文件执行 `rebuild`，随后 `update`、按入口 `retry`、有限 `cleanup`。逐领域核对输入处置、当前键、历史业务区间、字段限制和接入的本地增量。问题仍在时保持维护态。
4. 启动 API 后运行 `audit-export`；把其结果与实际 B06 真实全量指标、逐领域业务验收、部署配置、哈希清单组成 R01 结果包。全部完整且限制定位后才运行 `finish` 与 `restore`。抽样 API 检查不得代替真实逐领域验收。

R01 的私有导出目录示例：先执行 `docker compose --env-file .env -f compose.yaml exec -T backend mkdir -m 0700 /app/data/logs/lf-d05-audit`，随后执行 `docker compose --env-file .env -f compose.yaml exec -T backend python -m app.data_store audit-export --root /app/data/current-store --output /app/data/logs/lf-d05-audit/current --api-base-url http://127.0.0.1:8000 --api-token-env QF_API_TOKEN`，最后执行 `docker compose --env-file .env -f compose.yaml cp backend:/app/data/logs/lf-d05-audit/current "$HOST_EVIDENCE/audit-current"` 并复核导出哈希。目录须是新目录；本机实际服务端口若与 8000 不同，使用 `.env` 中 `QF_SERVER_PORT` 的回环地址。此示例运行时 backend 必须已启动且其进程使用交付镜像。

## 演练与尚未执行的生产阶段

LF-D05 的隔离演练、C01–C18/B01–B05 与共享回归见同包结果文件。生产部署、reset apply、真实业务 rebuild、逐领域生产验收和 B06 均由 P01/R01 另行记录，不能从开发测试或合成数据推断为已完成。

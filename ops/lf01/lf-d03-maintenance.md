# LF-D03 隔离维护工具交接

本文件是 D04、D05 和 R01 的操作接口说明。LF-D03 只在 `ops/lf01/compose/compose.yml` 定义的无宿主端口隔离 PostgreSQL 中运行过删除演练；下列目标环境命令尚未执行。任务包里的源码扫描结果只能作为线索，不能作为实际允许删除的对象清单。

## 阶段与入口

| 阶段 | 命令/动作 | 数据影响 |
| --- | --- | --- |
| 部署就绪 | `alembic upgrade head` | 只增设三张小型维护表，不删除旧表或来源；旧库、新空库均可安装 |
| 维护态 | `python -m app.legacy_reset enter --expect-database DB` | 记录并暂停两个精确旧 task type；只按显式 ID 暂停需要隔离的共享任务；旧排队 run 标记 skipped，历史保留 |
| 只读取证 | `status`、`plan`、`verify` | 不删除；`plan` 来自目标库 `pg_catalog`、函数定义、任务与专属归档目录 |
| 原件保全 | `export` | 只写新的有界 gzip JSONL 和清单，不改数据库 |
| 显式清退 | `apply` | 唯一删除入口；按 hooks、derived、originals、functions、files 顺序执行并留进度 |
| 本地重建 | `python -m app.data_store rebuild ...` | D02 当前底座从原生来源及必要救回文件重建；不调用供应商 |
| 重建完成 | `finish` | 仅在所有业务入口完整合格且旧限制已定位时标记 ready |

一般迁移、应用启动及 GET 均不调用 `apply`。D04 负责让新应用和 scheduler 根据 `DATA_STORE_REBUILDING` 停止旧正式化入口并给出维护态响应；D05 负责固化部署流程。新空库执行历史迁移后仍会有空的旧 `foundation_*` 结构，须通过显式计划与清退步骤移除，随后重建及 `finish`。不会在普通安装中自动删除。

## 目标环境前提

1. 使用维护窗口，停止旧 runner 的自动重启与两个旧正式化 writer，等待运行结束。确认原生采集暂停范围及共享任务 ID；不要修改供应商启用配置。D04 应把停写和维护态作为应用层门禁。
2. 使用具备目标库 DDL 权限且可调用 `pg_control_system()` 的维护角色；配置现有 `QF_DATABASE_*` 环境变量，不在命令、计划或回传中放完整连接串或密钥。所有命令都传预期数据库名 `--expect-database`。
3. 计划、救回、清单文件写到现有的绝对目录；目录及其上级不能通过符号链接跳转，文件必须是新文件。旧归档存在时提供绝对路径 `--archive-root`，末级目录必须是 `foundation-runtime-archives`，且为非符号链接。保留生成的计划、救回文件和摘要直到 R01 验收完成。
4. `plan` 的 `blockers` 必须为空。计划中的 80 张表、30 个函数等隔离计数不适用于目标库；目标库必须重新生成自己的计划和 SHA-256。计划会记录数据库集群 ID、数据库/schema OID、精确表/序列/触发器/函数定义摘要、旧任务 ID、外部依赖、专属归档和估计体积。未知同前缀对象、外部 FK/view、未审查动态 SQL、活跃旧 writer、越界路径均应先定位，不能放宽为 `CASCADE`。

## 命令顺序

在 `backend/` 工作目录执行，`DB`、`OUT` 与 `ARCHIVES` 由维护者在目标环境填写。命令只展示接口，不含目标环境取证结果。

```sh
alembic upgrade head
python -m app.legacy_reset status --expect-database "$DB"
python -m app.legacy_reset enter --expect-database "$DB" \
  --pause-task-id "$SHARED_TASK_ID"
python -m app.legacy_reset plan --expect-database "$DB" \
  --archive-root "$ARCHIVES" --out "$OUT/legacy-plan.json"
python -m app.legacy_reset export --expect-database "$DB" \
  --plan "$OUT/legacy-plan.json" --rescue "$OUT/rescue.jsonl.gz" \
  --manifest "$OUT/rescue-manifest.json"
python -m app.legacy_reset verify --expect-database "$DB" \
  --plan "$OUT/legacy-plan.json" --rescue "$OUT/rescue.jsonl.gz" \
  --manifest "$OUT/rescue-manifest.json"
python -m app.legacy_reset apply --expect-database "$DB" \
  --plan "$OUT/legacy-plan.json" --sha256 "$PLAN_SHA256" \
  --archive-root "$ARCHIVES" --rescue "$OUT/rescue.jsonl.gz" \
  --manifest "$OUT/rescue-manifest.json"
python -m app.legacy_reset status --expect-database "$DB"
python -m app.data_store rebuild --root "$CURRENT_ROOT" --initialize \
  --rescue "$OUT/rescue.jsonl.gz"
python -m app.legacy_reset finish --expect-database "$DB"
python -m app.legacy_reset restore --expect-database "$DB"
```

没有旧归档时省略 `--archive-root`。`enter` 中没有需暂停的共享任务时省略 `--pause-task-id`。原件容器为空时可省略 `export` 与 `verify`，但当前 CLI 的 `apply` 仍需 `--rescue`、`--manifest` 占位路径；这些路径不会被读取。`PLAN_SHA256` 必须是该次 `plan` 输出的 `sha256`，不能复用开发测试值。`rebuild` 的 `--root` 是 D02 当前数据目录；具体目录与签名密钥沿用 D02 配置。`restore` 默认只恢复本次记录的共享任务原状态，旧类型不会恢复。若在任何删除开始前放弃维护，可用 `restore --include-legacy` 恢复旧类型及共享任务；删除开始后拒绝恢复旧类型。

`status` 返回 `phase` 与 `code`，其中 `rebuilding`、`resetting`、`reset_done` 均对应 `DATA_STORE_REBUILDING`；`ready` 才可按当前底座对外提供合格读取。`finish` 对每个业务入口要求 `complete=true` 且 `qualified=true`，并拒绝尚未定位的旧限制。D04/R01 必须将保留的限制定位到新原生对象/成员/字段后再完成此步；不得为了让状态变绿而直接删掉限制。

## 原件与恢复

`qf-local-rescue-v1` 为 gzip JSONL，清单含目标库指纹、计划摘要、文件 SHA-256、逐类记录数及内容摘要。救回记录保留原生表完整行、旧暂存 dump 包装及采集时间、无法确定身份的请求原文；表变更前后像作为历史证据保存，不会冒充当前行。能证明原生表有完全一致主键与完整行的基线行不会重复导出。救回单条记录及单个旧基线最多 64 MiB，压缩/展开文件最多 4 GiB；超限、校验不一致或未知来源分类均返回拒绝，不能解释为空或完成。

`apply` 在有唯一原件时每组重新验证救回文件，并在派生/原件删除事务内锁住相关来源表、重算原件摘要。旧错误限制按最新未解决 issue 保存最小状态，包括旧范围、可证的领域、目标身份、字段、原因和原始定位信息；不能证明定位的记录保持 `located=false`。D02 使用 `--rescue` 时同时读原生表与救回文件，救回不能替代原生来源，也不能读取旧正式发布。

每个数据库组是独立事务；退出码 0 表示命令完成，2 表示计划有 blocker、条件拒绝或已知输入错误，意外异常为 1。`plan` 即使有 blocker 也会写出可审查文件并退出 2。中断后保留同一计划、救回文件和目录，重新运行 `status`、核对数据库及文件身份，再重复同一 `apply`；已完成组仍重新核对系统目录并返回 `already_complete`。旧对象已清退后不要恢复旧发布；修复新 reader 并从原生来源继续重建。路径删除一旦开始会记录 `files_started`，只对计划中精确哈希的旧专属文件做可重试的逐文件删除。

## 后续验收边界

D04：接入应用维护态、替换旧 router/runner/scheduler，解析当前限制并验证救回 reader；不让已退役旧运行内核成为本工具的依赖。D05：固化两阶段安装、共享目录、恢复和监控。R01：在获准的真实环境重新取证、救回、比对保护表/目录、执行 reset 与重建，记录实际空间、失败项和持续采集回归。LF-D03 没有连接真实环境，也没有执行真实暂停、删除、部署或业务写入。

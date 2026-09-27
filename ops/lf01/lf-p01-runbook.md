# LF-P01 实际部署与 R01 交接

固定运行代码：main `1da211611d559f603037c262d8f10cdc6a92972d`，含 D06 `typed-object-nodes-v2`。amd64 镜像身份见 lf-p01-result.md 和 evidence/lf-p01/artifact.json。D05 手册的维护命令仍适用，D05 镜像身份已被本次用户指定的新基线替代。

## 部署定位

内网 SSH 别名 `lee-quant-runner`；实际部署目录为 `/home/lemon/quant-foundry-p01-1da2116`，以下记为 `$DEPLOY_ROOT`。该目录包含固定源码、私有 `.env`、`compose.p01.yaml`、构建日志和完整私有 evidence。私有环境文件不提交、不打印。

当前服务：`quant-foundry-p01-backend-1`、`quant-foundry-p01-runner-1`、`quant-foundry-p01-frontend-1`。原 `quant-foundry-postgres-1` 持续运行，仍由旧项目管理，原部署目录 `/home/lemon/quant-foundry` 保留；不要执行旧项目的应用启动命令。新 Compose 不声明 PostgreSQL 服务，避免两个数据库进程同时挂载同一卷。

当前目录卷 `quant-foundry-p01_current_store`，两个应用容器均挂载 `/app/data/current-store`；日志沿用 `quant-foundry_server_logs`。旧归档仍在 `/home/lemon/quant-foundry/data/foundation-runtime-archives`，旧应用容器及可写层全部保留。P01 未将这些归档拷入新镜像或清理。

部署配置源见 `ops/lf01/compose/compose.p01.yaml`。该文件安装在固定源码根目录使用，构建上下文与 `.env` 均相对该根目录。

## 本次实际命令

```sh
cd "$DEPLOY_ROOT"
python3 scripts/selfhost_env.py --env .env --template backend/.env.example
docker build --label org.opencontainers.image.revision=1da211611d559f603037c262d8f10cdc6a92972d -f backend/Dockerfile -t qf-lfp01-backend:1da2116 .
docker build --label org.opencontainers.image.revision=1da211611d559f603037c262d8f10cdc6a92972d -f frontend/Dockerfile -t qf-lfp01-frontend:1da2116 .
# Old application containers were stopped and their restart policies set to no.
docker compose --env-file .env -f compose.p01.yaml run --rm --no-deps backend alembic upgrade head
docker compose --env-file .env -f compose.p01.yaml run --rm --no-deps backend python -m app.legacy_reset status --expect-database "$DB"
docker compose --env-file .env -f compose.p01.yaml run --rm --no-deps backend python -m app.legacy_reset enter --expect-database "$DB"
docker compose --env-file .env -f compose.p01.yaml up -d --no-deps --no-build --wait backend runner frontend
```

`DB` 从已有私有配置读取并显式传入，只是数据库名；不要传连接串。现有 secret/config 全部保持，初始化只补入 QF_DATA_STORE_ROOT。后端构建使用 Python 基镜像摘要 `sha256:5dc6f84b5e97bfb0c90abfb7c55f3cacc2cb6687c8f920b64a833a2219875997` 和 uv 摘要 `sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a`；内网 uv 下载慢时从本机传入同摘要 amd64 镜像，未修改 Dockerfile 或依赖锁。

## 后续只读状态命令

```sh
cd "$DEPLOY_ROOT"
docker compose --env-file .env -f compose.p01.yaml exec -T backend python -m app.legacy_reset status --expect-database "$DB"
docker compose --env-file .env -f compose.p01.yaml ps
```

鉴权 API 检查使用容器内已有 QF_API_TOKEN，只在进程内构造 Authorization，不回显；status 和 datasets 使用 limit=100，完整覆盖 71/60 项。维护态业务 query 返回 503 是预期门控，不应解除维护或注入样例来改变结果。

## R01 需要的条件

继续使用该固定运行代码、v2 格式与实际镜像；精确命令语法见 lf-d05-runbook.md。R01 需重新生成目标库 plan/export/verify 证据和审核摘要，识别旧表及全部旧容器中可能唯一的原件，再执行获授权的 apply 和从本地原件 rebuild；本次没有预先生成删除计划。旧归档需要时显式只读挂载到维护容器，并传实际 archive-root。不能直接执行隔离 stage-rehearsal 脚本。

备份 timer/service 保留原安排；其本次窗口之前的 timeout 和共享采集失败见结果文件。不得用恢复旧 writer 处理新应用故障，不可运行 down --volumes 或全局镜像/卷清理。P01 不恢复旧正式化任务，也不自动进入 R01。

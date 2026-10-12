# D18 集成与发行操作

完整验收以 [RESULT.md](RESULT.md) 和精确提交的自动输出为准。此目录不授权生产部署、权限调整或清理。

## 构建和隔离验收

从最新已复核提交、干净工作树执行。Python 3.12.2、锁定 Rust 1.90.0、Linux 原生受支持文件系统、已有 Docker/uv 是依赖。
两个生产 crate、SDK 和后端共用同一构建；构建在发行机完成，运行时不编译或联网安装。

```bash
export QF_S3_BUILD_ROOT=/absolute/private/s3-build
bash scripts/test_s3_engine.sh
export QF_S3_RELEASE_ROOT=/absolute/private/s3-release
bash scripts/build_s3_images.sh
python scripts/test_s3_images.py "$QF_S3_RELEASE_ROOT/images.json"
python scripts/test_s3_startup.py "$QF_S3_RELEASE_ROOT/images.json"
QF_S3_FINAL=1 QF_S3_RUNTIME_EVIDENCE="$QF_S3_RELEASE_ROOT/full" bash scripts/test_s3_runtime.sh
```

全量模式固定 B1、B2 两频率、B3 两类型和 B4；整个 runner/gateway、数据库和 worker 组最多 4 CPU / 8 GiB，批次最多两并发。
`QF_S3_FINAL_SMOKE=1` 仅调试，`QF_S3_FINAL_CASES` 仅重跑指定相关族；两者均不能输出全量通过。
本地 overlay/tmpfs 不是正式文件系统证据。全量模式不使用 `LF_D01_ALLOW_OVERLAY_TEST` 的探针替身。
原生双架构构建在标准 Linux x86_64/arm64 CI 分别执行，不把跨架构二进制说成相同。
[GitHub 标准 runner 规格](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)是环境选择依据，实际结果记录架构、文件系统和容器配额。

输出只有本次 `release.json`、`images.json`、wheel 和 `final-result.json`；不是部署历史台账。
构建源、测量业务源及 native/wheel SHA 分别校验。算法/schema改变需重测相关负载；纯文档更新可核对源摘要后复用。
合成行情只写一份 CurrentStore，不按 run 复制或归档旧输入。结果分片是业务结果；正常 worker/tmp/IPC由 supervisor 自动回收。
缓存为构建后共享 OS 暖缓存，没有强制清空宿主页缓存。阶段计时有嵌套关系，不能相加当总墙钟。
`selected_file_bytes` 是正式读取器选中文件预算量，包含重复选中；不是宣称同等物理 SSD I/O。

## P01 命令准备

`FormalProvider.check/binding` 和 E50/E70 原节点适配已实现，隔离 CurrentStore/SDK/Rust 路径已验证。
默认正式 API 还缺底座已接受的小型执行事实解析器，不能靠改配置启用。
P01需先落实下列依赖，再以真实已可用数据验证。下面命令尚未在生产执行。

| 依赖 | 必需来源/字段和现有边界 |
|---|---|
| 执行价格和数量 | E50 已声明 unadjusted，但 CNY/原量到整数份额待接受；E70 yuan/lots 描述仍须 raw 依据和精确手转份规则，不能仅凭字段名取 100 |
| 历史身份 | 既有 instruments 的稳定身份/来源代码和有效日期覆盖请求；当前名单/显示代码不能回填历史 |
| 会话和规则 | 既有 calendars 已接受交易日/时区/午休区间，D03 CalendarSession；D02 日期适用状态、单位、价格限制和费用 DTO |
| 公司行动与成本 | 实际会话的已接受状态/公司行动检查；受理时捕获账户佣金，法定费用不能填零；事实改变终止 DATA_CHANGED |
| 研究知识时间 | 财务/估值/行业/成分可靠公开/生效边界；报告期/采集时间/当前快照不能替代。未发布 E40 不参与 |

最小受支持接入为可信代码解析器返回 `AcceptedRunPlan(market, spec, session_facts, check_facts)`，
用 `daily_node_binding` 显式传入已接受 representation/identities/calendar/shares_per_quantity/evidence，
经 `FormalProvider(..., plan_resolver=...)` 接入原 `create_services`/API provider 端口。
不新增 REST 字段、环境开关、行情副本或事实发布台账。`check_facts(work, connection=...)`
须在最终提交的同一有界事务复核/锁定既有小型事实，普通读取也复核变化，不远程调用供应商。
解析器尚未发布，上述接口不是生产可用声明。真实节点/逆序标识/分页/窗口/缺量/分数/溢出/
质量变化/实际 FormalProvider → Rust/Python 回归见 `backend/tests/test_s3_formal_runtime.py`，
实际 runner 容器路径见 `tests/s3_runservice_suite.py`。
Web 只读现有结果根中的签名当前 runner 检查，五秒更新/三十秒过期，绑定 boot/固定
worker 镜像；停止撤销自身值，崩溃后自动过期。无历史登记，Web/worker 不挂 Docker socket。
原结果根必须已有批准的同 UID 权限，缺失/过期/不匹配明确拒绝；不自动修改权限。
部署所有者需提供现有 Compose 项目身份、已获准 Docker daemon 访问、同一宿主工作根映射、结果根和既有 UID/GID。
不得用 chmod/chown、额外宿主挂载、来源切换或 R01 reset代替依赖核验。

发行机可封装已实际验证的两个镜像，然后在获准目标导入；不重新构建供应商或市场数据。

```bash
export QF_S3_BACKEND_IMAGE=$(python -c 'import json,os;print(json.load(open(os.environ["QF_S3_RELEASE_ROOT"]+"/images.json"))["images"]["backend"]["id"])')
export QF_S3_WORKER_IMAGE=$(python -c 'import json,os;print(json.load(open(os.environ["QF_S3_RELEASE_ROOT"]+"/images.json"))["images"]["worker"]["id"])')
docker save --output "$QF_S3_RELEASE_ROOT/s3-images.tar" "$QF_S3_BACKEND_IMAGE" "$QF_S3_WORKER_IMAGE"
docker load --input "$QF_S3_RELEASE_ROOT/s3-images.tar"
```

先在原服务部署配置暂停新接单；保留状态、取消和历史读取，排空或显式终结旧 queued/active，核对未知结果。
不能让两个 supervisor 认领同一队列，不能把旧任务自动转为 Rust任务。
为既有项目填入已核准的 `QF_S3_TRUSTED_UID/GID`、`QF_S3_DAEMON_SOCKET_GID`、`QF_S3_HOST_WORK_ROOT/RESULT_ROOT`。

```bash
export QF_S3_ACCEPT_NEW_RUNS=false
docker compose -f compose.yaml -f ops/s3-d18/compose.s3.yaml config --quiet
# 旧活动任务核对完成后；不要同时保留旧 runner。
docker compose -f compose.yaml -f ops/s3-d18/compose.s3.yaml stop runner
# 使用已安装制品做正常升级；升级入口的空库和非空旧库路径已隔离验证。
docker compose -f compose.yaml -f ops/s3-d18/compose.s3.yaml run --rm --no-deps --entrypoint python backend \
  -m alembic -c /app/alembic.ini upgrade head
docker compose -f compose.yaml -f ops/s3-d18/compose.s3.yaml up -d --no-build --no-deps --force-recreate backend
docker compose -f compose.yaml -f ops/s3-d18/compose.s3.yaml up -d --no-build --no-deps --force-recreate runner
```

只有正式依赖核验通过且P01获准后，才将 `QF_S3_ACCEPT_NEW_RUNS=true` 并重建 backend 容器。
只读 preflight/capabilities保留；暂停期间新的 run/batch/失败项重跑返回明确拒绝。
凭据通过已有受保护环境提供，不写进命令参数、报告或策略 worker。

```bash
qf backtest capabilities
qf backtest preflight /absolute/approved/config.json
qf backtest run /absolute/approved/config.json --idempotency-key UNIQUE_APPROVED_KEY
qf backtest status EXACT_RUN_ID
qf backtest result EXACT_RUN_ID --kind summary
python ops/s3-d18/verify_result.py EXACT_RUN_ID
qf backtest cancel EXACT_RUN_ID
```

核对命令仅分页读取现有结果，检查完整终态、行数、权益终点和精确费用，输出摘要/hash；不重算或覆盖历史。
不可估值/partial/legacy 结果明确不标完整核对通过，仍可沿原只读入口查看。
停止新接单重复上述false/backend重建；确认任务已终结后正常停止runner。未验证可逆降级时停止接单并修复前进，不机械downgrade或启用旧worker读取新schema。

## C01 命令准备

P01通过后才由C01对精确已分类的旧专属对象执行。所有业务结果、策略、账户、正式/原生行情、共享卷及迁移链继续保留。
当前不删除生产容器、镜像或卷，不执行全局prune、CASCADE或目录前缀批删。

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/backtest_cleanup.py \
  --database-name EXACT_APPROVED_DB --root /absolute/approved/retired-temp-root \
  --path EXACT_STOPPED_ATTEMPT --table public.EXACT_EMPTY_TEMP_TABLE --output /absolute/private/c01-plan.json
# C01明确授权apply，且当前身份/引用/活动状态复核通过之后：
PYTHONPATH=backend backend/.venv/bin/python scripts/backtest_cleanup.py --apply-plan /absolute/private/c01-plan.json
```

连接由 `QF_D17_CLEANUP_DATABASE_URL` 独立提供。输出文件必须不存在；未知/非空/有引用/路径替换/链接/活动进程均拒绝。
完整标记、保护和失效规则沿用 [D17工具说明](../s3-d17/README.md)，不能仅凭文件或表名前缀分类。
清理后重新验证共享业务原值和旧历史只读结果；D18没有执行P01/C01。

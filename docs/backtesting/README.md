# CLI 与 Python 回测指南

Python 3.12 安装预构建 `quantfoundry-sdk` wheel。CLI、Python 和网页调用同一
`/api/admin/backtests` 服务，由可信 runner 启动受限 worker，Rust 完成账户、撮合和分析。
客户端没有本地策略执行命令。生产数据就绪与引擎能力分别列出；当前正式 D04/D05
映射仍未完成，正式预检会明确拒绝，不能据合成示例宣称真实行情可用。

从干净环境安装发布 wheel（`WHEEL` 是已取得的文件路径）：

```sh
python3.12 -m venv /tmp/qf-client
/tmp/qf-client/bin/python -m pip install "$WHEEL"
/tmp/qf-client/bin/qf backtest --help
```

SDK 依赖随 wheel 安装；不需要源码 `PYTHONPATH`、后台现场编译或旧 Python 内核。
源码中的 [五类策略](../../examples/strategies/README.md) 都是独立文件，只 import
标准库和已安装的 `quantfoundry.api`。策略修订/账户先在服务建立，再提交其 UUID；
不能把样例 UUID 当作自己账户。完整配置来自 `run_configs.json` 的每项 `config`，
剥离 `example/requires/synthetic_configuration` 说明字段后替换为自己的实际记录。

用 `QF_BASE_URL` 提供服务源站，例如本机隔离服务 `http://127.0.0.1:8000` 或已配置
HTTPS 服务。`--base-url` 放在 `backtest` 与子命令之间，也可覆盖环境值。通过既有
秘密管理器把 `QF_API_TOKEN` 注入进程环境；交互 CLI 在环境未设置时使用隐藏输入。
不把 token 写进 argv、URL、配置 JSON、脚本、日志或 shell 历史。非本机 HTTP、URL
用户名/密码、查询、片段、路径前缀均拒绝；TLS 校验开启。客户端直连指定源站，不读取
代理环境、不跟随任何重定向、不自动重试提交。错误不回显原始请求/连接异常；服务回显
当前 token 时输出也会脱敏。原生策略的网络禁用由 runner 实施。

下列变量均为非秘密：`CONFIG` 是完整配置文件；`KEY` 是用户保存的唯一提交键；
`RUN_ID` 来自 submit；`CURSOR` 来自上一页的 `next_cursor`。输出均为单个 JSON 对象。
成功退出码 0；参数、传输、服务拒绝及有 errors 的 preflight 退出码 2，安全错误写 stderr。
状态查询成功可返回 failed/cancelled，调用方仍须检查 `status/partial/error`。

```sh
qf backtest capabilities
qf backtest preflight "$CONFIG"
qf backtest submit "$CONFIG" --idempotency-key "$KEY"
qf backtest run "$CONFIG" --idempotency-key "$KEY"
qf backtest status "$RUN_ID"
```

`run` 是 submit 的别名，均仅受理；没有隐藏等待或本地执行。preflight 是只读检查，
不创建运行、不执行策略。提交会重新检查，不保证上次绿色预检仍有效。相同认证所有者、
同一键和相同归一化配置返回同一 run；同键不同配置报 `IDEMPOTENCY_CONFLICT`。
有意重新回测须使用新键。提交超时可能已受理，先以原配置/原键重试核对。

终态后读取摘要或一页结果；循环分页由调用者控制，不默认下载完整成交/日志：

```sh
qf backtest result "$RUN_ID"
qf backtest result "$RUN_ID" --kind trades --limit 1
qf backtest result "$RUN_ID" --kind trades --limit 1 --cursor "$CURSOR"
qf backtest cancel "$RUN_ID"
```

`summary` 不接受 cursor。其他 kind 是 equity/orders/trades/positions/records/logs，
最多 10,000 项、每个 HTTP 控制消息最多 1MiB；过大请减小 limit。游标绑定所有者、run、
kind、limit，翻页保持相同参数。未终态结果报 `RESULT_NOT_READY`。取消响应可能只是
`cancel_requested=true`，继续查询状态；succeeded 后取消保持 succeeded。
failed/cancelled 不能作为完整收益；已写中间结果带 `partial=true`，未启动取消可无中间结果。
无保存分片时 summary 可能为空，
仍返回 outcome/error。日志截断和不可计算指标理由随 notices/指标对象返回。

批次文件 `BATCH_CONFIG` 的唯一顶层字段是 `configs`，为 1..256 个完整 RunConfig。
每项仅 parameters 可变，同一策略修订、账户、日期、频率、标的和市场/费用模型；在客户端
显式展开参数，不上传无限网格。`BATCH_ID/BATCH_CURSOR` 来自批次响应。

```sh
qf backtest batch "$BATCH_CONFIG" --idempotency-key "$BATCH_KEY"
qf backtest batch-status "$BATCH_ID" --limit 1
qf backtest batch-status "$BATCH_ID" --limit 1 --cursor "$BATCH_CURSOR"
qf backtest cancel-batch "$BATCH_ID"
qf backtest retry-failed "$BATCH_ID" --idempotency-key "$RETRY_KEY"
qf backtest compare "$RUN_ID" "$OTHER_RUN_ID"
```

批次页最多 256 项且受字节预算限制；cancel-batch 的响应仅第一页，完整终态需分页查询。
retry-failed 仅对已为 failed 的项创建关联新 run，保留已成功项；重试请求也复用原键。
当前 D13 API 不自动重试主动 cancelled 项，需取其配置并以新提交键单独 submit；
上面的 cancel-batch 后若只有 cancelled 项，retry-failed 返回空 run_ids。
compare 接受 2..16 个不同的有权限 run，展示配置/费用/公式差异，不重算旧历史或给排名。

同一配置的 Python 入口：

```python
import json, os
from pathlib import Path
from quantfoundry import BacktestClient, RunConfig, BacktestError

config = RunConfig.from_dict(json.loads(Path(os.environ["CONFIG"]).read_text()))
client = BacktestClient(base_url=os.environ["QF_BASE_URL"], token=os.environ["QF_API_TOKEN"])
checked = client.preflight(config)
if checked["errors"]:
    raise BacktestError(checked["errors"][0]["code"], checked["errors"][0]["message"])
accepted = client.submit(config, idempotency_key=os.environ["KEY"])
status = client.status(accepted["run_id"])
# 仅在终态时读取；result 默认 summary，一次只取一页。
```

完整 preflight→submit→轮询→result 脚本见 [http_workflow.py](../../examples/http_workflow.py)，
只依赖已安装 SDK（配置文件与提交键是非秘密参数）。它返回 0 仅表示运行成功且结果完整；
等待超时不自动取消服务器任务。可用下面的同一调用验证：

```sh
python -I examples/http_workflow.py "$CONFIG" "$WORKFLOW_KEY"
```

[API 速查](api.md) 列出全部配置、返回与时间/成本语义；
[迁移说明](migration.md) 用新副本及不可变修订保护旧策略和历史。

开发验收：Python 3.12/Rust 1.90.0 下 `bash scripts/test_s3_engine.sh` 构建并在独立 venv
安装 wheel；`bash scripts/test_s3_runtime.sh` 使用该 wheel，创建无网络的私有 PG/runner
和兄弟 worker，运行五类完整例子（intraday 同时测 1m/60m）及本文命令。该脚本是仓库
隔离测试工具，需要已授权本机 Docker 和已构建的 `QF_S3_BUILD_ROOT`，不能用于生产种数。
机器证据保留于现有 `s3-worker-isolation` Actions artifact。合成数据不调用供应商，
不修改正式门禁/来源，不是 D18 全 B1–B4 或生产拓扑验收。

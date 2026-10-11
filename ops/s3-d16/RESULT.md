# S3-D16 v1.1 实施结果

从 D15 已合并的 main `91b7a876bf1bb49f962ad0e4e026edb50a38b8eb` 接续。结果、比较及旧历史 URL 消费 D13 同一 REST 结果合同；复用已合并的 S2 样式、Select 与现有图表，没有修改公共 Shell、DataAssets、CurrentStore 或 S2 未发布候选。

结果摘要、权益、回撤、订单、成交/费用、持仓、record 与日志分节展示。每个结果请求最多 100 条，一节只保留当前页；没有自动追游标下载全部 Tick/日志。曲线明确是服务保存的采样页，原始权益、价格、费用和纳秒保留字符串。金额图使用精确差值后才投影到绘图坐标，读数仍显示原值。NA、缺价、日志截断、失败和取消的部分结果明确显示，不补零，不从持仓量重新计算旧市值。

比较最多 16 项，可按参数/运行 ID 与终态筛选；基准和对照曲线分别分页，单次仅加载两条当前页。显示日期、频率、公式、基准、撮合假设和费用差异；缺口径或部分结果不计算误导性差值，无最优排名。原保存比较成员、顺序、名称及历史结果只读保留。旧重新执行按钮进入新配置，不调用旧引擎。页面 URL 保留游标，比较→结果→返回恢复位置；取消、卸载和乱序响应丢弃，401/403 及 D13 隐藏 404 清除缓存。

删除已无消费者的前端全结果累积读取/导出、基于全权益重算的月度展示、旧比较写按钮/客户端与对应旧浏览器脚本，改用 D16 有界浏览器回归。没有删除历史业务数据或后端旧核心；D17 不在本模块范围。

## 实际验证

- `make root-test release-check`：18 项通过，版本一致。
- `pnpm -C frontend test`：152 项通过；`pnpm -C frontend build`：通过。
- `QF_S3_PYTHON=backend/.venv/bin/python QF_S3_BUILD_ROOT=/tmp/qf-s3-d16-build bash scripts/test_s3_engine.sh`（Rust 1.90.0，独立可写构建目录）：fmt、clippy、Cargo 全套、共享合同检查、wheel 构建及 76 项 SDK 测试通过。
- 私有 PostgreSQL 下 `cd backend && LF_D01_ALLOW_OVERLAY_TEST=1 uv run --locked python -m pytest -q tests`：2938 通过、3 失败、5 跳过、236 个 subtests 通过。两项失败为云端 overlay 文件系统探测，一项为 PID 1 不回收孤儿进程。两项在只影响原测试的文件系统探测替身下补跑通过；孤儿进程原测试在 Docker `--init --network none` 下通过。没有修改生产探测、资源政策或原测试断言，也不表述为一次本地全量全绿。
- `CI=true CHROMIUM_PATH=/usr/bin/chromium LF_D01_ALLOW_OVERLAY_TEST=1 QF_D16_UI=1 QF_S3_BUILD_ROOT=/tmp/qf-s3-d16-build QF_D15_EVIDENCE=/tmp/qf-s3-d16-evidence/verified-isolated bash scripts/test_s3_run_ui.sh`：退出 0，D15 创建/运行/取消及 D16 结果/比较的四种浏览器模式均通过，接口替身与真实服务证据分别输出；CI 保留同一工作流并上传 `s3-run-ui-browser-evidence`。
- 本地 `QF_S3_BUILD_ROOT=/tmp/qf-s3-d16-build QF_S3_RUNTIME_EVIDENCE=/tmp/qf-s3-d16-evidence/runtime bash scripts/test_s3_runtime.sh` 两次因执行环境磁盘满在镜像构建阶段退出 102，尚未进入测试。只清理本会话 Git 忽略的 Cargo 构建缓存，源码、wheel 和证据保留；没有扩大到其他对象。既有 S3 worker isolation CI 保留原脚本，精确 head/main 的该检查通过才可合并/报告完成。

真实浏览器链路为 Chromium → HTTP → 私有 PostgreSQL / 真实 CurrentStore → runner 容器 → 受限兄弟 worker / 已构建 SDK / Rust。16 参数运行 15 成功、1 独立失败；新 record 超过 100 条实际翻页，取消迟到响应后重读。旧样本包含 105 个保存点、大 Decimal、缺价和 NA；摘要、金额、纳秒、ID 与保存比较成员前后逐值核对。另验 390/1024/1440、键盘滑块/模态、返回位置、20 项运行选择分页、局部失败、401 与隐藏 404。403 与部分网络乱序由独立接口替身验证；沿用 D13 私有认证夹具切换主体的方法，真实认证与所有权读取返回隐藏 404 并清除缓存，随后恢复夹具。业务 run 所有者和正式凭据不变，不删除或重算结果。

代码复核及浏览器修复：D13 compare 请求不接受旧 baseline 字段、D11 Metric 变体与 summary 形状、快速筛选 URL 竞态、权限撤销中止并发请求的迟到错误、旧保存成员 URL 被改写时恢复原定义、摘要/分页失败保留原值并提示。精确 PR head 和合并 main 的必要 CI 以 GitHub 自动输出为准。

## 未关闭依赖

正式 D04/D05 数据映射、R01/S2 正式可读性、分钟/Tick 生产数据、D17 旧核心物理退出、D18 全量 B1–B4 与生产 runner 拓扑仍未关闭。本次 16 组小隔离夹具不冒充 B4；合成规则和隔离容器成功不代表生产验收。未访问生产/供应商、未部署、未改凭据或安全/资源政策，未开始 D17。

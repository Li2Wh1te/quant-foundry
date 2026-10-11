# S3-D14 结果

从实际 main `5b756511af2cc989a6c708859b227c9b4ec0027d` 接续；仅 D14，无生产部署、
供应商请求、来源开关或用户业务数据删除。工作区未提供 AGENTS.md/本地 .agents skill。

实现真实 HTTP BacktestClient、qf backtest CLI、完整 RunConfig wire 读取、五个自包含
策略/配置及使用/API/迁移文档。认证沿既有 Bearer；环境/隐藏输入，不接受 token argv；
拒绝重定向/代理、远端明文 HTTP、URL 凭据；1MiB 请求/响应与有界分页，安全结构化错误，
回显凭据脱敏，无自动提交重试。唯一服务、runner、Rust 核心，未加入客户端执行器。
RunConfig/金额/ns/资本/费用/幂等沿 D01/D13；周/月选择后每日继续等待实际卖出再买。

实际执行的本地检查：`QF_S3_PYTHON=backend/.venv/bin/python QF_S3_BUILD_ROOT=...
bash scripts/test_s3_engine.sh` 的 fmt/clippy、Rust workspace/public contract、生成签名、
独立干净 wheel 安装通过；第二遍 SDK/HTTP/CLI 为 76 项通过，含畸形 HTTP、连接失败和
超时的安全错误验证。D13/D14/共享账户/策略/历史/runner/队列/IPC 受影响回归 119 passed、
2 skipped（真实 worker 只在隔离 CI 强制执行）、6 示例子项通过。D14 的
`QF_S3_D14_REQUIRED=1 ... pytest -q tests/test_s3_cli.py` 使用私有 PG、实际 HTTP/CurrentStore，
为 4 passed、1 skipped、6 示例子项通过，核对 CLI/Python 幂等和失败提示、新策略/新修订
保护旧稿/旧精确结果，以及完整 Rust 策略回调和跨会话换仓。另按指南创建只安装 wheel
及公开依赖的独立 venv，8 个 HTTP 边界检查、五个策略 import 和该真实服务的 CLI 检查
通过；客户端未安装后端或测试依赖。D04/D05/IPC 的 83 项机制回归通过；开发容器使用
既有 overlay probe 注入，不当作正式文件系统或生产验收。真实 worker 以最终准确 head
的 [PR #172 checks](https://github.com/Li2Wh1te/quant-foundry/pull/172/checks) 和 artifact 为准。

真实 worker 回归接入现有 `scripts/test_s3_runtime.sh`，强制无跳过：五类 preflight→
submit→status→result；intraday 1m/60m 都跑；CLI/Python 两次同配置的 performance/模型
一致；同键重放与网页同一路径；Tick 同纳秒72次；未来请求失败/部分结果、拒单检查、
文档15条 qf 命令与 Python 完整脚本。`d14-examples.json` 在现有隔离 artifact 保留。
宿主回调检查不冒充受限 runner 容器拓扑。

发现并先回报 S3 共享数据顺序边界：多标的 security-first 存储不能仅排序单页就声明
跨页全流有序。S3 gateway 明确拒绝缺少可验证顺序的完整回放，研究 count 窗口与单一
纳秒查询仍支持；第二遍修复该区别，未改变 ReadView 的模拟时点裁剪。
补单标的身份谓词，正确消费已有 time-first 布局。合成夹具显式建立按完整事件键有序的
唯一测试目录，不改 CurrentStore 门禁、不新增生产行情副本。正式 D04 有序映射仍开放。
示例 `end_date` 与 RunConfig.end 对齐，终点不再下需下一会话的 DAY 单，不强平末仓。
首轮隔离 CI 的 D14 测试目录过长被 D12 Unix socket 检查拒绝；已缩短私有测试根，
不放宽路径保护或隔离要求，最终准确 head 重验。

迁移文档只推荐新策略副本 + append-only 修订，旧 draft/revision/账户/历史值与关联保留。
旧占位 transport 已替换；旧核心物理清理由 D17，未复制/导入 fallback。当前 D13
retry-failed 仅重试 failed；cancelled 取配置新键 submit，文档与测试如实保留该实际行为。

未验收：正式 D04/D05 身份、日历、日期规则、研究/行情映射，D17 清退、D18 全量 B1–B4
和生产 runner 拓扑、P01/C01。合成单位1/T0/最低费1/参与率1/滑点0/合成周末会话均具名，
不作为生产行情、法规费用、实盘成交或聚宽零修改兼容证明。CI 最终 head 与合并后准确 main
必须全部必要检查通过；D14 完成后停工，D15 由父线程另行派发。

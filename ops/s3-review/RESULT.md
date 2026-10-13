# S2/S3 review 修复状态

基线：最新 `main` `50db997f8f50b955a6a5fe0ce237543d33b5f60b`。本会话未开开发子会话，未部署、访问生产、调用行情供应商、改凭据/权限、迁移或清理生产数据；R01、S2 批次和旧核心退役边界保留。

## 已实现

- **SR-03**：`d47f308ab34cc6d4f8cefbf8796ee9d5f5ed387f`。真实 PostgreSQL/Parquet/Rust 反例先复现 B 仅 H2；修复按各标的实际完整 Bar 倒序补取，保留授权起点/午休/完整性/质量/取消门禁。累计文件、字节、桶数、时间及缓存不足抛 `RESOURCE_LIMIT`，不缓存或发布短历史伪成功。真实历史耗尽保持短窗口，无前填/补零。
- **SR-02 装配部分**：`995c5a74dc636adf6bd9e1e7532dd04b711d0b8d`。`AcceptedRunPlan` 携带有界研究 bindings 和参考指数集合；普通 `FormalProvider.binding` 派生 worker routes，D05 dispatch 与日线/降采样 reader 共用一个 CurrentStore、依赖 context、小事实检查和最终事务。参考指数日线走原 D05 研究价格口径，保留 `index_points`，不进入股票成交来源；SDK 不把它送往 CNY/份额 market route。
- 受影响完整规模重测：`daf479c784519b75fe2c6dcc685bdf476c46e5c3`。本修复分支的 CI 只重跑 **完整 B2 / 60m**；明确记录 `full_B1_B4=false`，不进入完整来源复用缓存。此前 B1–B4 证据保留；其他分支原全套策略不变。

## 本地证据及限制

Python 3.12.2、锁定 Rust 1.90.0、本地独立安装的真实 wheel，真实隔离 PostgreSQL 和 CurrentStore/Parquet/IPC/Rust/SDK：

- SR-03 gateway/IPC/SDK 用例 20 通过，覆盖 H1/H2/H3、多个不同缺口、授权起点、短历史、累计读取预算、取消和质量变化；原有 5/15/30/60m、跨日、完整性与实际回调回归保留。
- 相关当前存储/研究/正式节点/降采样/就绪检查合计 139 项完成。最初 11 项缺独立 Rust consumer，编译后对应 11 项重跑通过；其余 128 项已通过。参考指数 SDK 分支补充后，对应研究/装配/降采样 58 项重跑通过，另新增组合降采样与研究用例。
- 独立 consumer 中 SDK 88 项通过，根测试 29 项通过。
- 这些本地 CurrentStore 测试使用既有、仅测试启用的文件系统 probe；宿主与 Docker 卷真实类型均为 **overlay**。无 probe 的实际 worker 套件在 CurrentStore 初始化处按原门禁 `UNSUPPORTED_FILESYSTEM` 拒绝，不算受支持文件系统通过。真实受限 worker、无 probe PG/Parquet 和完整 B2/60m 结果以精确提交 CI 为准。
- 研究正例使用明确的合成 accepted-fact DTO，测试通过普通 FormalProvider 派生 routes，没有手工构造 `Binding(research_routes=...)`。验证五个研究函数、复权、参考指数价格、缺公开时间/锚点/集合完整性、未来查询/越权、质量变化与最终提交拒绝。这仍不是默认解析器的产品验收。

## 未通过条件

**SR-01 未修复，SR-02 默认成功链未关闭。** main 的默认 API/`create_services` 仍未装配正式 `plan_resolver`；`FormalProvider.capabilities` 固定 False 的问题仍存在。不能把这两点写成仅 `data_ready=false`，也不能用显式注入的隔离计划宣称默认 API→runner→受限 worker 成功。

已查 `backtest_service`、CurrentStore registry、既有身份/规则/日历/状态/公司行动事实及 D13/D18 文档，未发现原量到份额/raw 基础及研究可靠公开时间/因子锚点的既定权威接入口。日历 capability 的领域契约仅三类交易状态，不能扩作通用准入表。已请求父方提供已有权威入口；个人上下文检索不可用。本 PR 未另造批准记录/来源服务，未把请求、参数或账户 metadata 当成接受事实。

默认解析器需接入该既定事实源后，再经默认 `create_app/create_services` 逐类缺事实拒绝、预检/提交/runner/受限 worker/结果及中途事实变化验收。当前草稿 PR **不得因本结果文件而宣称三项全部完成或直接合并**；留父会话核验。

路径交集：仅 S3 `backtest_service`、SDK 数据路由及其隔离测试/构建/CI；未改 R01 的 `data_store` 实现、身份/日历事实模型、迁移、生产执行脚本和状态。

CI / PR：待精确 head 核验后回报。

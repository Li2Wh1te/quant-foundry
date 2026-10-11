# S3-D13 运行服务与历史读取

从 `6248e5aa473410075c142a1e298bceaacaded6a8` 接续 D12。只交付 D13，不部署、不调用供应商、不改正式数据或来源、不清退历史数据。

统一根 `/api/admin/backtests`：capabilities、preflight、提交、列表/状态、取消、results/{kind}、batches（状态/取消/retry-failed）和 compare。只接受显式 `qf.backtest.v2`；认证后的旧创建/内部执行返回 410，未知新版本 422。金额使用十进制字符串，参数复用 D01 原生校验。OpenAPI 使用仓库共享 schema 的无损文档投影，不设第二份校验规则。

三个新增持久对象是 `s3_runs`、`s3_run_batches`、`s3_run_results`。受理固定策略修订文本和账户版本/费用小配置；不保存行情或逐事件成功记录。所有者与幂等键共同限定请求。D12 claim/token/fence/租约控制认领；D11 先封存必需分片，D04 最终依赖检查的同一个数据库事务写唯一结果引用和终态。旧 claim、取消和成功竞争只能产生一个权威终态。

账户、费用目录和原历史读模型/路由迁入 `backtest_service/accounts` 与 `history`，保留原表/主键/值/关系/权限。旧模块仅有共享功能的导入别名，旧计算代码留给 D17 完成物理退出。身份/规则的公共标量校验导入移到静态共享模块；API、模型发现、策略管理和 runner 启动无需导入旧计算。历史只返回原保存结果，原始输入说明仍保留，不重算。

`create_services` 使用同一数据库仓储、D12 可信工作根映射/固定镜像和 D04 provider。正式 provider 读取当前 D04 能力事实并明确拒绝尚未补齐的标识、单位、会话、规则/费用映射。没有合成 fallback、环境插件或客户端 Docker 参数；D13 私有测试显式注入合成事实，但走真实 PostgreSQL/CurrentStore/HTTP/原生 wheel/兄弟 worker。

运行测试：`QF_S3_D13_REQUIRED=1` 与私有 test PostgreSQL、已安装 wheel，运行 `backend/tests/test_s3_run_service.py`。实际 runner 容器测试通过 `scripts/test_s3_runtime.sh`：它创建无网络私有 PG，仅可信 runner 得到其 Unix socket；worker 仍只获得本 run 的代码/IPC。脚本保留原 D12 全套隔离检查并强制 D13 worker 用例，跳过即失败。

D14 消费同一协议。当前实际 SDK `RunConfig` 已用于 HTTP 回归；BacktestClient 传输/CLI 的完整交付仍属 D14。真实业务映射、全量 D18 B1–B4 和生产拓扑/上线验收仍开放。下一包由父线程读取 D14 后派发。

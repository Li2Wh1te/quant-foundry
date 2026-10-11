# S3-D13 结果

实现统一 v2 服务和三个小型持久对象；共享账户/费用/策略关联/历史读取切断旧计算导入。新增迁移仅新增 S3 对象和自身约束，保护原历史表及被引用账户，不改 R01 数据/来源。

实际本地验证：完整 `scripts/test_s3_engine.sh` 的格式、clippy、Rust workspace、独立 public contract 和安装 wheel 回归通过；空库 `alembic upgrade head` 通过；`QF_S3_D13_REQUIRED=1 python -m pytest -q backend/tests/test_s3_run_service.py` 使用私有 PostgreSQL/原生 wheel，验证旧样本升级、HTTP/幂等/数值/权限、冻结费用、claim 重启、取消与最终提交；受影响历史/账户/策略回归通过。

完整 backend 已执行。开发宿主有三项环境失败：两项 CurrentStore 直接挂载检查拒绝 overlay，另一个既有策略进程测试遇到 PID 1 不回收孙进程。相同命令在未修改的 `6248e5a` 工作树重现三项，未调整 S2/数据底座或降低正式检查。GitHub Validate 和 S3 Rust contracts 在本 PR 已通过，最终准确 head 的结果、日志和关联提交见 [PR #171 checks](https://github.com/Li2Wh1te/quant-foundry/pull/171/checks)。

`scripts/test_s3_runtime.sh` 强制执行原 D12 隔离族和 D13 的实际 runner → 兄弟 worker 测试，跳过即失败，证据保留在 `s3-worker-isolation` artifact。实际 HTTP/SDK RunConfig、私有数据库、网关、Rust 撮合与 D11 落盘链路已跑通；真实成交核对提交时费用 `1.00`，排队后更新账户为 `9` 不改变已受理运行。本地 Docker vfs 构建空间不足的复跑不计为通过；完整隔离验收以最终 head 的此项 CI 为准。合并前要求全部必要 CI 通过，合并后另核验准确 main 提交。

第二遍复核修复：保留旧费用目录的原 64 位十六进制摘要；区分用户提交键与内部批次/重跑键；允许已归一化参考日历复用同一请求；Unix socket 数据库 URL 在 Alembic 序列化后仍指向私有端点；批次页同时限制条数和字节数，游标只跨过已返回项。迁移保留原链与业务数据，只新增三个 S3 对象。

隔离样本核对实际旧主键、账户版本、修订原文、精确金额与纳秒和关联；不以数量代替值验证。导入阻断检查 API/OpenAPI、共享模型和 runner 可在旧计算禁止导入时启动。开发容器 CurrentStore 机制测试使用原有 overlay probe 注入，不能当作生产文件系统或生产验收；GitHub CI 在真实支持挂载上无此注入。

保留理由：旧计算物理清理由 D17；共享旧路径只保留导入别名，历史模型只读，历史 Alembic 链不改。正常新运行只写 D11 结果分片和本 run 序号，不写旧快照/事件表。

未完成的整体验收：正式身份/日历/规则与数据映射，D14 完整提交客户端/CLI，D18 全量 B1–B4 和生产拓扑、P01 上线、C01 生产清退。未访问生产、未部署、未改真实数据库、未删除用户历史。D13 完成后停工。

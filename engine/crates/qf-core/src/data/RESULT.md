# S3-D04 RESULT

实现完成；真实隔离 CurrentStore → Arrow IPC → 独立 Rust → D03 StreamingMerge 验证通过。
正式生产联调未做，未部署、未合并、未请求供应商、未访问内网或执行 R01 reset。

基线：main ce15a6d1b6ec8e923e677f672c2ea8585526edb7。
独立分支：codex/s3-d04-currentstore-arrow。实际接缝见同目录 README.md。

- 父进程网关核对认证 owner/run/universe/时间/字段，复用 require_ready、遗留限制、
  CurrentStore 质量准入、generation、查询预算与短锁。新增依赖前、读前/读后、
  缓存命中及成功前复核。质量问题内容变化不依赖 generation 增长。
- 原读取只增加 Arrow 投影和结构化时间/反向分页；无逐事件 JSON/normalizer、
  无原生表 fallback，无市场文件副本。缓存有行/字节界限；IPC count 查询也只读增量。
- 唯一 _transport.py 与 Rust 对应实现维护有界控制/Arrow、容量信用、取消和断管。
  断管检测进入 CurrentStore 的实际取消回调，读取中和传输中都释放锁/owner。
- Rust 拒绝长度/节点/行数/压缩/布局异常，精确解析价格与 ns/序号，保留双边量；
  ArrowEventSource 实现 D03 EventSource，错误终态、批量大小不改变合法事件身份。
- finalize(context, commit(connection)) 提供短锁/数据库事务中的最终状态校验，
  D13 需在该连接内完成权威结果/终态提交，不能拆成检查后另行成功提交。

实际验证：

1. scripts/test_s3_engine.sh：rustfmt、clippy -D warnings、workspace Rust 测试、
   公共合同/版本检查、独立 Python 3.12 wheel 构建和安装测试全部通过。
2. cargo build -p qf-core --locked --example consume_gateway；隔离 PostgreSQL 下
   python -m pytest -q tests/test_s3_data_gateway.py：29 passed。
   包括真实既有 B05/B05-M 节点投影、成交/报价/1m/60m、混合存储频率选择、
   同 ns 合法序号、精确 Decimal、三种批次大小、跨块变化、质量不变 generation、
   新依赖、最终事务协调、维护 503/限制/empty/missing、增量 lookback、
   实际 IPC 背压/取消/断管/读取中断管与 Arrow 释放。Rust 消费缺席在 S3 CI 明确失败。
3. 同一隔离 PostgreSQL 运行 CurrentStore kernel/local_pipeline/API 与 D04 回归：
   103 passed（当时 D04 为 25 项；后续新增的通道窗口/精度/断管回归另随最终 D04 执行）。
   原 JSON/分页和底座消费保持兼容。
4. 本地为 overlay，使用已有 LF_D01_ALLOW_OVERLAY_TEST=1 测试 probe；
   实际文件、PostgreSQL、DuckDB、flock 与 IPC 均运行，不能冒充受支持生产挂载验收。
   S3 CI 不注入 probe，原 Current store isolated acceptance 继续负责支持挂载检查。

保留边界：

- E50/E51/E52/E70 的 raw 成交口径/单位缺口继续 CAPABILITY_UNAVAILABLE，不猜测；
  当前生产 D94 固定范围验收不等于所有频率、全域或本模块生产联调通过。
- D05/D10 接公开策略 Visibility/完整 Tick 键裁剪；D12 接容器、私有端点认证及资源；
  D13 接 finalize 的真实 run/claim 事务。本模块不声称完整 Rust 回测产品已经上线。
- D02 未取得官方依据的历史规则/费用继续 RULE_UNAVAILABLE。本模块未修改其资料。
- 旧回测数据适配仍有旧入口消费者，未提前删共享/旧运行代码；D12/D13/D17/D18 完成
  调用退出后清退。策略、账户、历史结果与正式数据未动；未编辑 S2 UI。
- 草稿 PR 和最终精确 head 的 CI 链接由本次交付回传，不增加版本/审批台账。

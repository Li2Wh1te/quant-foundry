# S3-D11 RESULT

实现起点 main `e2c1f0f0c77ac5d28cbc27d88de79bd57bec72ac`；合入前接续
S2 已合并的 main `7f18987a86e6311b3367f4d7be6da61406454b80`，无文件冲突。
任务终点为 D11 README，不启动 D12。

## 交付

- 流式 Rust 日终分析和可选 Bar 曲线：净值、收益、回撤、ddof=1 波动、Sharpe、
  同区间基准/累计超额、换手、费用和成交/订单状态统计。
  [公式与 NA 规则](../analysis/README.md)公开日历、年化、无风险收益与精度；
  缺估值日保留空点并断开相邻收益，不拼接。精确金额不变成统计浮点。
- D09 `Account` 仍是唯一账务权威；只增加其总计的序列化和只读转发。
  D05 `analysis/indicators.rs` 未改动、未复制。
- `qf.results.v1` 有界文件分片、SHA256 摘要、小 manifest、活动/保留历史分页、
  必需分片全部同步提交后才成功。文件写满、错误、取消、依赖变化不能假成功；
  日志限字节/条数且截断可见。没有市场事件/行情快照或逐 Tick 成功记录类型。
  [格式、容量与接续边界](README.md)统一供 D12/D13 使用。
- D10 索引 trade 查询改为同一个 FileResultSink 薄适配，不再维护第二套格式。
  已安装 SDK `RunResults.summary/page` 单页有界、不可变、金额 Decimal/ns 整数；
  不缓存全部历史、不暴露分片路径、密钥或 DSN。HTTP 传输仍由 D14 实现。

## 已运行验证

Rust 1.90.0、Python 3.12.2，原生 Linux 隔离夹具，未访问生产或供应商。

- `QF_S3_PYTHON=backend/.venv/bin/python bash scripts/test_s3_engine.sh`：
  fmt/clippy、workspace Rust 241 项、公共核心契约、生成 DTO、仓库 Python 18 项、
  release wheel 构建/独立安装和 SDK 66 项通过。
- `cargo test -p qf-core --locked --test analysis_results`：20 项通过。
  收益 `[0.1,-0.2,0.2]` 的样本方差手算 `13/300`，回撤 `0.2`；
  真 D09/D10 买 100×10+1、卖 40×12−1，权益 10198、费用 2、总成交额 1480、
  剩余成本 600.6、已实现 78.6。空/单点/零波动/缺估值/无及错位基准/
  循环小数、分钟/Tick 年化不随事件数改变均有独立预期。
- 对抗复核补强并验证：分片软/硬链接、目录替换、已封存文件损坏/权限变化、
  最终 manifest 替换/已有目标、提交时取消、信用阻塞/断流、记录预算、空日志洪泛、
  SDK 用户 record 名与时间字段重名；接近全损/无风险收益 -1 时先保留精确正
  增长量再取对数，避免 float 1+r 变零。公式常规浮点断言误差 <=1e-11；
  `1e-20` 微小精确权益变化的收益断言误差 <1e-30。复核为本次独立手算和第二遍
  源码/对抗检查，没有声称额外人员或子代理审批。
- 受保护账户/策略/历史读取六个 backend 模块：78 passed。
  本地 CurrentStore 联调 8 passed/1 skipped/44 setup errors：本环境 tmpfs/overlayfs 被
  `UNSUPPORTED_FILESYSTEM` 正确拒绝，未绕过门禁。真实网关回归和完整 Validate
  由此 PR 精确最终 head 的 GitHub Actions 在受支持文件系统执行。

流式资源命令：`cargo build --locked --release -p qf-core --example result_stream`
（在 engine 目录），然后 `python scripts/check_s3_results.py --events 10000000`。
[实际自动输出](evidence/resources.json)包含 100 万/1000 万生成事件、子进程峰值
RSS/耗时/文件字节、4 CPU affinity、128 MiB 地址空间限制与真实 RLIMIT_FSIZE
1024 字节写失败。输入只保留一个事件；1000 万写出 10,000 笔合成业务成交、
501 个权益点及 summary。结果文件位于私有临时目录，关闭后释放。
此为 **D11 模块资源测试**，不含网关、D09 记账、Python 回调、DB、supervisor
或 Docker；不代表完整产品 B1–B4、4CPU/8GiB 进程组或生产验收。

## 保留与未验收

D04/D05 正式行情/研究/基准映射仍开放；流内不存在或边界不齐的 benchmark
保持缺失。D12 真进程隔离、IPC 关闭/信用及 claim/cancel 防陈旧提交，D13
数据库/API、鉴权和最终数据一致性事务须接续。本包的 `finalize_guarded` 保持
调用方 guard 穿过小文件提交；当前 wheel 的通道复核不冒充生产数据库事务。

旧 Python analyzers/equity_curve 仍被旧 runtime/历史结果层引用，未删除活依赖；
新 Rust/SDK 路径无旧 analyzer 调用或 fallback。D10 已被替代的重复 spool 实现
已删除。全局旧执行物理退役由 D12/D13/D17/D18 完成。
`new_engine_code_ready` 仅指 D11；`legacy_execution_removed`、`production_switched`、
`runtime_cleanup_done` 均未完成。没有部署、生产清理、来源开关或 CurrentStore 改动。

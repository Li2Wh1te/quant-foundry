# S3-D12 RESULT

从实际 main `44ddfaf887ba70f77fc1339a78e4db275dc5430c` 接续 D01–D11。
任务终点为 D12 README；不启动 D13/P01/C01，不改 S2 正式读取门禁。

## 实现与接续

- 现有 `python -m app.runner` 切到可信 S3 supervisor；每 run 一个独立兄弟容器。
  显式 runner/daemon 工作根映射，只挂本 attempt 的只读代码和私有 IPC；固定镜像 ID、
  命令和 UID 65532，network none、只读 root、cap-drop、no-new-privileges、seccomp、
  CPU/memory/swap/pids/tmp/file/墙钟/输出限额。策略没有 DB、密钥、源码目录或 Docker socket。
- 复用 D04 framing/真实网关 server、D10 Python→Rust 回调、D09 唯一 Account、
  D11 分片 sink/reader；信用有界、断流/取消打断，结果封存后才进入依赖最终检查。
  typed JSON 解码修复保留有限数值/未知字段拒绝，金额和纳秒不变成 float。
- `RunRepository` 和 `DataRunServices` 的
  [集成协议](README.md)已经实际接线。D13 实现 DB/API/历史仓储与 accepted-fact mapper，
  最终 claim/cancel/lease/结果指针提交须使用 `gateway.finalize` 提供的同一 connection。
  本包的有界 `LocalRunRepository` 是隔离集成/调度参考，不是第二套生产仓储。
  默认生产 composition 未接线时明确拒绝，不导回旧 runner 或恢复 Python checkpoint。
- 共享原子 claim/fence/心跳、一次权威终态；旧 claim 即刻拒绝，确认旧 worker 终止后
  从头重跑，不承诺恰好一次执行。停止/删除锁定已验证的容器 ID；路径清理按 inode
  拒绝替换/链接/未知对象。只回收自身 worker/code/tmp/IPC，完成和失败业务结果保留。
  批次至多 256 项、共享总预算/2 并发，失败项生成关联的新 run；成功项不重复执行。

## 实际验证

Rust 1.90.0 / Python 3.12.2，开发私有资源和正式 CI，没有生产或供应商访问。

- `QF_S3_PYTHON=backend/.venv/bin/python QF_S3_BUILD_ROOT=/tmp/qf-d12-build
  bash scripts/test_s3_engine.sh`：fmt/clippy、Rust workspace **234**、仓库 Python **18**、
  release wheel 独立安装和 SDK **68** 通过。包括实际 D11 spool→可信 writer→保留 reader、
  上游截断提示、精确金额/ns，以及伪数值对象/未知消息/非法终态拒绝。
- 本地一次性 PostgreSQL wrapper 下 `python -m pytest -q`：
  `test_s3_runtime_queue`、runner supervisor/process、gateway streams、account workspace、
  strategy storage、result API、pagination、account profiles/profile storage 共 **110 passed**。
  两个独立 spawn 进程竞争 claim；批次拒绝不部分受理、预算/所有权/取消/保留也实际回归。
- `QF_S3_BUILD_ROOT=... bash scripts/test_s3_runtime.sh` 在
  [正式隔离 CI](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38133671680)
  对代码 head `178bf50c78899abfcfcfdccb69b13733d6c0f3b3`：**12 passed，23.488s**，无 mock/skip。
  [实际证据](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38133671680/artifacts/11699819344)
  保存 runner→兄弟 worker 的不同路径映射、实际配置、cgroup 内存/pids/组预算采样和测试范围。
  16 参数/2 并发、故障项唯一重跑、其余成功结果原样保留；忙回调跨租约保持心跳，旧 claim
  迟到提交拒绝，取消/成功竞争唯一终态；网络/文件/密钥/socket/写 root、tmp/file/pids 配额、
  Python 异常、FFI segfault、OOM、死循环超时/取消、输出截断、非法 IPC/路径/超长头、
  DATA_CHANGED 最终拒绝全部通过。真实 D04 IPC/D09/D10/D11 买卖案例权益 **10198**、费用 **2**。
  停止 worker 后独立私有发布替身继续运行；这不声称实际生产 R01 已被测试。
- [既有 S3 CI](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38133178354)
  在复核代码 head `4c007e2eb5e5bf138e7c906134e1033488389497` 已通过：共享消费者 78、
  受支持文件系统 CurrentStore→Arrow→Rust/质量与释放 83、D12 queue 6。
  D11 千万事件模块资源回归继续执行，不冒称完整 B1–B4。

复核实际修复：严格 tagged JSON 数值解码、旧容器名替换竞态、创建后旧 claim 拒绝、
启动期间心跳、过期 attempt 并发回收、合法长回调的 IPC 等待、FFI EOF 终态分类和批次
拒绝的部分写入，以及终态已提交但活动索引未同步的崩溃后队列容量回收。
复核为第二遍源码/对抗检查，没有声称额外人员或子代理审批。
初版失败如实保留于 Actions；本地完整 Docker 回归被 vfs 构建磁盘满中断，未计为通过，
随后通过上述正式 CI 路线。仅清除本任务编译缓存/精确私有测试资源，未 general prune。

## 未验收与保留

正式 D04/D05 市场/研究映射、D13 DB/API/账户历史迁出、完整 B1–B4、P01 生产拓扑与
Docker 授权配置、生产运行和 C01 清退均未验收。隔离夹具是小型合成事实，不替代这些结论。
最终 head 和 merge 后 main 必须再核验 S3、Validate、新隔离工作流的精确提交结果。
旧 Python runner 的部署入口已退出；仍被旧 API/历史层引用的代码留给 D13/D17，未删活依赖。
`production_switched` / 生产 `runtime_cleanup_done` 均为 false。没有新增持久安全权限，
没有生产部署、删除、daemon/网络/凭据/来源开关或 CurrentStore 安全设置变更。

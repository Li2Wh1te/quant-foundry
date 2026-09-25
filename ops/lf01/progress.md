# LF-01 开发进度（开发分支，尚不可部署）

任务依据：用户提供的 `Quant_Foundry_Lightweight_Foundation_Rebuild_v1.0`。
开发基线：`1e0a7577c2c7f8c98f77fc4aac38751fd0befb23`，源码树
`9a0072c3abd79f2f6027edee0b7b096f14283a7e` 已与工作区完整比较。
任务包 17 项 SHA-256/字节长度检查通过，README、架构、实施、reset、
验收要求及附带参考均已阅读。随包只读扫描器扫描 754 个受控源文件，
识别 71 个旧映射入口（同花顺 57、Tushare 13、旧空范围 1）；没有连接生产。

## 已提交代码

1. 精确值及资源策略：`app.data_store.values`、`errors`、`limits`。
   Decimal 不经过 float 或当前 Decimal context 舍入；固定精度超限明确拒绝。
   纳秒身份使用 int64；控制 JSON 限制 UTF-8 字节、层数、节点数并拒绝非有限值。
   起始资源预算保持有限，暂存与 spill 共同计入，尚待 pipeline 接入测量与预留。
2. 单机锁：`app.data_store.locking`。每数据集写者互斥与读/提交锁分离；
   多集读取固定锁顺序和共同等待预算；支持取消、异常释放及进程退出释放。
   路径逐级禁止符号链接；拒绝不安全锁文件、被替换目录和未支持文件系统。
   fork 子进程关闭继承锁描述符，不解开父进程的锁；过期 guard 不能提交。

上述模块尚未注册到应用入口，不双写，不进行旧数据删除；第一批代码不是
新的完整底座，也不是生产切换版本。锁文件必须位于所有进程共享的可信本机
目录；不能删除或替换锁文件。跨容器部署的共享挂载仍需后续集成验证。

## 已运行测试

本地 Python 3.12.2、Arrow 23.0.1：新增三个测试文件合计 **184 个参数化用例通过**。
包括 38 位 Decimal/纳秒的显式 Arrow schema → 压缩 Parquet 往返、异常预算、
真实 Linux flock 竞争、独立进程 SIGKILL、fork、取消和锁文件安全检查。

本地测试文件系统是 overlay；锁行为测试仅替换文件系统名称探测结果，
内核 flock 与进程是真实执行。独立默认策略测试证明 overlay 被拒绝。
这不是 ext4/XFS/Btrfs 的落盘持久性测试，也不是跨容器/生产验收。
DuckDB/API 精度往返、PostgreSQL 提交及故障点测试尚未实现，不能标为通过。
分支 PR 的 CI 结果以 GitHub 实际运行记录为准。

## 剩余开发（最终合并前必须完成）

- 当前目录/schema/迁移、Parquet 分片、DuckDB 受限查询、事务提交和崩溃恢复。
- 真实资源测量/全局预留、日志轮转、异常与完成摘要保留、当前文件清理。
- 所有本地来源 reader/业务适配、重建/update/retry 与确定的覆盖/先后语义。
- 精确 reset plan/apply、唯一原件保全、历史迁移自包含及旧体系退役。
- 当前 API、最小前端/调度接入、旧入口移除及共享业务回归。
- 新只读 audit-export、C01–C18 整体行为测试、B01–B05 合成容量测试与部署说明。

## 状态和分支纪律

`code_ready=false`；`reset_applied=false`；`rebuild_complete=false`；
`domains_accepted=false`。后面三项的生产执行由维护者负责。

所有分批提交都留在同一开发分支 `codex/lf01-lightweight-foundation`。
中间不合并到 main；全部代码、整体测试与 CI 通过后才进行一次最终合并。
不得把旧 PR #113 算作 LF-01 完成；不为本任务创建自动生产部署。

# LF-D06 Current Store 存储瘦身

状态：实现与局部验收完成，完整 B05 和最终 CI 仍待完成。此状态不能作为 P01/R01 放行依据。

基线 `08c10fdc6109b169bb51ac096a5eb8b76141c42d`；代码提交 `d52e5455bae8386acb86f54aadf1f3ba744c60ed`。
分支 `codex/lf-d06-storage-slimming`。

## 实现与兼容边界

- 正式 Arrow/Parquet 删除 `value_hash`、`basis_valid`，`basis_state` 是唯一持久有效状态。
- `value_hash()` 与有界临时 SQLite `objects.h` 保留，用于相等、冲突和 no-op；结束后回收暂存状态。
- `row_layout=typed-object-nodes-v2` 改变 schema identity；业务规则仍为 `lfd02-v1`。
- v1 目录/文件通过既有 schema/descriptor/Arrow 校验返回 `REBUILD_REQUIRED`；即便提前注册 v2，旧文件仍被拒读。只能显式从保留本地原件 rebuild，不提供文件迁移器或双格式读取。
- 保留 `basis_ns`、`basis_group`、`basis_token`、精确 Decimal、ns identity、问题限制及撤回语义。
- 无数据库 schema 迁移，只有 Parquet row-layout v2；没有新增 per-row 数据库、sidecar、历史版本或配置键。

直接依赖检查：storage._pin_files、audit_export 的格式校验与 domain_reader/problem_samples 都由 DatasetSpec/声明字段驱动，无已删除列依赖；API fields 由 registry 生成，新增实际 HTTP 描述回归。前端不硬编码这两列，无页面改版。

## 正确性与集成验证

新增真实 Parquet 普通报告/Tick schema、API 字段描述、invalid/withdrawn qualified/debug、真实 v1 文件拒读/拒更新/显式重建回归。
原有三项 D02 回归全部保留：超过 256 分区跨进程续作、可变表同值 files_written=0 且 generation 不变、完整快照删除且失败/不完整不删除。

本地后端 2698 passed、1 skipped、236 subtests；根目录 17 passed、4 subtests；前端 85 passed；TypeScript、Vite build、完整 Alembic 链、Docker Parquet runtime、版本检查通过。
普通后端测试使用既有 overlay 探针；真实 ext4 跨容器另行验收。唯一 skip 为未配置可选 QF_TEST_POSTGRES_DSN 的既有测试。
初次运行的只读虚拟环境、root 所有者权限测试问题通过调整隔离测试环境解决，未放宽断言或资源门槛。

## 容量

100,000 Tick 使用原 benchmark_local_ticks.event/Range 生成器与原 ZSTD、Parquet 2.6、禁用 dictionary、page checksum 参数，包含热修正。业务字段/精度/行数不变。
旧基线 7,622,792 bytes，新正式文件 4,162,893 bytes（41.62893 bytes/tick），约 54.6%，通过 ≤60% 门槛。详见 [envelope](evidence/lf-d06-envelope.json)。合成样本比例不外推真实市场数据。

1000 自然分区：四次提交 256/256/256/232；首次读取/规范化 10,000，后续均为 0；1000 文件/10,000 行、已提交文件不变、临时 SQLite 已回收，用时 46.086 秒。详见 [natural-1000](evidence/lf-d06-natural-1000.json)。

完整 10M B05 正在固定代码和镜像上执行，最终通过状态待补齐。旧格式最终字节按已提交 lf-review-b05.json 重算：769,737,213 written − 7,632,227 replaced = 762,104,986 bytes。旧证据 files_read=files_retired=3；此固定全分区替换夹具每个被 commit 读取的文件都会被替换。新工具同时验证该恒等式、目录字节与实际文件大小一致，不以累计写入量冒充最终占用。门槛为旧最终字节的 65%。

新工具还比较热修正前后全部未触及文件路径/摘要，记录全进程 RSS 与含父级归并的 scratch 100ms 抽样峰值（抽样值不是连续绝对峰值）。原内核预算与容器 4 CPU/8 GiB 上限保持。

## 制品与生产边界

[artifact](evidence/lf-d06-artifact.json) 固定代码、镜像、架构、依赖锁及脚本摘要；[CI](evidence/lf-d06-ci.json) 记录验证状态。

`code_ready=false`（等待完整验收）；`production_deployed=false`；`P01_deploy=not_started`；`reset_applied=false`；`rebuild_complete=false`；`domains_accepted=false`。

本次只开发与隔离合成验收，不访问供应商，不执行生产部署、reset、rebuild 或生产清理。P01/R01 仍需后续独立执行，R01 从本地原件直接重建 v2。

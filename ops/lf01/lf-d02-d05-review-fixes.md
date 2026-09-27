# D02 / D05 main 复核修复

当前状态：`partial`，`code_ready=false`。完整 B05 和最终 PR CI 仍在运行/待执行。

## 修复

- LF-01：每个来源只扫描和规范化一次，完整扫描的归并结果保留于有预算的当前暂存槽；
  分区提交后删除工作行，跨调用和真实进程退出后继续剩余分区。正式目录 checkpoint
  防止提交与本地游标之间的崩溃窗口导致重复归并；共享 quota 原子替换，空闲续作也计费。
- LF-02：显式可变表相同业务值保留原 basis，重用未变化 checkpoint，正式文件和 generation 不空转。
- LF-03：完整、成功且无规范化失败的 NativeSources 可变表快照，仅删除对应来源表示缺失的键；
  整个分区清空也提交。未完成/异常扫描不会推断删除，历史/版本化事实和救回补充不套用此语义。
- A2：进度文件与 D05 状态改为当前真实状态，撤回旧 code_ready。

## 已执行验证

- PostgreSQL 三组反例加入常规测试。300 个自然月份分区：首次 256、重启后 44；
  总共读取 300 行、规范化 300 项；第二次为 0/0，前 256 个文件路径与摘要均不变。
- 专项及内核：68 passed（包括真实进程在目录提交后直接退出、恢复后零来源读取，以及空闲续作预算检查）。
- 最终后端全量：2693 passed、1 skipped、236 subtests passed；根目录 17 passed、4 subtests passed，版本检查通过。
  唯一 skip 为未配置可选 QF_TEST_POSTGRES_DSN 的既有迁移锁测试。
- 前端：85 passed；TypeScript 与生产构建通过，存在原有 bundle 大小提示。
- 全迁移链：在独立 quant_foundry_test PostgreSQL 数据库成功 upgrade head。

以上测试使用只读完整源码挂载、可写虚拟环境、独立 PostgreSQL；普通后端测试含
开发用 overlay 探针替换。首轮真实 ext4 跨容器、59 项内核/schema 测试和 B01–B04 共 11 组基准通过；
增加 pipeline 测试后的跨容器套件与完整 B05 仍在运行，结果待收齐后补录。

## 资源和行为边界

续作保留完整扫描中尚未提交的归并结果，受既有共享 scratch、spill、RSS 和时间预算约束。
源扫描在封存前失败需重新扫描；不声称能跨数据库事务继续未完成的 repeatable-read 快照。
封存后来源发生变化会由下一轮完整扫描处理。每个入口只保留一份当前工作状态，不保存历史。

本轮未连接供应商，未执行生产部署、停写、迁移、reset、全量重建或生产清理。


## A1：逐行 envelope 的实测构成

使用本轮 B05 的一个 100,000 行合成分片，保持 row group、ZSTD、Parquet 2.6、
禁用 dictionary、启用 page checksum 完全相同。复现工具为
[measure-tick-envelope.py](measure-tick-envelope.py)，原始结果为
[lf-review-tick-envelope.json](evidence/lf-review-tick-envelope.json)。

| 口径 | 压缩字节 | bytes/tick |
| --- | ---: | ---: |
| 当前完整 schema | 7,622,534 | 76.22534 |
| 去掉可重算 value_hash、basis_valid 的对照 | 4,167,158 | 41.67158 |
| 仅业务字段（不具备正式归并能力） | 413,269 | 4.13269 |

value_hash 占 3,418,039 压缩字节，basis_token 占 3,419,718 字节；本合成样本的业务值
重复度很高，所以不能把 4.13 bytes/tick 当作真实市场容量。value_hash 已由业务字段重算，
basis_valid 可从 basis_state 得到，两者合计可节省本样本约 45.3% 的文件字节。

建议后续显式 schema/rebuild 升级删除这两个冗余列。本轮保留现有正式 schema：直接删除
会使已有 current 文件不兼容，并要求全部受影响领域重建，超出三项扫描语义修复的兼容边界。
身份、对象顺序、来源 token 与质量状态仍用于当前键、冲突处理和合格读取，不能整组搬走或删掉。
此项已量化，格式优化尚未实施；没有新增 per-row provenance 表。

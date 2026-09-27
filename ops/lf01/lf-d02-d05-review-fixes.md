# D02 / D05 main 复核修复

当前状态：`code_ready=true`（隔离开发验收）。三个阻断项已修复，固定代码的两组 PR CI、
1000 自然分区和固定镜像完整 B05 均通过。最终代码提交为 `f131f1d23c6e1888ab726f22d0c24f54603499e7`；
PR 为 [#121](https://github.com/Li2Wh1te/quant-foundry/pull/121)。本报告的后续证据提交不改变存储运行代码。
生产部署、reset、真实重建和领域验收仍未执行。

## 修复

- LF-01：每个来源只扫描和规范化一次，完整扫描的归并结果保留于有预算的当前暂存槽；
  分区提交后删除工作行，跨调用和真实进程退出后继续剩余分区。正式目录 checkpoint
  防止提交与本地游标之间的崩溃窗口导致重复归并；共享 quota 原子替换，空闲续作也计费，并为提交保留至少一个写槽及对应预算，避免续作占满资源后无法排空。
  整批调用在现有入口状态中保留完成标记，未完成批次不重复扫描已完成入口；整批完成后下一轮重新获取来源。
- LF-02：显式可变表相同业务值保留原 basis，重用未变化 checkpoint，正式文件和 generation 不空转。
- LF-03：完整、成功且无规范化失败的 NativeSources 可变表快照，仅删除对应来源表示缺失的键；
  整个分区清空也提交。未完成/异常扫描不会推断删除，历史/版本化事实和救回补充不套用此语义。
- A2：进度文件与 D05 状态改为当前真实状态，以本轮复验恢复隔离 code_ready。

## 已执行验证

- PostgreSQL 三组反例加入常规测试。300 个自然月份分区：首次 256、重启后 44；
  总共读取 300 行、规范化 300 项；第二次为 0/0，前 256 个文件路径与摘要均不变。
- 专项及内核：69 passed；增加六入口公平轮转后，最终 pipeline 专项 22 passed。
  覆盖真实独立进程在目录提交后退出、恢复后零来源读取、空闲续作计费、写入容量预留及后续入口不饿死；六入口整批 complete 后下一轮再次每入口只扫描一次。
- 最终后端全量：2694 passed、1 skipped、236 subtests passed；根目录 17 passed、4 subtests passed，版本检查通过。
  唯一 skip 为未配置可选 QF_TEST_POSTGRES_DSN 的既有迁移锁测试。
- 前端：85 passed；TypeScript 与生产构建通过，存在原有 bundle 大小提示。
- 全迁移链：在独立 quant_foundry_test PostgreSQL 数据库成功 upgrade head。

以上测试使用只读完整源码挂载、可写虚拟环境、独立 PostgreSQL；普通后端测试含
开发用 overlay 探针替换。首轮真实 ext4 跨容器、59 项内核/schema 测试和 B01–B04 共 11 组基准通过；
存储核心提交 `de0eb40` 的两组 CI 通过；最终代码 `f131f1d` 也通过两组 CI。常规 CI：
2694 passed、1 skipped、236 subtests passed，前端 85 项及迁移、容器运行时检查通过。
de0eb40 的隔离验收首次在既有测试初始化阶段触发 100ms SQL 超时，同提交复跑通过。
最终 f131f1d 首次验收即通过：81 项测试、跨容器与 B01–B04 共 11 组基准全部成功，没有放宽测试阈值。
原始制品见 [cross-container](evidence/lf-review-cross-container.json)，
两组成功检查见 [code-ci](evidence/lf-review-code-ci.json)。

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
| 当前完整 schema | 7,622,792 | 76.22792 |
| 去掉可重算 value_hash、basis_valid 的对照 | 4,166,039 | 41.66039 |
| 仅业务字段（不具备正式归并能力） | 413,270 | 4.13270 |

value_hash 占 3,419,409 压缩字节，basis_token 占 3,418,570 字节；本合成样本的业务值
重复度很高，所以不能把 4.13 bytes/tick 当作真实市场容量。value_hash 已由业务字段重算，
basis_valid 可从 basis_state 得到，两者合计可节省本样本约 45.3% 的文件字节。

建议后续显式 schema/rebuild 升级删除这两个冗余列。本轮保留现有正式 schema：直接删除
会使已有 current 文件不兼容，并要求全部受影响领域重建，超出三项扫描语义修复的兼容边界。
身份、对象顺序、来源 token 与质量状态仍用于当前键、冲突处理和合格读取，不能整组搬走或删掉。
此项已量化，格式优化尚未实施；没有新增 per-row provenance 表。


## 1000 个自然分区完整来源基准

最终后端镜像 `qf-lf-review:batch-final` 在真实 ext4 与隔离 PostgreSQL 上通过 run_local 运行
[benchmark_local_snapshot.py](../../backend/scripts/benchmark_local_snapshot.py)。
真实本地日历表包含 1000 个月、每月 10 条数据；没有手工指定目标 partition。
每次调用都关闭并重新打开 CurrentStore，默认 256 pass。

| 调用 | 读取来源 | 规范化 | 提交分区 | complete |
| --- | ---: | ---: | ---: | --- |
| 1 | 10,000 | 10,000 | 256 | false |
| 2 | 0 | 0 | 256 | false |
| 3 | 0 | 0 | 256 | false |
| 4 | 0 | 0 | 232 | true |

用时 62.949 秒；最终 10,000 行、1000 个当前文件。已提交文件路径和摘要不变，
调用边界观测到的最大待续作文件合计 6,537,216 字节，进程 RSS 峰值 201,658,368 字节；
完成后续作 SQLite 已回收。边界观测值不是整个运行的绝对暂存峰值。
原始证据见 [natural-1000](evidence/lf-review-natural-1000.json)。


## 固定存储核心镜像完整 B05

后端镜像、代码树与依赖锁身份见 [artifact](evidence/lf-review-artifact.json)，
完整输出见 [B05](evidence/lf-review-b05.json)。使用固定镜像 `qf-lf-review:de0eb40`，
Linux arm64、真实 ext4、4 vCPU / 8 GiB 容器限额，未注入文件系统探针。
代码冻结前中止的预跑不计为通过；以下数据来自固定存储核心镜像的完整运行。

1000 万条 tick 全量写入用时 2622.070 秒，最终 10,000,010 行、108 个当前文件；111 个原子范围。热修正后 107 个非目标分区文件保持不变，大 Decimal 和纳秒时间戳/独立序号校验通过。

累计正式写入 769,737,213 字节，读取当前文件 7,632,227 字节，写入路径记录的 RSS 峰值 645,115,904 字节。B05 的 metrics 来自内核提交路径，暂存指标不是父级来源归并 SQLite 的全程峰值；来源工作文件仍由共享预算实时约束。

可复跑入口：`backend/scripts/benchmark_local_ticks.py --work-dir <隔离 ext4 目录> --output <新文件>`；
自然多分区入口：`backend/scripts/benchmark_local_snapshot.py --partitions 1000 --work-dir <隔离 ext4 目录> --output <新文件>`。
两个入口均要求显式 test 环境、本地隔离 PostgreSQL，源数据为合成夹具。

`code_ready=true` 不代表已经部署，亦不代表 R01 的真实全量业务验收。A1 的字段精简只完成量化，
尚未实施会改变正式 schema 的删列；此项不是本轮三个阻断修复的前置条件。


## 最终协调层与完整 B05 的对应关系

最终代码 f131f1d 相对完整 B05 的 de0eb40 仅修改 pipeline.py 的批次协调；merge、storage、schema、budget 和依赖锁保持相同，文件 SHA-256 见 artifact。完整 B05 证据据此保留，不声称最终镜像另跑了一次千万行。最终镜像补跑实际 run_local 的 1000 自然分区，以及 100,000 tick 的热修正/精度路径：最终 100,010 行，8 个非目标文件不变。补验原始结果见 [final-hot](evidence/lf-review-final-hot-correction.json)。六入口两轮整批完成由全量后端测试覆盖。

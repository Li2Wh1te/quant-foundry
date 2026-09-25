# LF-D01 结果

状态：**partial**。

代码实现及本环境可执行的本地回归、B01–B04合成负载已完成；**尚未推送、未执行本次远程CI、未合并**。
C12跨容器及受支持文件系统的实际验收未执行，因此不把本包标为完整通过。
本包通过也不等于整个LF-01达到`code_ready`；没有改变生产状态。

## 实际提交/PR与前序输入

仓库：`Li2Wh1te/quant-foundry`。前序实际输入为开发分支
`codex/lf01-lightweight-foundation`的`e09f41d093c665ea6ca34af05168864f79445f9f`，
源树`df1abe658ec8487b575cddaac162958e12878b01`。
已逐文件恢复源码并验证源树一致；复用前序精确数值、资源策略和锁模块，没有回退覆盖。
任务包`Quant_Foundry_LF-D01_v1.1`的10项文件字节数及SHA-256均核对通过，见
`evidence/lf-d01-package-validation.json`。

本地代码提交（**不是GitHub已发布提交**）：

```text
37096fdc23036e633d01784f2a302aa91e9f2f34 feat(data-store): add typed contracts and additive current catalog
05d1a43c6a689cb791ad8bc6071c286cf8954bf7 feat(data-store): implement bounded atomic Parquet writes and exact current reads
cb523fe8e1c7a02a20f66841a54920adc939adfb fix(data-store): bound Arrow decoding and preserve migration and quality gates
bbcd4358fb378b7c02148ea97d0c15cc0cf5ed5d test(data-store): add streamed capacity and isolated cross-container acceptance
```

随后追加文档及测试证据提交；交付包`delivery.json`记录最终本地提交、代码树和有序补丁清单。

本地Git根`88efb67d0b0612919101ba4bc467d27fbbe760ae`仅是已核验源码快照，
**不是远程Git祖先**，不得直接将它推送或强制覆盖远程分支。提供从该快照起的
`git format-patch`补丁，在真实`e09f41d...`开发分支上应用，保留原来的真实祖先关系。

最终只读核对：既有PR #114仍是草稿、未合并，远程head仍是`e09f41d...`；
`main`仍是`1e0a7577c2c7f8c98f77fc4aac38751fd0befb23`。
本地补丁没有出现在远程PR里，旧PR文字不能作为本次D01的新交付证据。

实际执行环境：**开发隔离**。UTC约2026-09-25 02:36开始；最后一轮功能测试与容量测试于03:25完成，
数据模块单独复验于03:28完成；本报告整理于03:31 UTC。

## 已完成

### 当前目录与类型化存储

`app.data_store.schema/catalog/tables/storage`提供真正可调用的当前存储：
PostgreSQL仅管理根绑定、数据集、当前文件、实际来源范围、未解决问题和有界清理意图。
没有每条正常业务行的成功台账、历史发布父链或可查询旧文件映射。
业务数据使用显式Arrow schema的压缩Parquet，DuckDB在进程内直接查询，不持久化第二套业务全库。

业务键支持确定分片、范围及跨批唯一性检查；Decimal最多38位，拒绝不能准确表示的精度，
纳秒身份保持int64。输出Decimal及超过JavaScript安全范围的整数使用无损字符串。
新增字符串字节上限，并据契约先限制原生Arrow读取批量，避免小输出限额掩盖巨型原生解码。
新增迁移`20261005_01`仅增加必要结构，迁移自包含；已有证据时拒绝破坏性降级。
保留历史迁移命名约定，旧回测分析迁移回归实际通过。

### 提交、读取与恢复

`storage/filesystem/readers`实现按数据集的写者锁和读/提交锁、唯一路径、写后校验/关闭/fsync、
先登记有限清理意图再持久化正式文件、短PostgreSQL事务仅替换受影响目录项。
来源CAS、可接入的来源确认谓词、问题、checkpoint与generation同事务提交。
确认丢失时在锁保护下查询当前提交标记；仍不可确认则返回`COMMIT_UNKNOWN`，不盲重放或删新文件。

只从目录中明确列出的文件读取，拒绝glob、任意SQL/路径及release/snapshot历史查询。
签名游标绑定查询及多依赖generation向量，变化返回`DATA_CHANGED`。
`read_many`持有固定顺序的所有依赖读锁，任何依赖失败不返回部分成功组合。
未解决问题使普通读取返回`DATA_RESTRICTED`；内部诊断读取明确携带restricted状态，不能冒充合格数据。

### 资源、问题与清理

`budget/maintenance`实施共享staging+spill预算、有限并发槽、磁盘余量、原生内存与线程上限、
RSS/超时/取消监测、批次/文件/查询行数与字节限制。
成功及失败仅记批次汇总，共享日志字节和年龄限额；没有新写入时也可显式执行年龄清理。
未解决问题按当前实际范围聚合；解除必须匹配具体问题及当前证据token，非相关变化不能自动解除。

旧文件退出后进入有界重试清理，只删除明确登记、已无当前引用且不在合法读写中的对象。
删除失败达到队列压力时背压，不以扩容或删业务原件绕过。
提交后scratch清理失败不能把已经成功的业务提交重新标为失败；下一次准入先处理遗留占用。
已完成共享任务摘要清理仅覆盖`data_store.update/rebuild/retry`的终态实例，30天且每任务最多1000条，
不影响其他task type、运行中任务和未解决问题。

### 内部接口和边界

后续D02/D03/D04可使用`CurrentStore`的register/capability/source_state、upsert、
replace_report、replace_partition、record_problems、read/read_many、recover和cleanup。
完整签名、原子单位、输入限制、幂等条件、确认谓词要求、错误和保留语义见`lf-d01-interfaces.md`。

D02仍负责真实来源顺序、完整性判断和领域转换；D03负责旧体系维护/重置；D04负责公共API及默认调度。
本包没有接入新线上入口、默认定时任务或新常驻服务，没有导入依赖旧发布模型。
当前目录零行不等于供应商已确认空范围或全部范围完成，后续调用方须核对来源状态。

## 检查结果

### 通过：本地开发隔离

| 检查 | 实际结果 | 证据 |
|---|---|---|
| 后端完整回归，真实PostgreSQL启用 | 3301 passed、1 skipped、234 subtests passed，277.29秒 | `evidence/lf-d01-backend-regression.txt` |
| 所有data_store测试单独复验 | 327 passed，25.57秒 | `evidence/lf-d01-data-store-tests.txt` |
| D01 kernel/schema及受影响旧迁移回归 | 67 passed、4 subtests passed | `evidence/lf-d01-migration-regression.txt` |
| 根目录运维/版本测试，普通非root用户 | 19 passed | `evidence/lf-d01-root-tests.txt` |
| 版本一致性检查 | 0.3.0一致 | `evidence/lf-d01-release-check.txt` |
| B01–B04合成负载 | 11项实际结果全部passed，complete=true | `evidence/lf-d01-benchmark.json` |

327项包括268项继承/扩展的精确值、限制及锁测试，以及59项新增的D01内核/schema用例；
3301项不是全部新增LF-D01用例。完整回归的1项原有跳过和6个非阻断警告在原始日志中保留。

| 原场景 | 本地实际检查 | 判定边界 |
|---|---|---|
| C01 | 100次同输入，业务文件/行数不增长，惰性输入不消费；成功日志仍受限 | 本地通过 |
| C09 | 38位Decimal、纳秒、时区与同时间多事件，真实Parquet→DuckDB→隔离HTTP JSON输出 | 本地通过；不是公共API已注册 |
| C11 | 签名游标、跨页generation及多依赖变化、历史参数拒绝 | 本地通过 |
| C12 | 真实内核flock、多进程退出/终止、活动读者与writer/cleanup互斥、不相关数据集仍可读 | **部分通过**；实际跨容器未执行 |
| C13 | 写入/fsync/提升/提交/删除等9个进程终止边界、提交确认丢失及不可确认、重复恢复、删除失败 | 机制测试通过；不代表真实断电持久性认证 |
| C15 | 实际磁盘余量/RSS阈值、DuckDB原生内存错误和interrupt、超时/输出/取消、删除背压 | 本地通过；不保证任意外部Python回调可强制中断 |
| C16 | 类型/单位/规则不兼容明确拒绝，nullable新增及受限局部重建，不隐式舍入 | 本地通过 |

### B01–B04原始指标摘要

| 原负载 | 规模/次数 | 秒 | 当前文件数 | 当次当前压缩文件读取 / 写入字节 |
|---|---:|---:|---:|---:|
| B01 | 100,000 | 2.246775 | 1 | 0 / 840,321 |
| B01 | 1,000,000 | 20.405517 | 4 | 0 / 8,269,553 |
| B02 | 1 | 0.011965 | 4 | 0 / 0 |
| B02 | 10 | 0.067034 | 4 | 0 / 0 |
| B02 | 100 | 0.565658 | 4 | 0 / 0 |
| B03 append | 10,000 | 0.233687 | 5 | 0 / 100,603 |
| B03 correct | 1,000 | 3.289976 | 5 | 2,166,749 / 2,192,918 |
| B01 | 10,000,000 | 204.825188 | 40 | 0 / 82,695,558 |
| B04 | 10 | 0.039125 | 11 | 4,235 / 4,511 |
| B04 | 100 | 0.045909 | 101 | 4,235 / 4,511 |
| B04 | 1,000 | 0.042536 | 1,001 | 4,235 / 4,511 |

B01的1000万行合成数据最终为40个当前文件，Parquet总计82,695,558字节，
实测进程RSS峰值454.30 MiB，单次原子范围staging+spill实测峰值14.51 MiB。
B02的1、10、100次无变化更新保持同一组4个当前文件及同一内容集合摘要，无业务文件读写。
B03追加只写1个新文件；0.1%纠错只读写1个受影响文件，未改变其他文件。
B04从10到1000个不相关背景分区，当次逻辑压缩文件读取均为4235字节、写入均为4511字节。

上述文件字节是**逻辑受影响文件大小**，不是全设备I/O；脚本另列客户端`/proc/self/io`增量，
不包含PostgreSQL服务进程I/O。B02不进入原生处理阶段，所以操作指标RSS采样为0，
不表示整个进程不占内存。原始JSON保留当前目录行数/关系大小、文件数/摘要、日志与scratch占用等。
这些是合成开发测量，不是生产数据正式发布或生产完成时间承诺。

### 未执行/仍阻断最终验收

**实际受支持文件系统与跨容器验收未执行。** 本环境只有overlayfs且无Docker CLI。
本地机制测试仅替换文件系统名称探测，真实flock、进程、PostgreSQL、Arrow、DuckDB和fsync调用均实际执行。
默认运行策略仍拒绝overlay；没有降低上线文件系统要求。不能把探测替换当成ext4/XFS/Btrfs的持久性通过。

已提供`ops/lf01/compose/`、`container_probe.py`及`scripts/check_current_store_containers.py`：
独立Compose项目、专用测试库、内部网络、不发布数据库端口、不读取生产.env；
真实双容器互斥、终止释放、读者保护、并行cleanup，并可复跑59项内核/schema与全部B负载。
本轮只完成该跨容器脚本的语法/YAML静态检查，**不能把尚未运行的测试脚本写成通过**。

新增`.github/workflows/current-store-acceptance.yml`是隔离验收CI，不是部署流程，权限仅contents:read。
移除了此前临时的`lf01-offline-workspace.yml`。新CI尚未推送/运行。
本次前端测试和构建也未执行；前端源码未改，但旧提交的92项通过结果不冒充本轮结果。

### 已修正的开发过程失败

首轮完整回归遇到隔离环境缺少uv子进程可发现的Python；在一次性测试运行时补齐准确解释器，
以已安装锁定依赖执行，未修改/跳过对应测试。随后旧迁移降级回归揭示metadata命名约定变化，
代码已修正并通过专门及全量回归。初版B04报告汇总触发控制JSON大小限额，
改为流式集合摘要后完整重跑，最终11项全部完成，没有把截断结果算作成功。

## 交付与后续输入

后续使用实际已应用并经CI验证的D01代码提交，不回退到旧参考main。
补丁应用脚本默认只检查状态，显式`--apply`才在精确匹配的开发分支执行git am；
不执行push/merge、数据库迁移、生产连接或删除。
先在支持Docker及本机ext4/XFS/Btrfs的隔离开发环境执行内部接口文档中的跨容器验收，
然后读取该实际提交的完整仓库CI结果；失败应修正后重跑，不应直接合并。

GitHub写入可用后，应将PR范围调整为本次LF-D01而不是整个LF-01，
确认最新main没有他人冲突、源码和测试提交一致，再按用户本次授权完成最终合并。
保留所有原始/共享业务数据；生产部署和后续事务仍由维护者执行。

## 未解决问题

1. 本轮GitHub连接仅暴露读取功能，没有提交/合并操作；运行环境也没有已认证GitHub CLI。
   已核对已安装集成，无可用写入替代。本地提交不能冒充GitHub提交。该限制阻断推送、远程CI和合并。
2. 本地缺少真实跨容器及受支持文件系统环境，C12完整验收未完成。
   已提供隔离执行工具，但必须实际运行成功才可关闭此项；不需要生产权限。
3. 本次尚未进行新的前端及远程完整CI复验。不能仅凭本地后端回归把合并门禁视为通过。

## 权限自述

| 事项 | 实际行为 |
|---|---|
| 生产访问、内网连接、供应商请求 | 未执行 |
| 生产部署、停写、调度修改、业务写入 | 未执行 |
| 生产reset、rebuild、迁移或清理 | 未执行 |
| 原始/共享业务数据删除 | 未执行 |
| 开发数据库 | 仅127.0.0.1:55432隔离`quant_foundry_test`，实际建表/迁移/故障测试；清理仅本测试创建的随机schema/数据库 |
| 开发文件 | 仅本工作区和隔离临时目录；真实写入/替换/删除均为合成测试文件 |
| GitHub | 仅读取既有分支/PR/制品；本轮无远程写入、无新合并 |

`lf_d01_acceptance_complete=false`；`lf01_code_ready=false`；`reset_applied=false`；
`rebuild_complete=false`；`domains_accepted=false`。

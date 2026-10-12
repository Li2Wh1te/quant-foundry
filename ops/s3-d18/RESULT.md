# S3-D18 集中验收与发行结果

**完整 B1–B4 和业务 head CI 已通过；文档最终 head/合并 main 待核。**
[PR176](https://github.com/Li2Wh1te/quant-foundry/pull/176) 仍为 draft。
完整测量源为 `55cbb9e453b523327e733c44e25e9d126478087b`，
[run38234261058](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38234261058)
于 2026-10-12 14:21 UTC 成功结束。最终测试修正 head
`0f0326a32e0dda7298329d13e3b26f5ecb1af723` 仅改两份独立测试文件；业务、测量与 native
源码逐文件一致，0f 精确 head 六个 workflow/八个 job 全部通过。文档最终 head 与合并 main
仍待核，尚未合并，也未执行 P01/C01。

从 D17 main `5a14110668f240f183bf3e6a08f638fc4e3fc02f` 接续，已整合实际 main
`81ad436a71d1e2871a7bff7266748aaab5b718d5`（含 S2 PR177、PR178 终态持久化修复）。
原 v1.1 全包、D01–D17 RESULT/相关 CI 已阅读，无适用 AGENTS/skills。
未访问生产/供应商、改 R01 数据或来源开关、混入未发布 S2/R01 E40 候选。

## 完整固定负载

实际路径为 CurrentStore → 有界 Arrow IPC → 真实 runner 容器发起兄弟 worker →
Rust/Python 策略 → PostgreSQL 队列与结果服务。没有每 run 行情副本或逐事件成功台账。

| 项 | 输入 / 实际引擎事件 / Python 行情回调 | 测量墙钟秒 | 结果字节 |
|---|---|---:|---:|
| B1 | 5,000×2,520 = 12,600,000 日线 / 同数 / 2,520 次真实 20 日横截面 | 3,154.17 | 4,857,007 |
| B2 1m | 200×252×240 = 12,096,000 分钟 / 同数 / 60,480 | 2,706.90 | 4,713,683 |
| B2 60m | 同一完整分钟源 / 201,600 / 1,008；生产 ResampledBarGateway 分桶 | 291.18 | 280,771 |
| B3 trade | 10,000,000 成交 Tick / 同数 / 同数真实逐 Tick | 1,452.06 | 185,621 |
| B3 quote | 10,000,000 报价 Tick / 同数 / 同数真实逐 Tick | 1,477.49 | 185,635 |
| B4 | 16×30×1,260；每项 37,800 事件、1,260 回调；最多两并发 | 2,736.64 | 49,432,327 |

全部必要结果写入和独立现金/费用核对通过。B1/B2/60m/两 Tick 的费用分别为
700/2,520/45/100/100 CNY（明示合成费用模型），不是省掉成本的循环。
对应订单/成交/权益/record 数分别为：B1 1,740/700/2,521/2,520；分钟
5,040/2,520/253/504；60m 90/45/253/256；两 Tick 各 200/100/2/1。
B4 共 30,859 订单、13,060 成交、20,176 权益点、20,160 record、49,787 日志。
B1/B4 按原数量上限保留所有目标尝试并记录合法拒单，没有提高规则上限。

B4 实际峰值两 worker；一项注入故障后只创建该项新 run，其他 15 项已保存结果逐值
保持，16 项最终核对与比较通过。两种 Tick 热修正探针各执行 100 次真实回调后
以 DATA_CHANGED 失败，partial 明确；修正输入验证后各运行完整千万条。
这些断言实际执行于上述成功完整 job，不引用旧局部数字代替。

[完整自动输出和可安装 wheel](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38234261058/artifacts/11753395787)：
3,223,834 字节，SHA256 `91fb1f7401025bf8705363b7b66ed08ddca9a95a4393080abd4c4e1c23f83069`。
含完整 `final-result.json`、制品和构建/启动证据。完整 job 日志与六项测量已保存至
本任务隔离证据目录 `ci/55cbb9e/`；本机代理拒绝 artifact 下载，未绕过代理/认证。
官方 artifact、精确 digest 和成功 job/来源门禁保留，不假称已在本机解压核对。

## 资源、阶段和存储

全组硬限制 **4 CPU/8 GiB**，含 runner/gateway 1 CPU/2 GiB、DB 1 CPU/1 GiB、
worker 合计 2 CPU/5 GiB；B4 每 worker 1 CPU/2.5 GiB。Python/NumPy 线程均为 1。
单 worker 原 3,600 秒、整组原运行预算未提高。B1 的 1 GiB 数据信用从原 5 GiB
worker 内分配，仍须不超过 worker 内存一半；Arrow 单批 64 MiB、控制消息 1 MiB。
四份窗口/转换信用从同一数据预算扣除，Rust 保守内存计费不减。

| 项 | 全组峰值 cgroup 字节 | 独立采样峰值 RSS 字节 | 夹具构建秒 | 唯一源字节 | 构建最大 tmp 字节 |
|---|---:|---:|---:|---:|---:|
| B1 | 793,034,752 | 606,543,872 | 752.46 | 30,011,482 | 1,480,641 |
| B2 1m | 947,699,712 | 657,465,344 | 1,203.65 | 50,592,153 | 5,974,862 |
| B2 60m | 883,666,944 | 545,820,672 | 共享上项 | 共享上项 | 共享上项 |
| B3 trade | 1,004,658,688 | 694,198,272 | 582.75 | 34,459,396 | 1,774,679 |
| B3 quote | 745,230,336 | 405,577,728 | 585.66 | 38,111,186 | 1,964,302 |
| B4 | 895,700,992 | 478,195,712 | 12.39 | 97,107 | 4,423,193 |

自动输出按 1 秒采样，保留成员/配额和启动、gateway、转换、Rust、Python、结果写入
及总体墙钟。下面阶段均有嵌套，不能相加；SDK 回调包含查询等待，Rust exclusive
扣除这些等待，gateway 的读包含其内部检查/转换。

| 项 | driver 秒 | SDK 回调秒 | IPC 秒 | 源 Arrow 转换秒 | Rust exclusive 秒 | worker 结果写秒 |
|---|---:|---:|---:|---:|---:|---:|
| B1 | 3,149.79 | 825.78 | 1,737.64 | 170.06 | 513.25 | 2.31 |
| B2 1m | 2,704.45 | 2,381.62 | 1,916.69 | 30.90 | 153.92 | 0.26 |
| B2 60m | 290.41 | 41.23 | 273.02 | 0.67 | 3.47 | 0.03 |
| B3 trade | 1,451.32 | 1,101.01 | 79.98 | 15.19 | 250.46 | 0.01 |
| B3 quote | 1,476.82 | 1,110.66 | 82.15 | 16.67 | 262.53 | 0.01 |

B1 gateway 1,263 物理读取、12,610,000 读取行、read 1,003.78/check 289.80/转换
22.19 秒；分钟 1,766 次、13,102,400 行、read 874.57/check 1,412.19/转换46.70秒。
60m gateway read151.58/check46.73/Rust 重采样及转换64.03秒，均包含嵌套。
B4 逐 run 阶段/启动与两并发组、单项重跑分别存于原自动输出，不用首项代替16项。

使用构建后共享 OS 暖缓存，无冷缓存成绩或宿主页缓存清空。source_bytes 为唯一
正式合成源，B2 两频率共享；selected_file_bytes 是重复选中文件的逻辑读取预算，
不是物理 SSD I/O。没有全历史事件对象入内存、未请求历史重复扫描或行情落盘缓存。
本轮旧/新诊断双方均为 Rust 产品源码；未提供退役 Python 的可比全量速度数字。

## 修复与 T01–T12

- 原 `3b61a3c` 完整 B1/B2 触及 wallcap 的失败和输出保留，不算通过。修复只读
  元数据深拷贝、同快照依赖 SQL、私有有界滚动窗口及 B1 两日存储分块；仍逐物理读
  前后/缓存发布/动态依赖/最终提交检查，保留时间裁剪、权限、质量与原批次上限。
  用户编辑返回表不能污染缓存；增量超限先关闭流再执行原 count；坏行数/schema
  不作正常回退。NULL 内部省节点，公开未知仍为 None，缺值降采样不造完整 Bar。
- 5,000 标的 IN 保留 10,000 项/1 MiB 及完整转义/游标摘要；多批窗口独立于
  单包信用。60m 执行与产品共用生产降采样，覆盖 5m/15m/30m/60m。
- 可选可信服务端额度接线保持默认 1 CPU/512 MiB/60 秒；客户端额度字段拒绝。
  HTTP→runner 实测已受理额度/费用不随后续修改，随后两名 1 CPU/2.5 GiB worker
  同时运行，既有成功结果不变。不同整组配置在队列未排空时拒绝，终态历史保留。
- 唯一 CI 失败是独立内存夹具仍按旧单份缓存信用准入，在行情加载前 RESOURCE_LIMIT。
  `0f0326a` 仅改测试：原总额度 1,218,445,312 字节不变，额外三份信用从 native view
  扣除，保留105,000输入/210,000回放、RSS/保守工作集/重置断言并报告stderr。
  本地复现 RSS618,192,896、工作集823,147,392≤view1,048,576,000、重置[0,0]，
  精确失败阶段126项本地与0f CI全部通过；后续检查也实际完成，上传无文件不是根因。

| 族 | 实际实现、独立反例与检查入口 |
|---|---|
| T01 | Rust numeric/fees/accounting；循环小数成本/最后释放、half-even、整数商余数、溢出；SDK Decimal/ns与完整现金/费用核对 |
| T02 | clock/bar/tick同刻屏障、合法同ns事件、批次稳定性；所有频率未来过滤，2日预取5,000标的仍只见已完成窗口 |
| T03 | orders/account目标幂等/原子替换、冻结/撤单/部分成交/DAY下一会话、限价、非递归通知 |
| T04 | dated market_rules/fees/dividend_tax，沪深北、ETF单位/回转、指数拒单和未知拒绝 |
| T05 | Bar代理时间/共享量独立案例；trade/quote共享额度、重复报价不补量、双边量；完整逐tick回调 |
| T06 | corporate与实际SDK分红登记/除权/支付、拆并/可卖日、GTC释放、配股政策/raw与复权区分 |
| T07 | 原生CurrentStore跨容器CI、E50/E70正式节点、权限/质量/generation/动态依赖/最终锁与非空问题摘要 |
| T08 | research_api/indicators知识/生效时点、缺失/预热、独立公式、缺能力拒绝及生产降采样 |
| T09 | 实际安装88项SDK，GIL/Arrow释放、固定表形/空表、分页/异常位置、五示例、窗口/元数据/坏头反例 |
| T10 | 精确55实际worker CI26项零跳过；秘密/网络/socket/目录、OOM/崩溃/超时/取消/陈旧claim/唯一终态、完整B4 |
| T11 | 同REST的Python/CLI/真实浏览器；配置/资本/成本、参数批次/单失败重跑/比较/分页/进度 |
| T12 | D17 103旧源路径物理缺席；空库/非空旧库迁移、十类非空历史关系逐列原值保护、29 cleanup拒绝与安装启动 |

精确55 CI：[Validate](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38234261051)
后端1396通过/19跳过/100子检查、根29、前端153/构建；
[实际worker](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38234261026)、
[浏览器](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38234261046)、
[原生CurrentStore](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38234261030)均通过。
[Rust综合](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38234261047)核心235/SDK88通过，
55内存夹具125通过/1失败曾阻断后续步骤；
[0f精确Rust/共享业务CI](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38262683046)
已实际补齐：账户/历史54、网关/研究/正式节点126、队列8、D13 7通过/1宿主依赖跳过、
D17 29、CLI 4通过/1宿主依赖跳过/6子检查。专门实际worker CI26项零跳过；
0f六workflow/八jobs均通过，并无未解决review thread。

## 制品、来源与运维

Linux x86_64/arm64均实际原生构建、独立安装wheel、离线镜像和空库API/runner启停。
[ARM artifact](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38234261058/artifacts/11740322181)：
3,074,952字节，SHA256 `5e13e3b365394360f828fc1a20263f7d216b6f4d6c9fe9beb7dfd5428bff8114`；
含native wheel与镜像/启动证明，未导出ARM镜像tar。受支持路径为使用该wheel、锁定依赖和
已验证Dockerfile构建离线镜像；不虚构双架构二进制相同。

本地干净55 x86镜像归档实际load核原ID：
`/tmp/s3-d18-release-images/55cbb9e-x86_64-images.tar.gz`，540,975,653字节，
SHA256 `16bf13dc544599ff5b12aa6ecf739a9a55c2568819a159a3dd7ab648a28208f1`。
backend `505ccccb38b1f247821c7c3a902d88a418a4d5af260907114925db00009083f6`，worker
`51f631140f89118548e9bcdd99be16bf2262ad64410be7bd8ebee588fbae6fad`。
原型/旧制品不混入最终源；只在核归档SHA/无引用后精确回收本任务可恢复缓存，
没有全局prune、共享卷或其他任务资料删除。

业务/测量源SHA `17012d12dc4c354ec5b3f88bcc0aa170f59f39ed34109ff427fa5a13abd9f187`；
native/SDK源SHA `6046f7fe97b8453f1cb28a287fc94defb88c73f5e19a87dabaa268b630fc8a28`。
本地native二进制SHA `64fc21e415c7ff2da4ba118e30db3a41ae1ab289734037fc6b692c1f9d55b07f`。
每个CI制品保留其自身native/wheel/架构/编译器摘要，不能以本地二进制值替代。
CI native二进制SHA `3d60385ec81ef6faba68fbca7cb669656465326ee5c4c89c8a23430d3bcbd01a`
与本地值不同，按各自编译制品记录。
[0f来源与二进制复用门禁](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38262683111/job/114856933078)
已实际通过，逐项核完整负载、回调、热修正/失败重跑、同业务源与同CI native二进制，
没有重跑全量。
[0f x86制品与reuse.json](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38262683111/artifacts/11753656565)
SHA256 `dc07e8b2c68b2f421622636b7a53e893d1c3d36f52d00e2dd5db4b4029cbafad`；
[0f原生ARM制品](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38262683111/artifacts/11753183267)
SHA256 `0c5470757d9e6da5d7f19418ac0b535c0324c17007f243c015e18a51b48f1ce1`。
测量/构建提交分别记录；算法/schema变化需相关重测，纯文档或不影响业务/测量源的测试
修正仅在来源与二进制门禁实测一致时复用完整成功证据，不拿失败/smoke/脏树复用。

构建、导入、正常迁移、启动、停止接单、取消、分页结果核对与精确cleanup命令、
当前依赖均见[README](README.md)，已在授权私有环境核验。生产部署所有者须先落实
已有UID/GID、daemon访问/宿主映射/结果根和接受事实；不以改权限/来源/R01 reset替代。

| 状态 | 当前结论与证据 |
|---|---|
| new_engine_code_ready | **业务代码验收通过，发行关单待文档最终head/mainCI**；固定完整B1–B4及0f全部必需CI通过 |
| legacy_execution_removed | **true（源码/发行物）**；103旧路径删除，安装/共享业务不依赖旧计算，无fallback |
| data_ready | **false**；默认正式事实解析器/接受事实和覆盖尚未发布，合成验证不代表生产数据就绪 |
| production_switched | **false**；P01未执行，未访问或部署生产 |
| runtime_cleanup_done | **false（旧生产残留）**；C01未执行；新worker容器/tmp/IPC日常自动回收另有证据 |

真实依赖为底座接受的E50 CNY/原量到整数份额、E70 raw依据/精确手转份、稳定历史
身份、日历、日期规则、状态、费用/公司行动和研究可靠知识/生效时间。AcceptedRunPlan/
FormalProvider/E50/E70端口已经实现并隔离验证，REST/account metadata不能注入执行事实。
E51未锚定前复权不成交、E52指数只研究、未发布E40不参与。缺事实拒绝，不填零、
查原生备用表或重建R01；未自行进入P01/C01。

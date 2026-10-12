# S3-D18 集中验收与发行结果

**尚未关单、未合并**：[PR176](https://github.com/Li2Wh1te/quant-foundry/pull/176) 为 draft。
当前精确 head `3b61a3cb402e855fbe6ed45d641dfb2850ecbb82` 的常规五 workflow/六 job
及原生 ARM64 job 已通过；[完整 B1–B4](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38196486524)
仍在运行。短夹具、旧源码成绩和单元测试数量均不作为全量通过证据。
可信资源接线修复已保存为本地 `8d8109a3b915961289ee8c10e268bddb9f56ffc3`，
尚未推送，远端在跑提交和预算保持原值。

从 D17 main `5a14110668f240f183bf3e6a08f638fc4e3fc02f` 接续，已整合随后实际 main
`dd7be607e106da06e927fecd165cae12a0b5b3bd`（已发布 S2 PR177）。原 v1.1 全包、
D01–D17 RESULT/相关 CI 已阅读，无适用 AGENTS/skills。未混入未发布 S2/R01 E40 候选。
本任务只写仓库和隔离合成环境，未访问生产/供应商，未改来源开关或 R01 数据。

## 完整负载和资源

运行路径为 CurrentStore → 有界 Arrow IPC → 真实 runner 容器发起兄弟 worker →
Rust/Python 策略 → PostgreSQL 队列与结果服务。B2 两频率共享唯一分钟源，60m 消费
生产 `ResampledBarGateway`，不是基准独有实现；没有每 run 行情副本或逐事件成功台账。

| 测量项 | 必需输入、真实回调及结果 | 当前完整结果 |
|---|---|---|
| B1 | 5,000×2,520 = 12,600,000 日线；2,520 次 Python 20 日横截面查询，基准、订单、费用与完整结果 | 待当前全量输出 |
| B2 1m | 200×252×240 = 12,096,000 分钟；60,480 次 Python 回调 | 待当前全量输出 |
| B2 60m | 同一完整分钟源；201,600 小时事件、1,008 次回调；午休/会话分桶 | 待当前全量输出 |
| B3 trade | 10,000,000 成交 Tick、同数逐 Tick Python 回调，成本/结果和修正探针 | 待当前全量输出 |
| B3 quote | 10,000,000 报价 Tick、同数逐 Tick Python 回调，双边保守额度和修正探针 | 待当前全量输出 |
| B4 | 16×30×1,260；最多两并发，故障项单独重跑，其他 15 项结果原值保留与比较 | 待当前全量输出 |

全组硬限制 **4 CPU/8 GiB**：runner/gateway 1 CPU/2 GiB、DB 1 CPU/1 GiB、worker
合计 2 CPU/5 GiB；B4 每 worker 1 CPU/2.5 GiB。Python/NumPy 线程均限制为 1。
单 worker 3,600 秒预算和输入/回调/成本/结果门禁保持，预算到期算未完成。
B1 在原 5 GiB worker 内分配 1 GiB 数据信用，仍受数据额度不超过 worker 内存一半
的原约束；Arrow 单批 64 MiB，控制消息 1 MiB。多批窗口与单包信用分别计费，
没有提高整组资源、外部配额或削弱 Rust 保守内存计费。
基准显式使用该受限资源配置；发布 API 的原默认 1 CPU/512 MiB、60 秒 worker 未扩大。
P01 还须提供与 AcceptedRunPlan 数据信用匹配的已批准服务配额，不能把测试配置
冒充默认生产受理能力。

自动输出 `final-result.json` 分开记录夹具构建、worker 启动、数据网关、转换、
Rust/回调、结果写入、接单/验收读取与整体墙钟，附 1 秒采样整组 RSS/cgroup、
各 worker 实际峰值、源/结果/tmp 字节及订单/成交/记录数。阶段包含嵌套调用，
不能相加当总墙钟；selected_file_bytes 是逻辑选中预算，不是物理 SSD I/O。
当前是构建后共享 OS 暖缓存，无冷缓存成绩或强制宿主页缓存清空。

## 已修复的真实集成缺口

- 5,000 标的 IN 请求被通用元数据节点数误拒：分块验证并保留完整转义字节/游标摘要，
  原 10,000 项/1 MiB 与通用门禁保持。SDK 的 64 MiB 单包信用又误用作整个多批窗口
  上限：现独立预算。真实 5,000×25 日诊断完成 125,000 事件/25 次回调、100,000 行
  20 日窗口、非零费用与资金核对；整组采样峰值 cgroup 967,450,624 字节，
  RSS 905,560,064 字节。该诊断明确 smoke/开发文件系统探针，**不是完整 B1**。
- 分钟路径重复检查/逐历史行裁剪慢：合并同一事务的小型限制查询，缓存不可变 schema，
  以 Arrow/NumPy 有界裁剪替代逐行 Python；每次物理读取前后、缓存发布、新依赖、
  取消和最终提交检查保留。原生小型正式节点、质量/变化/预算反例已验证。
- 60m 执行流原先只在基准类接线：生产和基准现共享 D05 Rust 降采样模块，覆盖
  5m/15m/30m/60m。缺分钟、价量或不足目标周期的末桶拒绝，不造完整 Bar。
  新增 13 个实际安装 wheel 的回归本地通过，待本轮全量结束后纳入必需 CI。
- B4 合成复利路径触发正确的单笔数量上限拒单：每次目标尝试/规则保留，仅精确
  该拒单记日志与计数，其他错误仍失败；未放宽数量、资金或结果上限。
- 真实进度采用每秒至多一次的当前计数/字符串纳秒，claim/租约保护数据库写入，
  取消、终态和旧 claim 拒绝迟到写入。Web 只读签名当前 runner 检查，无 Docker
  socket；缺失/过期/不同镜像/篡改/链接/FIFO/未知文件均拒绝，停止只撤销自身值。
- 发布 REST 原先不能选择基准已批准的可信资源配置：新增可选服务端 JSON 配置，
  默认 512 MiB/60 秒保持；超半内存数据信用受理前拒绝，客户端额度字段拒绝。
  本地实际 HTTP/SDK → runner → worker 已核保存配置与 Docker 限额一致。
  受理后改变可信默认配置仍按原额度启动；随后两项批次实际同时运行两名
  1 CPU/2.5 GiB worker，费用使用各自受理时配置，首个成功结果原值保留。
  当前队列未排空时不同整组配置仍拒绝；终态历史配置保留原值，不为配置切换删结果。
  配置/正式节点/降采样 47 项及 22 子检查、仓储/队列 15 项（宿主 worker 1 项跳过）、
  实际容器内 2 项通过；这些尚未进入当前 CI 的修复证据不冒充在跑 head 的 CI。

[首次完整失败及保留输出](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38174415508)
和后续旧源码失败仍可查阅，均不表示当前版本通过。当前全量保持原 head 与预算运行，
没有推送或取消；可信资源接线修复在本地等待该运行结束，最终证据必须核对新业务源。
另有私有回调插桩发现 Pandas 反复复制 attrs 元数据的热点；插桩/微测只用于诊断，
没有实施该性能原型或外推完整耗时。

## T01–T12 当前验证入口

| 族 | 真实实现与反例证据 |
|---|---|
| T01 数值 | Rust numeric/fees/accounting，循环小数成本与最后释放、half-even、整数数量、溢出拒绝；SDK Decimal/ns；独立现金/费用/权益核对 |
| T02 时序 | Rust clock/bar/tick 屏障、同纳秒不同序号、分页稳定性；SDK 未来过滤、默认/显式订阅与过期视图 |
| T03 订单 | Rust orders/account；目标幂等/原子替换、冻结/撤单/部分成交/DAY 下一会话、限价与非递归通知 |
| T04 规则 | dated market_rules/fees/dividend_tax，沪深北/ETF/指数拒单/未知规则拒绝 |
| T05 撮合 | Bar 独立手算案例；trade/quote 共享额度、重复同价同量不补量、双边数量与真实回调 |
| T06 公司行动 | accounting corporate 与真实 SDK 分红登记/除权/支付；拆并/可卖日期、GTC 释放与配股政策 |
| T07 数据 | 原生 CurrentStore CI；正式 E50/E70 节点与数据网关；权限/质量/generation/动态依赖/最终锁，非空问题集完整摘要 |
| T08 研究 | research_api/indicators 独立样例；知识/生效边界、缺失/预热与缺历史能力拒绝；实际生产降采样回归 |
| T09 SDK | 实际安装 wheel 83 项；真实 Python/Rust、GIL/Arrow 释放、固定表形/空表、分页、异常源码位置和示例 |
| T10 运行 | 实际 runner/worker 25 项、零跳过；秘密/网络/socket/目录隔离，崩溃/OOM/超时/取消/陈旧 claim/唯一终态；完整 B4 仍待输出 |
| T11 产品 | 同一 REST 的 Python/CLI/真实浏览器批次、失败重跑、比较、分页结果与实时进度；费用受理时固定 |
| T12 退役 | 103 旧源路径物理缺席；空库迁移/非空旧库升级、十类历史关系逐列原值保护、29 cleanup 拒绝与实际镜像启动 |

精确当前常规 CI：[Validate](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38196486389)
后端 1,359 通过/19 跳过/90 子检查、根 29 项及前端 153 项/构建；
[Rust/安装与共享业务](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38196486396)
Rust 235、SDK 83、网关/正式节点 111、迁移/历史/CLI；
[实际 worker](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38196486423)、
[真实浏览器](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38196486528)、
[原生 CurrentStore](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38196486382)均通过。
这些是各族回归入口，完整验收仍必须等待六个全量项，不以数量代替业务完成。

## 可安装制品与运维交付

Linux x86_64 本地干净 `8d8109a` 的 wheel/离线 backend/worker 镜像已独立安装、空库迁移、
API readyz、暂停接单、Web 无 Docker 权限及 runner 启停通过。镜像归档实际 load
核对原 ID：`/tmp/s3-d18-release-images/8d8109a-x86_64-images.tar.gz`，
508,218,139 字节，SHA256
`0d15f6f0cec71491bdde50efb2a49b435bca35fa377e4d66cc4badb284db3063`。
worker ID `55c4814…` 与 `3b61a3c` 完全一致，复用了已验证 native/SDK；
backend ID `57b6eb2…` 含可信配置修复。原生 ARM64 当前 CI 的 `3b61a3c` 构建、
独立 wheel 安装、离线镜像与空库启动/停止通过；本地后端修复的 ARM 制品尚待 CI。
[实际可安装制品](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38196486524/artifacts/11723568221)
SHA256 `b9c658e67174adf1aa96909389377fdda7fb09128aad2a71cfcaf3b63f591d0b`。
该 ARM 制品含 wheel/镜像与启动证明，未导出 ARM 镜像 tar；受支持路径为从该 wheel
及锁定依赖构建已验证的离线镜像。双架构二进制不宣称相同。

上述 `3b61a3c` 制品的 native/SDK 源摘要为
`1554673bfe4db10528c4663730f9877322ec594437bb9fa8489b8b4fcf8667af`；
该提交的业务/测量源摘要为
`1055ea53feb7bf3d57af026a651833f8b86361a3e5b2fd6046fb697ea6aff437`。
本地 `8d8109a` 业务/测量源摘要为
`e16ded1fe3a42ad1a702b854b9fd049951bb4b9f907742511e911f3df5b6a76d`，native/SDK 摘要不变。
每个制品保留自身构建/native 二进制摘要，当前测量提交另记，不借 wheel 提交冒充。
只有相同业务/native 摘要且六项完整成功才可复用纯文档后的证据，失败/smoke/脏树拒绝。
本地启动冒烟曾因磁盘满失败，保留原失败日志；核对本任务旧镜像归档 SHA 与无引用后，
仅精确回收旧镜像及三个无共享引用的构建缓存，重试启动通过。无全局 prune 或其他任务删除。

实际构建、导入、正常迁移、启动、停止接单、取消、逐页结果核对和 cleanup dry-run/apply
命令及当前依赖见 [README](README.md)。Compose 合并配置已在私有副本验证，未启动生产。

| 状态 | 当前结论及证据 |
|---|---|
| new_engine_code_ready | **false，D18 未完成**：当前完整 B1–B4 待输出，最终精确 head/合并 main CI 尚未完成 |
| legacy_execution_removed | **true（源码/发行镜像）**：D17 103 旧路径物理删除；新双架构实际安装启动无旧执行 fallback |
| data_ready | **false**：默认正式事实解析器尚未发布；合成及接受事实的隔离测试不表示生产覆盖 |
| production_switched | **false**：未访问或部署生产，P01 未执行 |
| runtime_cleanup_done | **false（旧生产残留）**：C01 未执行；新 worker 私有容器/tmp/IPC 日常自动回收另有证据 |

正式依赖为底座接受的 E50 CNY/原量到整数份额、E70 raw 依据/精确手转份、稳定历史
身份、日历、日期规则、状态、费用与公司行动，以及研究可靠知识/生效时间。
可信 `AcceptedRunPlan`/`FormalProvider` 端口和 E50/E70 原节点读取已经实际实现并隔离
验证，REST/账户 metadata 不能注入执行事实。E51 未锚定前复权不成交，E52 指数只作
研究；未发布 E40 不参与。缺事实明确拒绝，不填零、查原生备用表或重建 R01。

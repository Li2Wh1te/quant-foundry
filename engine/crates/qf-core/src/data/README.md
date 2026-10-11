# S3-D04 数据网关实施接缝

实际实现：可信父进程的 RunDataGateway、CurrentStore 同路径 Arrow 投影、
qf.market.v1 编码、qf.ipc.v1 私有通道、Rust BatchSource / ArrowEventSource。
不创建市场副本、永久快照或逐行成功记录。

## 可信父进程

backend/app/backtest_service/data_gateway.py 的 RunDataGateway 接收已认证
AuthenticatedPrincipal、受理阶段的 AuthorizedRun 和代码拥有的 MarketBinding。
owner、run、universe、允许历史起点和结束时点必须来自受理配置，不能取自 worker。
binding 的字段、业务键范围、真实时间列、单位/价格基础和列转换只由可信代码定义。

- open(scope, dependencies=...) 核对本 run 授权、require_ready 和已声明当前依赖。
- declare 在增加依赖前重新核对已有依赖；read 与 lookback 复用 CurrentStore。
- read(binding, request, context) 返回有界 DataBatch 迭代器。字段白名单、
  标的、实际 UTC ns 范围、查询预算、质量拒绝均在父进程执行。
- count 请求经 lookback 的有界本 run 缓存；相同窗口复用，向前推进只加载
  上次结束之后的范围。增大 count 或后退时间重新读取必要窗口。无落盘缓存。
  read 与 IPC 都使用这条缓存路径；超过行数/字节预算明确 RESOURCE_LIMIT。
  每页合并后立即检查缓存预算，空窗口也计入条目上限；相同窗口直接复用数组。
- check 校验 generation、schema/rule、质量问题内容、入口状态与维护准入。
  token 是当前一致性标识；按数据集保守中止，无历史恢复能力。问题指纹流式计算，
  有字节/数量/取消预算，不一次加载全部问题正文。
  已用依赖及新依赖在同一连接的 REPEATABLE READ 快照中捕获；检查有统一时限
  （最多 5 秒），各查询不能重置该时限。入口仅 overflow_restriction 进入质量指纹，
  refresh、计数等操作进度不引起 DATA_CHANGED。
- finalize(context, commit(connection)) 在短读锁及 PostgreSQL SHARE 表锁
  中检查依赖，并用同一个事务调用 D13 的权威终态提交。commit 只做小型数据库
  操作，不执行策略、网络或 Arrow 工作。不能替换成 check(); commit() 两步。
  最终事务使用 READ COMMITTED，在排除写者的锁取得后重新读取；不能把锁前旧
  快照沿用到提交。真实 CurrentStore 发布器及不持文件锁的质量/入口/维护写者
  均已纳入隔离竞争测试；回调越过统一时限也会回滚。
- close 清空依赖/缓存并失效 context；共享 CurrentStore 的寿命由调用者管理。

CurrentStore.read_arrow 是原 readers.read_many 的投影分支；使用相同的锁、
relevant_issue_count、generation、预算、DuckDB、文件 schema 检查与清理。
Query 只新增白名单列的结构化比较和反向分页，未新增 SQL/路径参数入口。
旧 Page/JSON API 及既有默认查询的签名游标保持兼容；新增排序/过滤请求各自绑定。
无文件范围也执行正式质量门禁，不能以空结果隐藏限制。
返回 Arrow 数组时源 fd、DuckDB、临时空间及发布读锁已退出。

current_market_bindings 逐入口报告正式成交能力缺口，继续 CAPABILITY_UNAVAILABLE，
不从原生表补行情。这四项目前是能力拒绝描述，尚非可启用的正式映射；
单独移除 unavailable_reason 仍不能启用未实现的业务键/日期/会话投影。
market_projection.py 另外提供显式命名的既有 B05/B05-M 合成节点投影，仅用于
隔离契约/集成输入，不默认登记为生产能力；不在读取阶段调用 normalizer。
将来经过底座接受的布局通过 MarketBinding 接入同一通路，不重写底座算法。

### 正式入口映射复查

依据仓库现有正式契约，而非从网页价格存在反推可成交。
入口注册见 [registry.py](../../../../../backend/app/data_store/adapters/registry.py)，
类型/限制见 [record_schemas.py](../../../../../backend/app/data_store/adapters/record_schemas.py)，
来源转换见 [daily_windows.py](../../../../../backend/app/data_store/adapters/daily_windows.py)
及 [record_adapters.py](../../../../../backend/app/data_store/adapters/record_adapters.py)。

| 入口 | 已明确的事实 | 当前成交映射缺口及最小 R01 衔接 |
|---|---|---|
| E50 股票日线 | reported_adjustment=none，未复权基础已知；精确 OHLC；currency/volume_unit/turnover_unit 在正式模型中仅为 None | 需要底座接受的价格币种、volume 的份额含义/比例及整数精确换算。未复权输入不需要新增复权锚点。D04 再实现正式节点身份及真实会话端点。 |
| E51 ETF 日线 | reported_adjustment=forward；当前正式契约明确无锚点/公式，必要成交单位未确认 | 该序列不能标成 raw 成交。研究前复权需底座接受的锚点/因子及单位；成交需已有正式 raw 输入获确认后另映射，不能读原生表补空。 |
| E52 指数日线 | reported_adjustment=not_applicable；指数 OHLC 精确；不是可下单的股票/ETF | 当前 qf.market.v1 是 CNY/整数份额/raw 成交格式，不能把指数点数套入。研究/基准需确认点数单位和收盘可见边界，再由 D05/D11 的研究接口衔接；不要求伪造可交易份额。 |
| E70 fund_daily | price_unit=yuan、price_basis=provider_reported；volume_lots；turnover_thousand_yuan 已明确 | 单位并非未知。仍需底座接受 provider_reported 在该正式入口中的 raw 解释，以及手到整数 ETF 份额的精确换算依据；禁止默认乘 100 或无声截尾。官方字段文档仅证明已列单位。 |

E70 单位的供应商原文：[Tushare fund_daily 文档](https://tushare.pro/document/2?doc_id=127)。
本轮仅查看公开文档，未调用行情接口或供应商账号。文档未给出这里所需的 raw
解释/整数份额换算证明；缺少复权参数不是该证明。旧 watchlist 的 raw_daily_close
标签来自原生仓储，不能代替正式 CurrentStore 的接受事实。

四入口实际主键均为 (representation, subject, object_key, member_key)，日期列为
date32；源代码还需要底座身份解释。合格事实具备后，D04 窄接缝是选择 point 节点、
按接受的 representation/subject/身份过滤、保留精确价量并用 D03 接受的真实日历
生成区间端点；不能将交易日直接强转为 UTC ns，也不能写死自然日/交易时段。
本 PR 未改这些 R01 生产模型、采集器、来源开关或 require_ready。
最小补充是已有正式入口的小型权威单位/口径事实及相应正式可读范围确认，
不是重采集、行情复制或要求 60 域重新验收。未验证生产 CurrentStore 的 receipt，
D94 固定范围通过不代表这四入口的本次生产联调通过。

## Arrow 与 Rust

qf.market.v1 是扁平 schema：事件种类、标的、会话、完整来源身份、
UTC int64 ns、uint64 序号，价格使用精确 UTF8 十进制；价量单位和 raw 基础
在 schema/BatchMetadata 中显式声明。Bar 保留区间起止，Quote 保留双边数量。
未选择的业务字段为 null，不向策略传源 token、DSN 或文件路径。
Rust 使用 D01 Price/Quantity/身份类型；超出精确执行范围拒绝，不截断。

arrow::decode_market 在 Arrow reader 分配前验证 IPC 消息长度、body/buffer 范围、
行数与节点数，以及固定列顺序、类型位宽、buffer 数量、8 字节对齐、互不重叠、
逐列位图/offset/值长度。先拒绝可触发 Arrow 对齐复制的畸形缓冲，再创建 reader。
仅允许一个 schema 和一个未压缩 batch，拒绝字典/嵌套/压缩/未知类型。
解码后再次核对 schema 单位、实际范围、完整键、顺序和内存预算。
ArrowEventSource 实现 D03 EventSource；批次 owner 在转换结束释放，跨块键严格递增，
错误为终态，空批次循环有上限。数据源应按有序标的/来源流拆分，交给 D03
StreamingMerge 合并；不借用文件顺序猜事件总序。

data::ipc::IpcGateway 实现 D01 DataGateway，并提供显式绑定的 stream / RemoteStream。
with_binding 选择代码已授权的默认绑定，单批 read 不会悄悄丢掉多批请求的尾部，
这种请求返回 RESOURCE_LIMIT；正常引擎输入使用流式接口。stream 的 credit 与
父进程预算取较小值，EventSource 输出长度/容量均不超过 D03 请求上限。
EOF 前父进程完成依赖检查，EOF 后 Rust 仍可 check，再由 D13 完成最终事务协调。

## 唯一 IPC 格式与 D12

sdk/python/quantfoundry/_transport.py 是唯一格式所有者；Rust ipc.rs 为其对应实现。
D12 复用或在此格式中扩展结果操作，不另造 pickle/JSON/socket 协议。

连接由 D12 在本 run 私有端点创建和认证；本模块只接收已连接 socket。
serve(channel, gateway, context) 不 import 策略、不启动容器、不接受客户端镜像、
挂载、DSN、SQL 或路径。格式不是安全沙箱；网络/密钥/目录/容器权限仍由 D12 落实。

前缀为 QFD4 + big-endian u32 控制长度 + u64 Arrow 长度。控制头严格为
protocol/run_id/request_id/op/status/body/payload_bytes，protocol=qf.ipc.v1。
默认头 <=1 MiB、Arrow <=64 MiB，读取前缀后先拒绝长度，再分配；重复 JSON key、
未知枚举/额外头字段、错 run、反序 request id 均拒绝。
op 为 open_stream / next / check / close_stream / cancel / close，
status 为 request / ok / batch / eof / error。

每通道最多一个流、一个在途请求。open_stream 只接受 binding/request/max_rows；
next 的一次请求就是一次容量信用，返回一个 Arrow IPC batch 或明确 EOF。
控制/传输阻塞每 50ms 检查取消，有有限墙钟；断管、取消、超时和错误关闭通道、
迭代器与 context。等待 worker 时没有 CurrentStore 锁或源文件 owner。
SDK 客户端收到终态错误、错误响应阶段或错 run/id 时立即关闭自身端点，
不依赖对象析构释放 socket；error DTO 的类型严格检查。
错误 DTO 沿用 D01 code/operation/message/scope；底座 503、missing、限制等保留
store_code/http_status，不伪装 empty。真正的维护准入仍调用原 require_ready。

## 可运行证据与集成边界

backend/tests/test_s3_data_gateway.py 创建随机隔离 PostgreSQL schema 和 CurrentStore，
实际登记/写入 Parquet，再经 DuckDB Arrow、私有 Unix socket、独立 Rust 进程和
D03 StreamingMerge 消费。原 B05/B05-M 的 typed-object-nodes-v2 也走该完整链路；
测试期间阻断读取 normalizer。无预切 Range 替代实际底座。

S3 CI 构建 consume_gateway 例程，QF_S3_INTEGRATION_REQUIRED=1 使缺 Rust 消费者
明确失败，不能跳过。普通无 Rust 环境的后端测试仅跳过进程集成部分。
本地 overlay 通过既有测试 probe 验证真实文件/锁机制；不冒充受支持生产挂载验证。
CI 不注入该 probe；原 Current store isolated acceptance 工作流继续验证支持的挂载。

consume_gateway 是有界样本/统计的隔离验证例程，不是生产 runner。
公开策略视图按 Visibility/完整 Tick 键再裁剪由 D05/D10 实现；
生产容器/IPC peer 权限与资源隔离由 D12 实现；
finalize 中的真实 run 终态/claim 事务由 D13 使用。
上述后续模块及 R01 全域/生产频率可用性不由 D04 冒领。

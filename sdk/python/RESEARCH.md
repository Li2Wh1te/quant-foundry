# S3-D05 研究数据与指标

`quantfoundry.api` 与 `quantfoundry.data` 暴露同一组函数。数据查询只能在有效模拟
回调内使用；默认时点是 `context.now_ns`。本模块已接通真实 CurrentStore/私有 IPC/
已安装 wheel/Rust 查询链，正式入口是否可用仍由原底座及代码拥有的映射决定。
当前修订数据不表示复原过去数据库版本。正式缺口见文末。

## 函数与返回

签名保持 [strategy_api.pyi](../../contracts/strategy_api.pyi)。

| API | 固定列/返回与字段 |
|---|---|
| `get_price` | 长表 `security,time_ns,*fields`；一标的、多标的、空集合相同结构；价格为 Decimal，ns 为 int64，未知数量为 None。Bar 可选 open/high/low/close/quantity/interval_start_ns/interval_end_ns/is_partial/status；Tick 必须明确 price/bid/ask/quantity/bid_quantity/ask_quantity/kind/channel/sequence，默认 close 不适用。 |
| `get_current_data` | 只读映射及冻结 CurrentQuote/Tick。last_price/price_time_ns 可未知；旧价标 stale；halted=None 表示未知，不能推断停牌。Tick 的 time_ns 是报价事件时点，双边数量分开。 |
| `get_fundamentals` | `security,time_ns,instrument_id,report_period,*fields`。revenue/net_profit/total_assets/total_liabilities/eps/roe；time_ns 为已验证公开边界。选最新可见报告期及其可见修订，期末不替代公开时间。 |
| `get_valuation` | 同一长表；pe_ratio/pb_ratio/market_cap/circulating_market_cap；当前快照不填历史。 |
| `get_index_stocks` | 稳定排序的成员 tuple；要求完整历史集合的真实公开/有效期，参考指数不扩展交易授权集合。 |
| `get_industry` | 同一长表；industry_code/industry_name/taxonomy，按有效期。 |
| `get_instruments` | 同一长表；name/exchange/asset_type/currency/listing_date/end_date，按稳定身份及有效期；不从当前名字或来源代码猜历史。 |

TimeLike 接受 UTC ns 整数、有时区 datetime、带时区 ISO 字符串；ISO 的九位小数
保持 ns。日期字符串只能由已接受的参考日历解释为当日开市/闭市边界；缺日历
返回 RULE_UNAVAILABLE。bool、无时区时间、未来 end/as_of 拒绝。
ISO 小数最多九位；超纳秒精度或非 ISO 文本拒绝，不能依赖 Pandas 的无声截断。
get_price 的 start/count 恰好一个；count 按每标的计算，先裁剪完整已发布事件键
再计数。同 ns 尚未发布的 Tick 和引擎未来预取不出现在公开结果。

表格 `attrs['qf']` 给 requested_scope、actual_scope、实际单位、限制、精度和
short_window。少于 count 的历史不补零/前填；质量限制、缺依赖、取消不是空表。
空集合保留同一列与 dtype。纳秒/十进制的私有 JSON 表示均为字符串。
研究 point 单位通过 qf.research.v1 保留；不会进入 CNY/整数份额的撮合 schema。

Filter 只支持 eq/ne/lt/le/gt/ge/in，值为 Decimal/int/bool/有界字符串，in 最多128项。
筛选与排序只能使用已选择的白名单字段；排序 `-field` 为降序，同值最终按 security。
默认 limit=10000，最大10000；最多32个谓词、16个排序。缺字段不满足谓词。
无 eval、SQL、用户路径、供应商参数或隐式当前全市场名单。

## 复权与降采样

只有经过底座接受的累积因子/公式/锚点能进入研究复权。pre 使用查询终点前最后
已公开有效因子为锚，post 使用已接受原始锚；不得跨稳定身份，未来因子不参与。
ResearchBinding 必须明确传入 factor_anchor 的 factor/effective_ns/public_ns；原始锚
缺失、未生效或当时尚未公开均拒绝。post 不从现存因子行推断基准；pre 还要求
查询终点有有效因子，不能用已过期因子代替。重叠身份或同边界歧义修订拒绝。
价格按 `p*factor/basis` 计算，12位 half-even；原价查询保持原 Decimal，不改变
撮合输入。缺因子、锚点或精确数值范围明确拒绝。正式 E51/E69 仍未启用复权。
先按细粒度原 Bar 的有效因子复权，再聚合 OHLC；因子在来源 Bar 内变化或存在
覆盖缺口时拒绝，不能假定盘中路径。因子先按标的分组，避免每个价格重扫横截面。

分钟→更粗分钟/60m 只在 D03 已接受的 bar_windows 内分桶；源与目标必须整除，
源 Bar 必须匹配源桶，不跨午休或会话。只输出已结束桶；会话末短桶标 is_partial。
窗口有洞或 OHLC 缺失时 status=missing 且价格/量为 None；不完整未来桶不返回。
不能从日线升分钟、小时升分钟或把 Tick 粗略解释为 Bar。
当前 ns 的最终细 Bar 尚未发布时，目标桶也不返回；已完成历史桶的缺失与此分开。

## 指标

`quantfoundry.indicators` 提供 sma/ema/rsi/atr/macd/rolling_std/rank，及 SMA/EMA/RSI/
ATR/MACD 别名。接受一维 NumPy、Pandas Series、Decimal/int/float/None；NaN/NA
明确当缺失，Infinity、bool 和字符串拒绝。Series 索引保留，输出 value 为 float64、
status 为 available/warmup/missing；MACD 每列有对应 status。原输入不改。

SMA 完整 N 点；EMA 首 N 连续有效值 SMA seed、alpha=2/(N+1)。RSI 需要 N 个
价格差，Wilder alpha=1/N；全平为50，只有涨为100，只有跌为0。ATR 首个连续
样本 TR=high-low，之后 max(high-low,abs(high-prev_close),abs(low-prev_close))，
首 N 个 TR 均值 seed。MACD 默认12/26/9，histogram=macd-signal，signal 单独
预热。rolling_std 默认 ddof=1、中心化双遍样本方差；rank 同值平均名次，默认
最小值 rank=1，missing 不参加排名。缺值后递归指标重新预热，不前填。

样本上限100000、period上限10000；双遍方差最多1000万窗口元素运算。
Rust 拒绝非有限中间结果。指标 float 不能自动成为订单金额。
可执行查询例程见 [examples/research.py](examples/research.py)；真实链路回归直接
在隔离回调中使用同一 API，未引入替代网关或旧 Python 计算。

## 模块接缝和预算

D04 的 RunDataGateway/serve/_transport 是唯一通道；ResearchDataGateway 继承原
依赖准入、require_ready、质量检查、CurrentStore Arrow 读取、取消、最终事务。
qf.research.v1 是同一 open_stream/next 的新增扁平 Arrow 数据 schema，按字段
保留 kind/value/unit、真实公开/有效期与稳定身份；不是新 socket/版本协议。
ResearchBinding 只映射既有布局；可接入被底座接受的研究事实，不调用 normalizer。

集合筛选及每组 count 是原 CurrentStore Query 的有界扩展，原默认游标兼容。
质量筛选沿用原门禁，宽业务键范围可以保守拒绝，不能放行受限范围。
父进程 count 缓存只保留有限窗口并读取增量；SDK 缓存匹配请求、完整可见边界、
依赖状态，命中仍复核依赖。默认最多10000输入行、64MiB视图、8MiB/32项 SDK
缓存；父进程预算另独立受 D04 控制。超限 RESOURCE_LIMIT，取消打断等待/读取。
可信宿主可从全 run 预算配置视图，硬上限100万行/1GiB；IPC 单批仍最多1万行/64MiB。
扩大至64MiB以上必须给 _DataSession 显式 run_data_budget_bytes，覆盖视图工作集、
缓存、两份最大 IPC 缓冲和2MiB控制开销。该数据份额须由 D10/D12 从完整 run 预算
扣除常驻 Python/引擎/账户等开销后分配；D12 的进程组/cgroup 仍约束完整运行以及
策略主动保留的历史 DataFrame。不能把1GiB视图上限当作整个运行的 RSS 上限。
原生行信用保守覆盖投影、输出映射、JSON/Decimal/Pandas 转换，字符串按输入
payload 的六倍留额外复制余量。reset/expire 释放 Vec 容量；SDK 整表 JSON 只解析
一次。resources_json 提供工作集信用及真实 Vec 容量，用于接缝验证。
例如5000标的×20点需容纳10万历史点与当前已发布事件，不能按单批大小限制整个
lookback。父进程 count 缓存须同时明确配置其原有预算；这些配置不是全量基准通过。
分组排名按映射后的完整事件列排序，最终分页仍按原存储键和签名游标；新 group_order
仅是白名单排名列，旧默认请求指纹不变。无对应历史成员集合明确拒绝；已验证空集
需真实空集记录，完整成员超过返回上限时拒绝而不截断。相同公开时点的歧义修订
不依赖 record_id 字典顺序选取。

D10 每次回调进入 `_callback(session, boundary)`，必须在 finally 退出。原生
ReadView expire 清空缓冲，任何保留的活视图方法返回过期错误；已物化的过去
Decimal/DataFrame/冻结 DTO 可以保留。boundary 使用 D03 Visibility 的 now_ns 与
完整 market_through 键，不能仅传时间戳。跨语言薄绑定在 qf-python，未实现新宿主。
D10 可把已接受已发布的当前行情及完整键传入 published_quotes 内存接缝；此时
get_current_data 不逐 Tick 调父进程，Rust 仍复核完整发布键和 lease。D10/D12 负责
引擎读取块的依赖复核及进程隔离。没有该内存接缝时当前行情复用有界查询缓存。
D13 成功终态继续使用原 finalize(context, commit(connection))；不拆成 check 后
另行提交。D11 不得覆盖 data/views.rs 或 analysis/indicators.rs。

## 正式联调缺口

E14/E15 缺真实历史公开时间；E39 仅当前估值观察；E56/E57 缺历史生效/成员完整集合；
E40/E67 来源目录不能独自证明稳定身份和历史名字；E69 缺已接受锚点及公开时间。
仓库已有独立身份事实服务区分 known_at/observed_at/effective_at；这些字段不是
将 E40 当前来源目录自动变为历史公开数据的许可，亦未在本包验证其生产记录。
底座确认后按 ResearchBinding 接入同一准入和一致性链，不重采集/复制行情。

D04 的 E50 未复权已知但币种/整数份额依据未接受；E51 前复权未锚定；E52 缺
点数单位及闭市可见边界；E70 元/手/千元已知，仍缺 raw 解释/份额换算及日线
可见口径。D05 未启用这些正式行情映射，未把合成节点登记为生产能力。
未做内网/生产联调、部署、供应商调用、凭据变更或清理。

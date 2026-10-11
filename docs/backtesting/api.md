# API 速查与业务语义

源站 `base_url` + `/api/admin/backtests`；所有请求使用现有 Bearer 身份。SDK 的
`BacktestClient(base_url=..., token=..., timeout=30)` 只持有内存凭据，网络等待受超时
及 1MiB 响应预算约束；timeout 范围 (0,300] 秒。返回为原 JSON Mapping，金额/ns 字符串
不变成 float；参数大整数无损、统计数值允许有限 float。`RunConfig.to_dict()` 每次返回副本；
`RunConfig.from_dict()` 要求完整 wire 配置，包括显式 schema，拒绝未知字段。

| Python 方法 | HTTP | 返回/参数 |
|---|---|---|
| capabilities() | GET /capabilities | api_schema、engine、actual_data、limits；实际 unavailable 不能由 engine 支持推断可用 |
| preflight(config) | POST /preflight | errors、warnings、normalized_config；HTTP 200 也须检查 errors |
| submit(config, idempotency_key=...) | POST 根 | 202 run 摘要；幂等重放同摘要 |
| status(run_id) | GET /{run_id} | 状态、进度、cancel_requested、error、partial、归一化配置、冻结费用、retry_of |
| cancel(run_id) | POST /{run_id}/cancel | 当前状态；终态不逆转 |
| result(run_id, kind="summary", limit=100, cursor=None) | GET /{run_id}/results/{kind} | summary 或有界 items/next_cursor/partial/notices；不自动全量读取 |
| batch(configs, idempotency_key=...) | POST /batches，{"configs":[...]} | 202 batch_id、items、has_more、next_cursor |
| batch_status(batch_id, limit=100, cursor=None) | GET /batches/{batch_id} | 有界子 run 摘要；limit 1..256 |
| cancel_batch(batch_id) | POST /batches/{batch_id}/cancel | 第一页；已完成结果保留 |
| retry_failed(batch_id, idempotency_key=...) | POST /batches/{batch_id}/retry-failed | batch_id、run_ids，关联原 failed 项的新 run；cancelled 另取配置新键 submit |
| compare(run_ids) | POST /compare，{"run_ids":[...]} | items、configuration_differences、warnings、ranking=null |

同根 GET 列表使用 limit 1..500/cursor，网页直接调用现有 API。run/batch ID 为 UUID。
提交键为 1..128 个可见 ASCII 字符，不能采用 token 或服务内部保留命名空间。
认证者从 token 派生 owner；伪造 owner header 无效。当前账户/策略是部署内 admin 资源；
run/结果有 owner 隔离，轮换 token 也会改变 owner。没有给 SDK 新持久权限。

RunConfig 使用以下共享契约（必填用 **粗体**）：

| 参数 | 类型/默认/约束 |
|---|---|
| **api_schema** | wire 必须 "qf.backtest.v2"；构造函数默认此值 |
| **strategy_revision_id/account_id** | 已有不可变策略修订和 active 账户 UUID；账户决定费用/权限，不隐含资本 |
| **initial_cash** | 正十进制字符串；CNY、初始持仓为空、无外部注资 |
| **start/end** | YYYY-MM-DD，start<=end；按参考交易日历选择实际会话 |
| **frequency** | 1d/1m/5m/15m/30m/60m/tick；小时为 60m |
| **universe** | 1..10,000 唯一、授权标的；有限集合，非完整历史全市场或成分证明 |
| **parameters** | JSON object；构造默认 {}；最多128字段、64KiB、深度8；再按修订 parameter_schema 校验 |
| **execution_model** | bar_next_interval_v1；tick 使用 trade_tick_v1 或 quote_tick_v1 |
| benchmark | 可选标的/null；省略不自动提供超额指标 |
| currency | "CNY" |
| reference_calendar | 可选字符串；省略时服务必须选到无歧义日历并写回 normalized_config |
| seed | 0，整数 0..2147483647；标准 random/NumPy 和核心使用同配置 |
| participation_rate | "0.10"，(0,1]；模拟共享流动性比例，非真实可成交保证 |
| slippage_bps | "0"，[0,10000]；不利方向基点滑点，0 是理想化假设，不等于费用为0 |
| risk_free_rate | "0"，>-1；明示研究假设，非观测利率 |
| annualization_sessions | 252，整数1..366；随结果公开，不宣称每自然年恰有252日 |
| result_sampling | session_close；bar 可选 bar_close，tick 仅 session_close |
| cost_overrides | 可选 object，仅 commission_rate [0,1]、minimum_commission >=0 的十进制字符串；不覆盖强制日期税费 |

金额/比例输入不是 float，不接受 bool 充当整数、NaN/Infinity、指数金额格式或无声截位。
支持范围由原生校验决定，超限明确报错。SDK、服务和网页均消费同一归一化配置；受理时
固定修订文本、账户版本及费用小配置，排队期间账户修改不改已受理 run。行情不做历史快照；
依赖变化 `DATA_CHANGED` 中止并保留诊断，复跑当前修订数据不保证恢复过去输入。

策略 API 完整签名见 [strategy_api.pyi](../../contracts/strategy_api.pyi)。价格始终为长表
`security,time_ns,...`，Decimal 原价保留，范围/限制在 `frame.attrs["qf"]`。get_price 的 start
与 count 恰好一个，count 按每标的计数，end 默认当前可见边界；显式未来 end/as_of 报
`LOOKAHEAD_FORBIDDEN`。时间参数可为 UTC 纳秒 int、有时区 datetime/ISO 字符串或按市场
会话解释的日期。context.now_ns 精确；current_dt 为时区显示，微秒 datetime 不能作 Tick ID。
Tick identity 包含 session/channel/sequence，同纳秒多事件保留。财务/估值/行业/成分使用
真实公开/生效边界；报告期/采集时间/当前快照不冒充历史知识时间。未覆盖/不合格数据报错，
不补零、不前填、不从供应商或旧接口 fallback。
多标的完整回放还须有可验证的跨存储页事件顺序；仅把每页按时间排序不能修复标的优先的
目录全流顺序。没有受支持的有序投影时返回 CAPABILITY_UNAVAILABLE，正式映射仍属 D04。

initialize 注册 run_daily/weekly/monthly、subscribe；不可在后续回调注册。无显式 subscribe
时匹配频率的 handle_data/handle_tick 默认订阅 universe。Bar 同结束时点合并一次回调；
所有同刻 Bar 先完成撮合/账户更新，再运行策略。Tick 则每个真实事件一次。订单/成交回调
不递归推进引擎。公开视图只在有效回调边界使用，状态仅为本 run 的内存，不提供现场恢复。

OrderResult 包含 accepted、order_id、reason_code、message、unchanged、requested_quantity、
effective_quantity。accepted 是准入，不是成交。目标委托含本标的挂单净量，相同目标不叠单；
新目标被拒时原挂单保留。quantity 下单用正负表达买卖，Order/Trade 返回 side 及非负绝对数量。
get_order/get_open_orders/get_trades 查实际状态；get_trades 为有界 TradePage，最多10,000项。
Portfolio.cash 已包含 frozen_cash；available_cash 不含冻结，不能把 cash 和 frozen 再相加。
跨标的卖款必须实际成交并达到规则可用时点后才能买入。

Bar 模型在下一可匹配区间结束确认成交（execution_time_ns=bar_end），用 open 代理/限价触及，
不是已观察 Bar 内倒填开盘成交。DAY 盘后委托归属下一会话；部分成交共享该 Bar 的参与率量，
涨跌停等方向不能证明可成交时不成交。Trade Tick 使用后续总序事件的可验证打印量；Quote
Tick 买 ask/卖 bid，双边量分别共享；同价同量重复报价不刷新消耗额度。均无真实队列位置、
逐单订单簿或精确撤单延迟。结果 execution_model 含模型名、assumptions 和 limitations。

费用由产品/方向/日期事实与已受理账户佣金组成；最低佣金按订单累计，只对差额扣收，
不对每个部分成交重复扣最低。缺费率或适用依据不能当0；合成最低费1等仅为 named fixture。
内部移动成本总額12位 half-even，最后卖出释放全部余成本；展示均价12位，不反写成本。
结算金额按适用费用/货币规则舍入。分红应收/支付与现金不双计；末仓按已验证/stale 政策估值，
缺价不填0、不强平。法规适用性信息不足时不宣称实盘税后精确。

summary 含 outcome/partial、performance、execution_model、notices；D11 performance
包含权益、收益、回撤、波动、Sharpe、基准/超额、换手和交易/费用统计。指标对象是
`{"status":"available","value":...}` 或 `{"status":"unavailable","reason":...}`。
日收益为 E_t/E_(t-1)-1，累计 E_end/E_start-1；回撤损失取非负幅度，波动使用 ddof=1 和
sqrt(A)，年化 `(1+累计)^(A/n)-1`，日无风险 `(1+rf)^(1/A)-1`。Sharpe 为日超额均值/样本
标准差*sqrt(A)。不足样本/零分母/不可估值不给伪数值，不跨缺日拼收益。超额为策略累计
减基准累计的百分点；换手为买卖总成交额/(2*平均有效日终权益)。分钟/Tick 的统计仍按日终。

queued→starting→running→succeeded/failed/cancelled；cancel_requested 是非终态。
取消/成功由一次原子提交决定赢家；claim 防迟到结果，崩溃重跑从头开始，仅一个权威结果，
不承诺恰好一次实际执行。服务不能把旧任务当作新语义恢复。

| 诊断 | 处理 |
|---|---|
| UNAUTHENTICATED / HTTP 401 | 通过既有方式更新身份，勿把 token 放入命令输出 |
| DATA_RESTRICTED / 403/404 | 核对所有者、账户/修订、授权集合和质量限制 |
| CAPABILITY_UNAVAILABLE / RULE_UNAVAILABLE | 检查实际能力及日期事实；正式缺口留待数据映射，勿改门禁 |
| INVALID_RUN_CONFIG / STRATEGY_ERROR | 核对共享 schema、修订参数、回调签名；运行异常带有界位置 |
| LOOKAHEAD_FORBIDDEN | end/as_of 不能晚于模拟可见时点 |
| INSUFFICIENT_CASH / INSUFFICIENT_SELLABLE / INVALID_ORDER | 检查 OrderResult，等待实际成交/可卖时点，按申报单位提交 |
| NUMERIC_RANGE_UNSUPPORTED | 使用受支持的精确输入/范围，不能静默截位 |
| DATA_CHANGED | 此 run 失败；检查诊断后用新键重跑，不混合修订数据 |
| RESULT_NOT_READY | 等待终态，再取一页 |
| RESOURCE_LIMIT / RESULT_BUDGET_EXCEEDED | 减小参数/页/记录量，核心必需结果超预算不得静默丢失 |
| CANCELLED | 保留 partial，不能当完整收益 |
| LEGACY_ENGINE_RETIRED / 410 | 旧创建接口已退役；迁入新修订，旧历史只读 |
| HTTP_TIMEOUT / HTTP_UNAVAILABLE / REDIRECT_REFUSED / INVALID_RESPONSE | 检查源站/TLS/服务；提交未知时复用原键，避免重复运行 |

HTTP 错误通过 BacktestError.code/message/operation/scope/status_code 暴露；本地 RunConfig
或数值校验沿用 ContractError。preflight.errors 是诊断数组，业务拒单是 OrderResult；两者
不混成进程异常。未来查询等致命策略错误会使运行 failed，结果明确 partial。

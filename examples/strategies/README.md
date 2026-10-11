# 五类可运行策略

这是合成隔离验收样例及迁入自有 S3 API 的参考，不是生产行情、投资建议或聚宽原策略兼容。
每个文件可单独作为策略修订，所用函数/参数以实际 `quantfoundry.api` 为准。

| 例子 | 必需输入和能力 | 合成回归 |
|---|---|---|
| etf_rotation.py | 批量日线 raw close/OHLC/quantity，日期 ETF 规则/费用、会话、权限 | 3 样本动量；持有旧 ETF 后跨会话卖完再买新 ETF |
| stock_momentum.py | 股票批量价格、可卖/日期规则与费用、会话 | 第3个合成周交易日选股，每日继续调仓，实际旧→新换仓 |
| fundamental_selection.py | 历史 net_profit Decimal、稳定身份、真实公开/生效时点、日线规则/费用 | 两个月的两次公开修订，月选股、每日完成卖后买 |
| intraday.py | 对应 1m 或 60m 完成 Bar，明确午休分段、OHLC/quantity/规则 | 相同策略分别跑 1m、60m，不从日线插值 |
| basic_tick.py | Trade Tick price/quantity、精确 ns、session/channel/sequence、日期规则/费用 | 同纳秒合法不同序号共72次回调，真实下单/成交/结果，不宣称盘口队列 |

价格比较保持 Decimal，不用 Pandas.mean 把价格转 float。拒单记录 reason_code/message；
ETF/股票/财务例子查实际持仓和剩余卖单再买，受理的预计卖款不可用。每日继续回调只完成
最近一次选股，不改变周/月信号周期；部分成交/拒售会继续等待。不假设 T0 适用于全部产品。

`run_configs.json` 的五项说明都有 requires 和完整 config。UUID 是说明用占位记录，
并非任何生产账户/修订 ID。隔离 suite 先通过现有账户/策略服务建立实际记录和参数 schema，
发布不可变修订，替换成实际 UUID 后输出/执行配置。正式用户替换为自己的已有记录；不用
fixture_calendar 绕服务门禁。五个参数形状与 RunConfig/CLI 原生校验对齐。
Bar 示例的参数 `end_date` 与 RunConfig.end 一致：最后会话盘后不再下需要下一会话的 DAY
单，intraday 在终点15:00后停止新信号；末仓仍按核心估值，不强平。修改日期时同步该参数。

合成输入是12个明确构造的会话（2026-01-26..2026-02-06，包括合成周末会话），两标的价格
故意翻转以检验调仓，intraday 分午休前后四个小时区间/短分钟输入，Tick 带 +123ns 的合法
不同序号。合成申报单位1、允许当会话可卖/成交卖款立即可用、每订单最低佣金1、参与率1、
滑点0仅为测试模型；不是中国市场交易日历、真实 T+规则或法定零费用声明。财务例子只要求
net_profit，不假定行业/指数名单；要添加相应筛选须先具备那些真实历史能力。

真实数据不足由 formal provider/preflight 或动态查询明确报 CAPABILITY_UNAVAILABLE /
RULE_UNAVAILABLE / DATA_RESTRICTED。未来读取报 LOOKAHEAD_FORBIDDEN，不能以空表/填零
或供应商请求掩盖。样例不声称 CurrentStore 正式映射已经完成。

安装与全部命令见 [用户指南](../../docs/backtesting/README.md)。
`scripts/test_s3_runtime.sh` 在现有 runner→兄弟 worker 隔离 CI 跑每类完整
preflight→submit→status→result；1m/60m 两个变体均必跑，无跳过、无供应商/生产访问。
另外 host CurrentStore→IPC→Rust 回调回归用于快速定位策略错误，不能替代真实容器隔离。
安装 wheel 后 CLI 与 Python 对同配置的 normalized_config、performance 和模型说明核对一致。

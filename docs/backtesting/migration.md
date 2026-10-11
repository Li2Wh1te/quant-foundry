# 旧策略迁入与聚宽式写法对照

迁入是一份新的 S3 策略副本和新发布修订。先读取/下载旧修订原文，在本地编辑副本，再在
策略页“新建”独立策略、校验并发布新的不可变修订，记录返回的 revision UUID 到 RunConfig。
不对原策略 draft 发 PATCH，不改旧修订，不删账户或历史运行；旧稿与旧结果仍只读可用。
服务既有 POST `/api/admin/strategies` 创建新 draft，POST `/{新策略id}/publish` 用
`draft_version/alias` 发布，版本冲突重新读取后处理，不覆盖旧内容。发布是 append-only。
D14 回归核对原策略 ID、draft 文本/版本、revision 文本/哈希和旧精确结果，而不只比行数。

不提供零修改聚宽/旧 Quant Foundry 兼容，不把未核对的聚宽行为当作官方事实。下面仅给
常见写法迁入自有 API 的方向；旧样例的市场/费用/历史数据能力须分别核对。

| 常见旧/聚宽式写法 | Quant Foundry S3 |
|---|---|
| g.xxx /全局运行状态 | context.state.xxx，仅本 run 内存；参数从不可变 context.params 取 |
| 旧 run(context, parameters) 主入口 | initialize/context + handle_data/handle_tick、定时回调；无旧 Python engine fallback |
| history/attribute_history 后直接 Series.mean | get_price(...,count=N,fields=["close"]) 长表；Decimal 的 sum 比较，窗口不足明确预热 |
| set_universe/任意新增标的 | RunConfig.universe 是有限预授权集合，subscribe 只在 initialize 内选择子集 |
| 当前财务/行业/成分当历史名单 | get_fundamentals/get_industry/get_index_stocks 按真实公开/生效边界；缺能力明确拒绝 |
| 日期字符串代表一个精确 Tick | context.now_ns、Tick time_ns + channel/sequence；datetime 只供显示 |
| 卖单返回后马上买新标的 | 检查实际 position.quantity、get_open_orders/get_order；卖出实际完成且资金可用后再买 |
| order_target_percent 后假设完全成交 | 检查 accepted/reason_code/effective_quantity；后续通知/成交页确认 |
| 设置二进制 float 金额/佣金 | 十进制字符串/Decimal，RunConfig/cost_overrides 用字符串；账户保留强制日期费用 |
| 中断后恢复任意 Python 对象现场 | 查询错误/取消，再以新键从头运行；批次 failed 用 retry_failed，cancelled 取配置新键 submit，保留已完成项 |
| 调用旧创建 URL 或旧调试 worker | 经认证返回 410；新提交仅使用 v2 统一服务 |

核对五个自包含例子所用函数均来自实际发布的
[strategy_api.pyi](../../contracts/strategy_api.pyi)，不要以未声明 helper 参数延续旧写法。
每个新的修订先在合成隔离数据验证 preflight→run→result，再检查真实实际能力；合成会话、
费用和行情不能写成生产可用。历史结果读取只展示原保存值/公式，compare 明示旧/新语义
和模型差异，不用新核心回算覆盖旧结果。

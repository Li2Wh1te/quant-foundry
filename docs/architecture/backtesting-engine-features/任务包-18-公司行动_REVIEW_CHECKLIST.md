# 任务包 18：ETF 公司行动与现金分红——审查检查表

本表依据《任务包-18-公司行动.md》冻结，是本任务包后续审查的唯一基线。状态由 `任务包-18-公司行动_REVIEW_STATUS.md` 记录。

## 验收项

| 编号 | 可量化验收标准 | 验证要点 |
|---|---|---|
| A01 | `fund_div` 合法 ETF 已实施现金分红保存来源事实和 complete 统一事实 | 来源快照、规范化事实均可查询 |
| A02 | 非目标基金仅计 skipped；目标 ETF 映射缺失/歧义 invalid 且不推进 checkpoint；不扩大资产范围 | 资产过滤、失败与 checkpoint 原子性 |
| A03 | 相同逻辑键和内容重复采集为 unchanged，不新增版本 | 幂等测试 |
| A04 | 相同逻辑键内容修订追加版本并 supersedes，旧事实保留 | append-only 修订链 |
| A05 | 逻辑键对应两个独立事件时批次 blocked，不按数组序号消歧 | 冲突检测 |
| A06 | 未知/不完整来源状态保留原始事实且不可执行会计 | 精确状态映射 |
| A07 | 双日期存在时按批准规则选择 `earpay_date` 并记录证据 | T18-00 画像与规则冻结 |
| A08 | 仅 `earpay_date` 可正常派生 | 日期规则 |
| A09 | 仅 `pay_date` 按批准规则 fallback | 日期规则 |
| A10 | 两日期均缺失为 invalid，固定预检 blocked | 稳定错误码 |
| A11 | 双日期不同时仍按批准规则选 `earpay_date`，保存两个原值，不按大小交换 | 来源语义 |
| A12 | 仅 `account_date` 不兜底，blocked | 禁止错误字段兜底 |
| A13 | 选中日期为开市日时有效日为同日 | 具名日历 |
| A14 | 选中日期为非开市日时映射到下一开市会话 | 具名日历与时区 |
| A15 | 日历/时区/定义证据缺失 blocked，不默认 SSE/服务器时区 | 失败关闭 |
| A16 | 完整未截断 per-code 全量请求可生成现金 coverage fact | 覆盖证明 |
| A17 | 增量公告日未返回某 ETF 不能生成 complete-zero | 覆盖语义 |
| A18 | 空公司行动表且无覆盖事实为 unavailable/blocked | 不把空表当完整 |
| A19 | complete 且 event_count=0 可证明窗口覆盖且无事件 | complete-zero evidence |
| A20 | 数量类生产覆盖不存在时 formal blocked，internal 仅具名 fixture | profile 门禁 |
| A21 | 固定标的检测数量类事件整体 blocked，不调整持仓/挂单 | 硬门禁 |
| A22 | 动态候选数量覆盖不足过滤候选 | 复用任务包 15 |
| A23 | 实际选中后资格失败终止整次运行且无部分订单 | 选中后复检 |
| A24 | 登记日前于运行起点且到账在窗口内、无冻结权益时 blocked，不从起点持仓反推 | 应收边界 |
| A25 | 内部显式冻结 entitlement 时可执行并进入快照 | 内部 profile |
| A26 | D2 开盘前资金不足时订单仍失败/不成交，分红仅开盘撮合后入账 | 会计时序 |
| A27 | 1000 份、每份 0.10 的分红有效日现金增加 100.00 | 金额计算 |
| A28 | 登记日后卖出不减少已冻结权益 | 权益冻结 |
| A29 | 登记日后买入不获得该事件权益 | 权益冻结 |
| A30 | 重复事件/快照只应用一次或冲突 blocked | 幂等与冲突 |
| A31 | 同日多笔合法分红批量原子应用且稳定排序 | 批量会计 |
| A32 | 批量中任一笔破坏账户不变量时整批不提交 | 原子性 |
| A33 | qfq/hfq 仅影响研究信号，成交/费用/分红/估值仍 raw | 价格口径隔离 |
| A34 | Provider 公司行动查询不访问网络且不返回 ORM | engine-only 读取 |
| A35 | 相同事实不同输入顺序产生相同 Provider 结果和 snapshot hash | 确定性 |
| A36 | cutoff 下两个 active 版本返回 contract violation/blocked | PIT 唯一版本 |
| A37 | generated_at/中文 message 变化不改变 snapshot/report hash | hash 稳定性 |
| A38 | source fact/rule/coverage 内容变化改变 snapshot/report hash | hash 完整性 |
| A39 | 增量目标行失败不推进 checkpoint，中文日志含失败数 | 调度原子性 |
| A40 | 近期复核发现修订追加版本且主 checkpoint 不推进 | 复核语义 |
| A41 | 三个 scheduler task 展示 `中文名（English name）`，普通标签不暴露 key | 稳定任务类型 |
| A42 | 日志页面以中文标题/message 为主，技术字段可展开 | 前端映射 |
| A43 | formal profile 使用数量覆盖 fixture 时 blocked | 正式门禁 |
| A44 | internal profile 使用范围不足 fixture 时 blocked | 内部门禁 |
| A45 | task 17/19/20 未完成时本任务不补实现，formal 仍由 16B 门禁 | 依赖边界 |
| A46 | 登记日在窗口内、到账日晚于终点时 blocked，不静默忽略/提前入账 | 应收边界 |

## 完成定义与红线

| 编号 | 条款 |
|---|---|
| R01 | 数据源仅为 Tushare ETF/场内基金 `fund_div`，不得接入股票分红。 |
| R02 | 不实现拆分/合并/份额变化数量会计、成本调整或挂单调整。 |
| R03 | 不新增通用覆盖框架、token 签发/校验、交易状态生产、通用 revision vector。 |
| R04 | 不从公司行动重算 qfq/hfq，不实现新日历算法或默认 SSE。 |
| R05 | 不新增正式运行创建入口、队列、Supervisor、策略工作台或管理 CRUD 页面。 |
| R06 | 现金事件只能经 `DataProvider.corporate_actions()` 进入生产运行；Runner 不读 ORM/Tushare。 |
| R07 | 来源事实 append-only；历史版本不可覆盖/删除；来源撤回不生成账户冲销。 |
| R08 | 空事件表不得推断 complete；数量类生产覆盖未审批时 formal 必须 blocked。 |
| R09 | 三个调度任务 key 稳定且中英文展示；操作日志必须含中文 message、范围、计数与 checkpoint。 |
| R10 | migration 必须 SQLite/PostgreSQL upgrade/downgrade 往返且不改动其他任务表。 |
| R11 | 必须生成 T18-00 来源画像并冻结日期/状态规则；证据不足时不得临时选择规则上线。 |
| R12 | 必须运行定向、完整后端测试及前端构建，并证明运行读取无外网。 |

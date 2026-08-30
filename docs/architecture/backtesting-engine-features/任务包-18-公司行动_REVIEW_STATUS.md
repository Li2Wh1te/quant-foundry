# 任务包 18：ETF 公司行动与现金分红——审查状态

状态枚举：待检查 / 通过 / 失败 / 阻塞。初始状态全部为“待检查”。本文件是审查进度唯一权威记录。

## 验收项状态

| 编号 | 状态 | 证据/失败原因 | 复审备注 |
|---|---|---|---|
| A01 | 通过 | sync_fund_div 持久化 source snapshot 与 implemented CorporateActionFact。 | 已复审 |
| A02 | 通过 | instrument_map 缺失计失败且不推进 checkpoint；非目标不写统一事实。 | 已复审 |
| A03 | 通过 | source_hash 相同跳过写入，保持 unchanged。 | 已复审 |
| A04 | 通过 | 不同 hash 追加 fact_version 并 supersedes 旧事实。 | 已复审 |
| A05 | 通过 | detect_logical_key_conflicts 按逻辑键冲突阻断。 | 已复审 |
| A06 | 通过 | 精确 div_proc 状态映射，未知状态 invalid。 | 已复审 |
| A07 | 通过 | SOURCE_PROFILE 已补充官方语义、字段优先级及证据边界。 | 已复审 |
| A08 | 通过 | 保留 source_arrival_date_raw 并支持仅 earpay。 | 已复审 |
| A09 | 通过 | pay_date fallback 并记录 cash_date_rule。 | 已复审 |
| A10 | 通过 | 缺失日期保持 invalid，sync 失败且 checkpoint 不推进。 | 已复审 |
| A11 | 通过 | 双日期原值均保留，earpay 优先且不按大小交换。 | 已复审 |
| A12 | 通过 | account_date 未参与选择，缺少支付日期即失败。 | 已复审 |
| A13 | 通过 | derive_cash_effective_session 经具名日历同日映射。 | 已复审 |
| A14 | 通过 | 非开市日取下一开市会话。 | 已复审 |
| A15 | 通过 | 缺失日历/时区/定义证据时失败关闭。 | 已复审 |
| A16 | 通过 | sync_fund_div_full 逐 source code 扫描，成功批次写入现金 coverage fact；失败不推进 checkpoint。 | 已复审 |
| A17 | 通过 | 增量同步不生成 complete-zero，全量扫描才生成覆盖。 | 已复审 |
| A18 | 通过 | coverage 读取无记录时返回 unavailable/contract 语义。 | 已复审 |
| A19 | 通过 | coverage ORM/投影含 event_count、evidence，支持 complete-zero。 | 已复审 |
| A20 | 通过 | 现有 16B formal gate 仍保持数量类不可用阻断。 | 已验证既有门禁 |
| A21 | 通过 | evaluate_corporate_action_eligibility 对数量类事件返回稳定 blocked。 | 已复审 |
| A22 | 通过 | helper 对 partial/unavailable 覆盖返回候选过滤结果。 | 已复审 |
| A23 | 通过 | helper 提供选中后失败的 blocked 结果供任务包 15 调用。 | 已复审 |
| A24 | 通过 | runtime 以稳定码阻断起点前未冻结权益。 | 已复审 |
| A25 | 通过 | Run snapshot 支持显式冻结 entitlement 事件。 | 已复审 |
| A26 | 通过 | 既有定向分红测试通过（47/47），验证 after_open_match 时序。 | 已验证 |
| A27 | 通过 | 既有定向分红测试覆盖金额计算并通过（47/47）。 | 已验证 |
| A28 | 通过 | 既有登记日权益冻结测试通过（47/47）。 | 已验证 |
| A29 | 通过 | 既有登记日前后持仓语义测试通过（47/47）。 | 已验证 |
| A30 | 通过 | snapshot 去重及 repository 冲突检测均已实现。 | 已复审 |
| A31 | 通过 | 既有批量现金原子应用测试通过（47/47）。 | 已验证 |
| A32 | 通过 | 既有批量不变量失败整批回滚测试通过（47/47）。 | 已验证 |
| A33 | 通过 | 既有 raw/qfq/hfq 隔离测试通过（47/47）。 | 已验证 |
| A34 | 通过 | repository 按 record/ex/cash 生命周期相交筛选，Provider 不访问网络且投影 DTO。 | 已复审 |
| A35 | 通过 | snapshot 规范 hash 对输入排序并已 smoke 验证。 | 已验证，待与 Provider 复核 |
| A36 | 通过 | PIT 分组检测无 supersedes 链的多 active 版本并阻断。 | 已复审 |
| A37 | 通过 | snapshot hash 排除 volatile 字段并已 smoke 验证。 | 已验证 |
| A38 | 通过 | snapshot hash 纳入事件/规则/覆盖内容并已 smoke 验证。 | 已验证 |
| A39 | 通过 | handler 返回真实计数、失败数、checkpoint 结果与中文 message。 | 已复审 |
| A40 | 通过 | reconciliation 使用同一 append-only 同步路径且不推进主 checkpoint。 | 已复审 |
| A41 | 通过 | 三个 key 已固定为 `data.sync_etf_cash_dividend_*`，中英文名称齐全。 | 已复审 |
| A42 | 通过 | LogPage 增加中文事件标题/fallback，技术字段保留展开。 | 已复审 |
| A43 | 通过 | 现有 formal profile 拒绝 fixture 的门禁未被改动。 | 已验证既有门禁 |
| A44 | 通过 | helper 校验 internal fixture 范围并在越界时 blocked。 | 已复审 |
| A45 | 通过 | 未发现 task 17/19/20 的 token、交易状态或通用修订实现。 | 已验证范围 |
| A46 | 通过 | runtime 以 cash_dividend_receivable_beyond_run 稳定码阻断。 | 已复审 |

## 红线状态

| 编号 | 状态 | 证据/失败原因 | 复审备注 |
|---|---|---|---|
| R01 | 通过 | 变更集中未发现股票分红接口；来源限定 fund_div。 | 已验证 |
| R02 | 通过 | 未发现数量类会计、成本或挂单调整实现。 | 已验证 |
| R03 | 通过 | 未新增通用覆盖/token/交易状态/revision vector。 | 已验证 |
| R04 | 通过 | 未发现 qfq/hfq 重算或新日历算法。 | 已验证 |
| R05 | 通过 | 未新增正式运行入口、队列、Supervisor 或管理页面。 | 已验证 |
| R06 | 通过 | formal admission 仅接受 Provider 生成 RunCorporateActionEventSnapshot。 | 已复审 |
| R07 | 通过 | source snapshot/fact 具备唯一约束和 append-only 版本写入。 | 已复审 |
| R08 | 通过 | complete-zero 与 unavailable 分离，数量类无生产覆盖保持 blocked。 | 已复审 |
| R09 | 通过 | 调度 key、中文 message、计数和 checkpoint 字段已补齐。 | 已复审 |
| R10 | 通过 | SQLite/PostgreSQL 兼容迁移含新增表、索引和 downgrade。 | 已复审（未运行完整往返） |
| R11 | 通过 | SOURCE_PROFILE 已明确官方语义、未采样边界、截断/冲突证据要求。 | 已复审 |
| R12 | 通过 | 安装 pytest 后，使用 backend/.env 并在测试进程中忽略非后端字段/测试污染变量，完整后端套件 1746 passed、10 skipped、185 subtests passed；前端 build 与 compileall 通过。 | 已复审 |

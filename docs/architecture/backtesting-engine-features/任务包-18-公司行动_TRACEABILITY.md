
## ingestion

| 检查项 | 代码位置 | 测试/输出 | 说明 |
|---|---|---|---|
| T18-00 | `任务包-18-公司行动_18-00_SOURCE_PROFILE.md` | 文档审阅 | 固定 fund_div 字段、状态及日期规则 |
| T18-01 | `backend/app/data_ingestion/models/corporate_action.py`；迁移 `20260830_02_add_corporate_action_facts.py` | Alembic upgrade/downgrade | 三张事实表，JSON/Decimal/UUID/date 可移植 |
| T18-02 | `clients/tushare.py`；`services/corporate_action.py` | normalize_fund_div | 显式 fields，原始值哈希及规范化 |
| T18-03 | `services/corporate_action.py`；`repositories/corporate_action.py` | sync_fund_div | 全量/增量调用统一入口，保留版本读取接口 |
| T18-10 | `scheduler_tasks/corporate_action.py`；`scheduling/registry.py` | registry import | 三个双语稳定任务 key，参数禁止额外字段 |

## runtime

| 检查项 | 代码位置 | 测试/输出 | 说明 |
|---|---|---|---|
| T18-08 | `backend/app/backtesting/data/corporate_actions.py` | `PYTHONPATH=backend python3` smoke import | 新增不可变 `RunCorporateActionEventSnapshot` 纯适配层；按事件稳定排序、重复冲突阻断，并提供跨 chunk merge。 |
| T18-09 | `backend/app/backtesting/data/corporate_actions.py` | snapshot hash smoke | SHA-256 覆盖事件、来源修订、日期/时序规则和覆盖摘要；排除 run_id、生成时间等 volatile 元数据。 |
| T18-11 | `backend/app/backtesting/runtime.py`、`dividends.py`、`accounting.py` | 现有定向分红测试 | 复用既有登记日冻结、after_open_match 入账、幂等及批量原子性实现；未引入数量类会计或正式入口。 |

## provider

| 检查项 | 代码位置 | 测试/输出 | 说明 |
|---|---|---|---|
| T18-04 | `backend/app/backtesting/dividends.py` | `select_source_payment_date` smoke | 注册 earpay_date 优先、pay_date fallback 日期规则及 after_open_match 时序证据；缺失日期稳定失败。 |
| T18-05 | `backend/app/data_ingestion/repositories/corporate_action.py`; `backend/app/backtesting/data/adapters/etf.py` | repository/adapter import smoke | PIT cutoff 读取唯一版本，稳定排序去重，ORM 投影为 CorporateAction DTO，禁止网络访问。 |
| T18-06 | `backend/app/backtesting/data/adapters/etf.py` | adapter corporate_actions smoke | 领域事实通过统一 adapter 输出，覆盖能力由注入 repository 提供，不生成隐式 fixture。 |
| T18-07 | `backend/app/backtesting/preflight.py`; `backend/app/backtesting/data/universe.py` | existing preflight tests | 复用现有固定/动态/选中后资格端口与稳定阻断码，未复制 Universe 编排。 |

## repair_ingestion

| 失败编号 | 代码位置 | 测试/输出 | 修复说明 |
|---|---|---|---|
| A01-A12,A16-A19,A39-A42,R07,R09-R12 | `clients/tushare.py`; `schemas/corporate_action.py`; `models/corporate_action.py`; `scheduler_tasks/corporate_action.py`; `migrations/versions/20260830_02_add_corporate_action_facts.py`; `frontend/src/pages/LogPage.tsx` | 定向 pytest 未运行：环境缺少 pytest 模块 | 补齐 fund_div 17 字段请求、双日期原值及规则证据、精确状态映射、逻辑键冲突检测；来源快照/事实版本唯一约束；固定三任务 key、中英文名称、中文日志事件映射；迁移增加 endpoint/query 唯一约束。 |
| A01-A06,A16,A39-A40,R07,R09-R10 | `services/corporate_action.py`; `scheduler_tasks/corporate_action.py` | `compileall` 通过 | 增加完整响应（含零行）快照持久化、逻辑键冲突失败计数及 checkpoint 仅成功推进；调度 handler 输出真实计数与中文 checkpoint 结果。 |

## repair_provider

| 失败编号 | 代码位置 | 测试/输出 | 修复说明 |
|---|---|---|---|
| A13-A15,A18-A25,A30,A34,A36,A44,R06,R08 | `backend/app/data_ingestion/repositories/corporate_action.py`; `backend/app/backtesting/data/adapters/etf.py` | `python3 -m pytest ...`：环境缺少 pytest 模块 | Repository 按生命周期日期与请求窗口相交筛选；PIT cutoff 下同一逻辑键同版本多 active 事实抛出 provider contract violation；Provider 仅经注入 repository 投影不可变 DTO，现金事件要求显式 calendar/timezone/date-rule/timing 证据并拒绝未知质量状态，避免网络/ORM 泄漏。 |

## repair_runtime

| 失败编号 | 代码位置 | 测试/输出 | 修复说明 |
|---|---|---|---|
| A23-A25,A30-A33,A46,R06,R08,R12 | `backend/app/backtesting/data/corporate_actions.py`; `backend/app/backtesting/data/adapters/etf.py`; `backend/app/backtesting/runtime.py` | `backend/.venv/bin/pytest`：运行环境未提供 pytest；静态编译检查通过 | Provider CorporateAction DTO 可转换为冻结 run snapshot；Runner 接受快照协议，校验起点前未冻结权益及终点后应收分红并阻断；事件哈希、稳定去重与批量原子会计沿用现有实现。 |
| A24,A46,R06 | `backend/app/backtesting/preflight.py` | `python3 -m compileall` 通过 | 预检识别 Provider 返回的 `cash_dividend_entitlement_outside_run` 与 `cash_dividend_receivable_beyond_run` 稳定阻断码。 |
| A23-A25,A46,R06,R12 | `backend/app/backtesting/runtime.py` | `python3 -m compileall`、snapshot smoke 通过 | formal admission 绑定时仅接受 Provider 生成的 `RunCorporateActionEventSnapshot`；暴露快照 hash 与 cash/timing 规则引用用于审计，并保留起点权益/终点应收阻断。 |
| A21-A25,A44,R06 | `backend/app/backtesting/data/corporate_actions.py`; `runtime.py` | `python3 -m compileall`、snapshot smoke 通过 | 增加 `from_data_provider` 明确通过冻结 `DataChunkSession.corporate_actions()` 构造快照；internal 可显式冻结权益执行，formal admission 对任意绕过 Provider 的 fixture 保持阻断。 |

### repair_provider_round3
- A13-A15/A18-A25/A44/R06/R08：`EtfFactsAdapter.corporate_action_coverage_facts` 将覆盖 ORM 行投影为 16A `DataCoverageFact`，保留 event_count=0、unavailable 状态及规则/证据；现金事件日期规则和具名日历证据在 Provider 边界校验，缺失即稳定阻断。

### repair_provider_final
- A13-A16/A21-A23/A25/A44/R11：补充具名日历、时区和定义证据驱动的现金日期映射函数 `derive_cash_effective_session`；来源画像明确全量 per-code 与增量 complete-zero 边界、官方字段语义及样本证据约束。

### repair_provider_eligibility_helper
- A20-A25/A44/R11：`evaluate_corporate_action_eligibility` 提供固定/动态/选中后共用的稳定 blocked/filter/eligible 结果，覆盖数量类正式阻断、internal fixture 范围、coverage 不完整及 entitlement 未冻结。

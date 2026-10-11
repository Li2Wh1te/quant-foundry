# S3-D09 RESULT

基于 main `45b7d24583372ae49d5b9c96f581ed184b4715cd`，独立分支
`codex/s3-d09-accounting`。本次终点为 D09 README；D06 接续由父线程协调。

## 已交付

- 唯一 Rust `Account`：可用/冻结现金、可卖/冻结份额、12 位 half-even 权威总成本、
  已实现损益、订单级累计费用、T+ 会话结算、显式卖款可用日期和有界账户视图。
- 公司行动：登记持仓、除权应收、支付转现金、FIFO 分红税；股份分配/拆并及独立
  上市可卖会话；明确不参与配股，必要条款缺失拒绝。原价估值与研究复权分离，
  已知停牌标 stale，未知缺价保持不可计算，终点未平仓不强平。
- 成交仅采用真实 D01/D02 类型、费用累加器及规则；每次更新先受检计算再提交。
  `preview/commit` 候选绑定账户和修订，支持 D06 原子替换及公司行动取消订单。
  实际调用约定见同目录 [README.md](README.md)。
- D03 增补默认兼容 `AccountPort::assess_fill`：只核定 fee，账户、通知、结果使用
  同一费用；`ExecutionPort::session_end` 在末次行情后、DAY 到期/盘后回调前处理
  登记及收盘支付，并以 `check_session_end` 验证真实关闭状态（默认明确拒绝）。
  现有 DTO/方法签名保持；D02 价格格检查仅扩大为 crate 内可用。

## 已运行验证

工具链 Rust 1.90.0 / Python 3.12.2。隔离夹具明确
`synthetic_business_oracles`、`production_executed=false`；预期直接取包内原本缺失的
`contracts/accounting_cases.json` 和手算断言，不以旧 Python 引擎作正确性 oracle。

- `cargo test -p qf-core --locked --test accounting`：47 passed，0 ignored。
  含 6 个真实 D03 driver + D09 + D02 费用接缝用例；host/data/matcher/订单存储
  明确为隔离适配。另有官方 D02 股票/ETF 日期规则与费用案例，行情/现金可用
  事实仍为明示隔离输入，不代表正式数据联调。
- `cargo clippy --workspace --all-targets --locked -- -D warnings`：通过。
- `QF_S3_PYTHON=backend/.venv/bin/python bash scripts/test_s3_engine.sh`：退出 0；
  fmt/clippy、workspace Rust 139 项、公共核心契约、生成契约/版本一致性、仓库
  Python 18 项、release wheel 构建及独立安装后的 SDK 29 项全部通过。
- `git diff --check` 通过；D05 `data/views.rs` / `analysis/indicators.rs` 无改动。
  云端 CI 以本 PR 最终精确 head 的 Actions 输出为准，不能用本地通过代替。

独立手算重点：买 100×10+1、卖 40×12−1，现金 9478、成本 600.6、已实现
78.6、权益 10198；1 元成本/3 份均价 0.333333333333，全卖释放完整 1 元；
0.005/0.015 half-even 到 0.00/0.02；分批及跨日只收一次最低佣金，撤单不再收费；
20 元分红在除权后作为应收，支付不再增加权益。极端 Decimal/整数溢出、公司行动
批次后项失败、资金不足的 target 候选均验证无半状态。

正式合并前审查修复两项：关闭公司行动原先晚于 close 市场/定时回调，且兼容
no-op 可能漏终点登记/支付；现改为末笔成交后、关闭回调前处理一次，D06 必须
转发真实 `check_session_end` 后置检查，漏转发默认 `CAPABILITY_UNAVAILABLE`；
旧时钟隔离 fixture 显式承认无会计业务，清仓后漏支付也拒绝。旧 driver 的三个
独立关闭回归均实际失败，修复后通过。另修复 FIFO 初扣税分摊产生分以下现金：
累计应扣调整按指定精度舍入再减已收；独立 0.30 元分红案例由旧算式实际输出
0.033333333333 首次累计税，改为 0.03，最终总税 0.06。预估多次不修改费用或
账户 revision；候选过期/跨账户、跨日最低佣金及撤单、零碎/溢出批次回滚、
raw/stale/缺价和成本全卖清零的原有回归继续通过。

## 保留项与依赖

D06 仍须实现订单生命周期/target 解析、matcher 合法可支付数量和原子订单候选
提交，消费公司行动的取消通知与 effects；不得维护第二份资金池。D10/D11/D13
绑定及结果消费不在 D09 范围。D02 未确认历史规则/费用继续 `RULE_UNAVAILABLE`；
D04 正式市场、公开时间、公司行动、资金可用及税事实映射尚未全启用。股份比例
需逐份额可整除；零碎分配/补偿、未知扣缴事实、可交易配股权等明确拒绝。

旧 Python `accounting.py` / `dividends.py` 仍有 runtime、production_runtime、
settlement、session_matching、result_models 等消费者，未提前删除活的依赖；
新 Rust 模块无旧算法调用或 fallback，最终引用切断/物理退役交 D12/D13/D17/D18。
`new_engine_code_ready` 仅指 D09；`legacy_execution_removed`、`production_switched`、
`runtime_cleanup_done` 均未完成。未访问生产/供应商，未部署/清理/改凭据或安全配置，
未修改采集器、CurrentStore、S2 公共 UI；代码及隔离通过不能称生产验收。

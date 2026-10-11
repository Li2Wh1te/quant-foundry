# S3-D07 RESULT

已从 main `d4becb53bc2393412cb9550530827bc2bbb97b36` 完成
`bar_next_interval_v1`。模型与接入说明见 [README.md](README.md)，实现见
[bar.rs](bar.rs)，独立手算输入见 `tests/bar_matching/cases.json`。

2026-10-11 从 PR #166 远端 head
`043f02c1086978a35323cea09f68586e7f8b84d2` 重新完成完整复核。旧复核会话的
未推送工作区不可取得；本次没有将远端之外的旧改动视为已恢复。

- 区间资格、同 close 多标的屏障、before_open 与次会话 DAY 通过真实
  D03/D06/D09 验证。六种 Bar 频率、chunk 1/2/17、来源置换成交语义一致，
  同频率完整结果记录相同。
- 开盘优价、限价触及、买卖对称、不利滑点一次取 tick、限价/OHLC 边界截断、
  共享买卖参与量、零量/停牌/价格限制、重复 Bar 及缺项拒绝已实现。ETF 用真实
  订单/账户验证其 milli-CNY tick、申报整手与整数部分成交；规则是具名合成输入。
- 预算每次预览此前已选择的候选成交；真实账户累计费用，跳空案例成交 10+7
  份且现金为 8。后序预算/提交失败不留下半批账户、订单或费用变化。
- 复核修复小额卖出在提交时因最低佣金侵占冻结现金而使运行失败的问题。
  `candidate_allowance_after` 携带实际时间/ID，买卖均通过唯一 D09 的实际
  成交计划核验。1 份 × 0.1 的卖出不足以支付 1 元佣金时保留余单及共享量；
  后序候选只消费已选择的前序净卖款及累计费用。未知事实继续拒绝整批。
  买先卖后与卖先买后、未触及前序卖单、小额卖出连续拒绝、整手买入产生的
  137 份零股卖出按 100+37 部分成交、非法/重复成交 ID 均增加独立回归。
- 添加一次有界 `ResultRecord::ExecutionModel` 及同步 wire 类型；driver 核对
  同一 RunConfig 的模型/频率/参与率/滑点，初始化异常也保存模型假设。
  D06 复用同一账户临时预览及状态机，补充同状态未成交原因变化的原子
  更新和无新成交的部分单检查，没有第二资金池/状态机/费用累计器。
  新增模型说明在 initialize 前的零记录信用/少一字节拒绝、精确单条批次
  字节信用成功及写入后确认中断保留说明的回归。

实际云端隔离检查全部通过（Rust 1.90.0，Python 3.12.2）：

```sh
cd engine
cargo test -p qf-core --locked --test bar_matching --test numeric
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cd ..
QF_S3_PYTHON=python3.12 bash scripts/test_s3_engine.sh
```

本次完整脚本通过，最终 Rust 工作区 199 个测试通过，其中 Bar 30 个；价格表
含 20 个独立预期，包括零滑点、满额滑点及限价/OHLC 截断不产生有利价格。
完整脚本还通过 public_contract、生成合同/版本检查、18 个仓库共享测试、
Python 3.12.2 wheel 构建及独立安装后 29 个 SDK 测试。生产数据、SDK 策略执行
或 B1–B4 全量基准未在这里运行。精确最终 head 和合并提交的 S3/Validate
CI 结果及链接记录于 [PR #166](https://github.com/Li2Wh1te/quant-foundry/pull/166)
和交付回传；未触发的工作流不计为通过。

正式历史规则/费用、原价/量/状态/公司行动映射缺项继续拒绝。D04/D05 正式数据
映射缺口仍开放，代码与合成回归完成不等于生产业务验收。D04 正式接入、
D10 执行绑定、D11/D13 结果摘要消费及生产联调仍开放；合成测试不启用生产能力。
旧 Python `bar_matching.py`/`execution.py` 仍被 runtime 调用，未提前删除活依赖，
最终切断留给 D12/D13/D17。没有改变 D05 视图/指标、CurrentStore、采集器、公共
UI、来源开关、凭据或安全配置，没有生产部署/清理或上传原 ZIP。

状态：D07 `new_engine_code_ready=true`（代码与隔离业务）；
`legacy_execution_removed=false`、`production_switched=false`、
`runtime_cleanup_done=false`。本次仅完成 D07；后续任务包由父线程读取并派发。

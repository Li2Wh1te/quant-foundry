# S3-D07 RESULT

已从 main `d4becb53bc2393412cb9550530827bc2bbb97b36` 完成
`bar_next_interval_v1`。模型与接入说明见 [README.md](README.md)，实现见
[bar.rs](bar.rs)，独立手算输入见 `tests/bar_matching/cases.json`。

- 区间资格、同 close 多标的屏障、before_open 与次会话 DAY 通过真实
  D03/D06/D09 验证。六种 Bar 频率、chunk 1/2/17、来源置换成交语义一致，
  同频率完整结果记录相同。
- 开盘优价、限价触及、买卖对称、不利滑点一次取 tick、限价/OHLC 边界截断、
  共享买卖参与量、零量/停牌/价格限制、重复 Bar 及缺项拒绝已实现。ETF 用真实
  订单/账户验证其 milli-CNY tick、申报整手与整数部分成交；规则是具名合成输入。
- 预算每次预览此前已选择的候选成交；真实账户累计费用，跳空案例成交 10+7
  份且现金为 8。后序预算/提交失败不留下半批账户、订单或费用变化。
- 添加一次有界 `ResultRecord::ExecutionModel` 及同步 wire 类型；driver 核对
  同一 RunConfig 的模型/频率/参与率/滑点。D06 仅补同状态未成交原因变化的原子
  更新和无新成交的部分单检查，没有第二资金池/状态机/费用累计器。

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

最终 Rust 工作区 190 个测试通过，其中 Bar 21 个；价格表含 14 个独立预期。
完整脚本还通过 public_contract、生成合同/版本检查、18 个仓库共享测试、
Python 3.12 wheel 构建及独立安装后 29 个 SDK 测试。最后增加的 ETF 回归已
再次通过完整 Rust 工作区与 clippy；生产数据、SDK 策略执行或 B1–B4 全量基准
未在这里运行。草稿 PR 的精确 head CI 及链接由交付回传引用现有自动检查。

正式历史规则/费用、原价/量/状态/公司行动映射缺项继续拒绝。D04 正式接入、
D10 执行绑定、D11/D13 结果摘要消费及生产联调仍开放；合成测试不启用生产能力。
旧 Python `bar_matching.py`/`execution.py` 仍被 runtime 调用，未提前删除活依赖，
最终切断留给 D12/D13/D17。没有改变 D05 视图/指标、CurrentStore、采集器、公共
UI、来源开关、凭据或安全配置，没有生产部署/清理或上传原 ZIP。

状态：D07 `new_engine_code_ready=true`（代码与隔离业务）；
`legacy_execution_removed=false`、`production_switched=false`、
`runtime_cleanup_done=false`。父线程审查协调合并。

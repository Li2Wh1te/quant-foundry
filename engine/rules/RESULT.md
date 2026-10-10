# S3-D02 实施结果

基线：最新 `main` `b39a3e7317eb47804a1301b1d56738d07dfc62c5`，推送前再次 fetch 确认未变。仅本会话实施 D02，没有并行工程任务。

交付：`qf-core::rules/{date,market,catalog,fees,dividends}`；D13 只读账户佣金适配；有界 native 佣金验证/费用合成；同步生成共享 Python 契约。[模块说明](README.md)及[官方正文、有效范围与缺口](SOURCES.md)包含实际行为。D01 `FeeConfig`、精确价量与 `QfError` 保持同一契约；补充经手费分类、明确账户舍入与执行范围。

实际本地验证：

| 命令 | 结果 |
| --- | --- |
| `bash scripts/test_s3_engine.sh`（锁定 Rust 1.90.0、Python 3.12.2） | fmt、clippy `-D warnings`、workspace Rust 37 个测试、独立核心公开契约、生成契约/版本检查、仓库 18 个测试、release wheel 构建及独立安装 wheel 17 个测试全部通过。 |
| `cargo test -p qf-core --locked`、`cargo clippy --workspace --all-targets --locked -- -D warnings` | 最后完善边界夹具后再次通过；37 个 Rust 测试，无警告。 |
| `uv run --locked python -m pytest -q tests/test_account_workspace.py tests/test_backtesting_account_profiles.py tests/test_backtesting_account_profile_storage.py tests/test_strategy_storage.py tests/test_backtesting_result_api.py tests/test_backtesting_pagination.py` | 隔离测试配置，78 passed。 |
| `python3 scripts/generate_s3_contracts.py --check`、`git diff --check` | 通过。有效日期 JSON `from` 保留原键，通过 functional TypedDict 避免 Python 关键字语法错误；已实际重建安装 wheel 验证。 |

定向自复核：按官方原文核对 2026 竞价规则、数量/回转/价格/风险边界；手算部分成交累计最低佣金、跨日税率、舍入、包含项；核对缺项不补零、来源不串用、费用失败不改状态、错误跨 Python 保留范围。账户适配仅读配置，未导入旧费用计算；保护回归确认原账户/策略/历史消费者保留。

未决依据：2026-07-06 前市场规则、北风险/退市另行启用日、部分 ETF 及深北证管费用历史覆盖，另有未核投资者/ETF/北股票分红税与月末划档情形。全部明确拒绝，不能把本结果称为沪深北/ETF全历史已支持。当前完整官方四项费用组合仅落在沪股票核验日观测，**不证明该周六可交易**。真实会话/行情/状态、订单撮合/账户扣缴执行及生产接线属于后续所属模块，未在本包冒充实现。

未删除旧 `backtesting/fees.py`：生产旧执行及共享账户仍引用，D13 迁出/D06–D09 切换未完成；D02 新计算不依赖它。没有业务数据迁移/修改、生产部署/清理、供应商请求、R01/S2 操作或原任务包上传。

草稿 PR 和精确提交的远端 CI 在 GitHub 保留；本会话验证后回报父协调者，不合并。

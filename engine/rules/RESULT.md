# S3-D02 实施结果

基线：最新 `main` `b39a3e7317eb47804a1301b1d56738d07dfc62c5`，推送前再次 fetch 确认未变。仅本会话实施 D02，没有并行工程任务。

交付：`qf-core::rules/{date,market,catalog,fees,dividends}`；D13 只读账户佣金适配；有界 native 佣金验证/费用合成；同步生成共享 Python 契约。[模块说明](README.md)及[官方正文、有效范围与缺口](SOURCES.md)包含实际行为。D01 `FeeConfig`、精确价量与 `QfError` 保持同一契约；补充经手费分类、明确账户舍入与执行范围。

实际本地验证：

| 命令 | 结果 |
| --- | --- |
| `bash scripts/test_s3_engine.sh`（锁定 Rust 1.90.0、Python 3.12.2） | fmt、clippy `-D warnings`、workspace Rust 41 个测试、独立核心公开契约、生成契约/版本检查、仓库 18 个测试、release wheel 构建及独立安装 wheel 17 个测试全部通过。 |
| `uv run --locked python -m pytest -q tests/test_account_workspace.py tests/test_backtesting_account_profiles.py tests/test_backtesting_account_profile_storage.py tests/test_strategy_storage.py tests/test_backtesting_result_api.py tests/test_backtesting_pagination.py` | 隔离测试配置，78 passed。 |
| `python3 scripts/generate_s3_contracts.py --check`、`git diff --check` | 通过。有效日期 JSON `from` 保留原键，通过 functional TypedDict 避免 Python 关键字语法错误；已实际重建安装 wheel 验证。 |

定向自复核：按官方原文核对 2023 深市和 2026 沪深北竞价规则、数量/回转/价格/风险边界；区分发布日期与首批实际上市日；沿 2015/2018/2021/2023 通知、整合继承和完整收费表核对费用连续范围。ETF 二级过户及北股票监管费不适用是明示的收费项目范围判断，有完整原表与禁止扩项的原通知支持，不把缺失费率当零。手算部分成交累计最低佣金、跨日税率、舍入、包含项；核对缺项不补零、来源不串用、费用失败不改状态、错误跨 Python 保留范围。账户适配仅读配置，未导入旧费用计算；保护回归确认原账户/策略/历史消费者保留。

本次修复消除核验日单点模型。完整四项费用可合成范围至 2026-10-10：沪深股票从 2022-07-01、沪 ETF 从 2023-11-24、深 ETF 和北股票从 2025-05-01 起。交易规则与完整费用交集：沪深北正常状态股票及沪深六类 ETF 从 2026-07-06 起，深股票另从 2023-04-10、深 ETF 从 2025-05-01 起。2026-07-07 联合边界测试实际选择官方规则及官方费用、使用明确合成当日事实，涵盖全部上述类型；2023 降费前后及 2026 主板风险警示比例变化也有独立预期。日期和费用目录仍不代替真实交易会话、状态及行情证明。

未决依据：沪 2023 正文附件和完整可解析同版 PDF 受阻，沪/北 2026-07-06 前、深 2023-04-10 前市场规则仍未核；沪深 ETF 更早过户范围、北更早经手/监管范围、北风险/退市另行启用日，以及投资者/ETF/北股票分红税和月末划档仍有缺项。对应日期或条件全部明确拒绝，具体恢复资料和已尝试路径见 SOURCES；未缩减已批准品种/历史范围，也不能将本结果称为全历史验收完成。真实会话/行情/状态、订单撮合/账户扣缴执行及生产接线属于后续所属模块，未在本包冒充实现。

未删除旧 `backtesting/fees.py`：生产旧执行及共享账户仍引用，D13 迁出/D06–D09 切换未完成；D02 新计算不依赖它。没有业务数据迁移/修改、生产部署/清理、供应商请求、R01/S2 操作或原任务包上传。

草稿 PR 和精确提交的远端 CI 在 GitHub 保留；本会话验证后回报父协调者，不合并。

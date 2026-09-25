# LF-D02 逐入口小样本验收

范围：71 个基线入口，含 60 个业务入口、7 个来源辅助入口、3 个导入通道和 1 个空状态。
样本均为按已知契约构造的合成输入，不冒充供应商原件；未连接生产或供应商。
业务路径：隔离 PostgreSQL 中的原生观察/本地表 → `NativeSources.iter_entry` → `normalize` → `run_entry` → 当前 PostgreSQL 目录与 Parquet → DuckDB 支持的 `read_object` 读回。
来源辅助、导入通道与空状态按真实 `run_entry` 处置验证，不创建伪业务数据集。

| 入口 | 来源 / 原生类型 | 处置 | 独立预期值或目标 | 本地结果 |
|---|---|---|---|---|
| E01 | tonghuashun / `stock_daily_dump` | 导入通道 | `ingestion_channel`；目标 `E50`；无业务数据集 | 通过 |
| E02 | tonghuashun / `stock_recent_dump` | 导入通道 | `ingestion_channel`；目标 `E50`；无业务数据集 | 通过 |
| E03 | tonghuashun / `stock_actions_dump` | 导入通道 | `ingestion_channel`；目标 `E22`；无业务数据集 | 通过 |
| E04 | tonghuashun / `fund_news` | 业务 | `FUND.SH` / `n1`；`reported_title` = `标题` | 通过 |
| E05 | tonghuashun / `anomaly_stock` | 业务 | `000001.SZ` / `current`；`narratives[0].reported_analysis` = `内容` | 通过 |
| E06 | tonghuashun / `rank_trend` | 业务 | `000001.SZ` / `2026-01-02`；`reported_rank` = `1` | 通过 |
| E07 | tonghuashun / `fund_holdings` | 业务 | `FUND.SH` / `2026-01-03`；`members[0].reported_hold_ratio` = `1.2` | 通过 |
| E08 | tonghuashun / `fund_stock_history` | 业务 | `FUND.SH` / `2026-01-03:quarter`；`members[0].reported_asset_type` = `stock` | 通过 |
| E09 | tonghuashun / `fund_bond_history` | 业务 | `FUND.SH` / `2026-01-03:quarter`；`members[0].reported_asset_type` = `bond` | 通过 |
| E10 | tonghuashun / `stock_indicators` | 业务 | `000001.SZ` / `2026Q1`；`abilities[0].indicators[0].reported_value` = `10.5` | 通过 |
| E11 | tonghuashun / `fund_financial_indicators` | 业务 | `FUND.SH` / `2026-01-03:2026-01-02`；`reported_asset_nav` = `123456789.012345` | 通过 |
| E12 | tonghuashun / `fund_income` | 业务 | `FUND.SH` / `2026-01-03:2026-01-02`；`reported_net_profit` = `-123.456789` | 通过 |
| E13 | tonghuashun / `fund_balance` | 业务 | `FUND.SH` / `2026-01-03:2026-01-02`；`reported_total_assets` = `987654.321` | 通过 |
| E14 | tonghuashun / `stock_income` | 业务 | `000001.SZ` / `2026-01-03:Q1`；`reported_basic_eps` = `1.234567` | 通过 |
| E15 | tonghuashun / `stock_balance` | 业务 | `000001.SZ` / `2026-01-03:Q1`；`reported_assets_total` = `765432.1` | 通过 |
| E16 | tonghuashun / `stock_cash_flow` | 业务 | `000001.SZ` / `2026-01-03:Q1`；`reported_act_cash_flow_net` = `-12345.67` | 通过 |
| E17 | tonghuashun / `fund_allocation` | 业务 | `FUND.SH` / `2026-01-02`；`members[0].reported_stock_ratio_pct` = `80` | 通过 |
| E18 | tonghuashun / `fund_industry` | 业务 | `FUND.SH` / `2026Q1`；`members[0].reported_industry_name` = `科技` | 通过 |
| E19 | tonghuashun / `fund_holders` | 业务 | `FUND.SH` / `2026-01-02`；`members[0].reported_holder_amount` = `10` | 通过 |
| E20 | tonghuashun / `fund_top_holders` | 业务 | `FUND.SH` / `2026-01-02`；`members[0].reported_hold_share` = `100` | 通过 |
| E21 | tonghuashun / `fund_dividends` | 业务 | `FUND.SH` / `2026-01-02`；`members[0].reported_per_ten_cash_before_tax` = `1.2` | 通过 |
| E22 | tonghuashun / `stock_actions` | 业务 | `000001.SZ` / `2026-01-02`；`members[0].reported_dividend_per_share` = `0.2` | 通过 |
| E23 | tonghuashun / `fund_manager_performance` | 业务 | `MGR1.month` / `2026-01-02`；`reported_manager_return_pct` = `1.1` | 通过 |
| E24 | tonghuashun / `fund_manager_style` | 业务 | `MGR1` / `current`；`reported_investment_idea` = `价值` | 通过 |
| E25 | tonghuashun / `fund_returns` | 业务 | `FUND.SH` / `current`；`periods[0].reported_return` = `1` | 通过 |
| E26 | tonghuashun / `fund_drawdowns` | 业务 | `FUND.SH` / `current`；`periods[0].reported_drawdown` = `-2` | 通过 |
| E27 | tonghuashun / `fund_performance_history` | 业务 | `FUND.SH` / `2026-01-02`；`reported_rsi_pct` = `50` | 通过 |
| E28 | tonghuashun / `dragon_tiger` | 业务 | `dragon_tiger:2026-01-02.all` / `2026-01-02`；`stocks[0].source_code` = `000001.SZ` | 通过 |
| E29 | tonghuashun / `stock_auction` | 业务 | `000001.SZ` / `current`；`reported_auction_price` = `10.01` | 通过 |
| E30 | tonghuashun / `auction_benchmark` | 业务 | `auction_benchmark:2026-01-02` / `2026-01-02`；`members[0].reported_auction_pct` = `1.2` | 通过 |
| E31 | tonghuashun / `limit_up` | 业务 | `limit_up:2026-01-02` / `2026-01-02`；`members[0].reported_seal_money` = `80` | 通过 |
| E32 | tonghuashun / `limit_down` | 业务 | `limit_down:2026-01-02` / `2026-01-02`；`members[0].reported_last_price` = `9` | 通过 |
| E33 | tonghuashun / `limit_break` | 业务 | `limit_break:2026-01-02` / `2026-01-02`；`members[0].reported_open_times` = `2` | 通过 |
| E34 | tonghuashun / `anomaly_list` | 业务 | `anomaly_list:market` / `current`；`members[0].reported_tag` = `异动` | 通过 |
| E35 | tonghuashun / `limit_ladder` | 业务 | `limit_ladder:market` / `2026-01-02`；`reported_groups.seven_over` = `[]` | 通过 |
| E36 | tonghuashun / `stock_quote` | 业务 | `000001.SZ` / `current`；`reported_last_price` = `10.5` | 通过 |
| E37 | tonghuashun / `etf_quote` | 业务 | `000001.SZ` / `current`；`reported_last_price` = `10.5` | 通过 |
| E38 | tonghuashun / `index_quote` | 业务 | `000001.SZ` / `current`；`reported_last_price` = `10.5` | 通过 |
| E39 | tonghuashun / `stock_valuation` | 业务 | `000001.SZ` / `current`；`reported_pe_ttm` = `12.3` | 通过 |
| E40 | tonghuashun / `tickers` | 业务 | `000001.SZ` / `current`；`name` = `示例` | 通过 |
| E41 | tonghuashun / `fund_company` | 业务 | `COMP1` / `current`；`name` = `公司` | 通过 |
| E42 | tonghuashun / `fund_manager` | 业务 | `MGR1` / `current`；`name` = `经理` | 通过 |
| E43 | tonghuashun / `fund_profile` | 业务 | `FUND.SH` / `current`；`name` = `基金` | 通过 |
| E44 | tonghuashun / `fund_nav` | 业务 | `FUND.SH` / `2026-01-02`；`reported_unit_nav` = `1.2345` | 通过 |
| E45 | tonghuashun / `hot_list` | 业务 | `hot_list:day` / `current`；`members[0].reported_heat` = `9.9` | 通过 |
| E46 | tonghuashun / `skyrocket` | 业务 | `skyrocket:hour` / `current`；`members[0].reported_heat` = `9.9` | 通过 |
| E47 | tonghuashun / `hot_history` | 业务 | `hot_history:2026-01-02` / `2026-01-02`；`members[0].reported_rank` = `1` | 通过 |
| E48 | tonghuashun / `fund_quota_summary` | 业务 | `QDII` / `current`；`reported_groups[0].reported_total_text` = `100` | 通过 |
| E49 | tonghuashun / `fund_quota_list` | 业务 | `QDII` / `current`；`reported_groups[0].subcategories[0].funds[0].reported_quota_text` = `100` | 通过 |
| E50 | tonghuashun / `stock_daily` | 业务 | `000001.SZ` / `2026-01-02`；`reported_close` = `10.5` | 通过 |
| E51 | tonghuashun / `etf_daily` | 业务 | `510300.SH` / `2026-01-02`；`reported_close` = `10.5` | 通过 |
| E52 | tonghuashun / `index_daily` | 业务 | `000300.SH` / `2026-01-02`；`reported_close` = `10.5` | 通过 |
| E53 | tonghuashun / `fund_offerings` | 业务 | `active` / `current`；`members[0].source_code` = `FUND.SH` | 通过 |
| E54 | tonghuashun / `fund_manager_experience` | 业务 | `MGR1` / `FUND.SH:2025-01-01`；`end_text` = `2026-01-01` | 通过 |
| E55 | tonghuashun / `calendar` | 业务 | `provider-calendar` / `2026-01-02`；`calendar_date` = `2026-01-02` | 通过 |
| E56 | tonghuashun / `index_catalog` | 业务 | `cn_concept` / `current`；`members[0].name` = `概念` | 通过 |
| E57 | tonghuashun / `index_constituents` | 业务 | `000300.SH` / `current`；`members[0].name` = `成分` | 通过 |
| E58 | tushare / `etf_code_mapping_audits` | 来源辅助 | `source_support`；目标 `E67`；无业务数据集 | 通过 |
| E59 | tushare / `etf_daily_revision_audits` | 来源辅助 | `source_support`；目标 `E70`；无业务数据集 | 通过 |
| E60 | tushare / `corporate_action_source_facts` | 来源辅助 | `source_support`；目标 `E61`；无业务数据集 | 通过 |
| E61 | tushare / `corporate_action_facts` | 业务 | `inst-1` / `event:8dfca90436aa774d965e2443b5dd56b5f0de93d8678c4eb1b828cb86c38b8f63`；`cash_amount_per_unit` = `0.2` | 通过 |
| E62 | tushare / `corporate_action_coverage_facts` | 来源辅助 | `source_support`；目标 `E61`；无业务数据集 | 通过 |
| E63 | tushare / `trading_status_source_facts` | 来源辅助 | `source_support`；目标 `E64`；无业务数据集 | 通过 |
| E64 | tushare / `trading_status_facts` | 业务 | `000001.SZ` / `2026-01-02:suspend`；`status` = `normal` | 通过 |
| E65 | tushare / `trading_status_coverage_facts` | 来源辅助 | `source_support`；目标 `E64`；无业务数据集 | 通过 |
| E66 | tushare / `trading_status_revision_audits` | 来源辅助 | `source_support`；目标 `E64`；无业务数据集 | 通过 |
| E67 | tushare / `etf_directory` | 业务 | `510300.SH` / `510300.SH`；`name` = `ETF` | 通过 |
| E68 | tushare / `exchange_calendar` | 业务 | `SSE` / `2026-01-02`；`is_open` = `True` | 通过 |
| E69 | tushare / `etf_adjustment_factors` | 业务 | `510300.SH` / `2026-01-02`；`factor` = `1.001` | 通过 |
| E70 | tushare / `etf_daily` | 业务 | `510300.SH` / `2026-01-02`；`turnover_thousand_yuan` = `4100` | 通过 |
| E71 | foundation / `empty_local_scope` | 空状态 | `remove_operational_pseudodataset`；目标 `无`；无业务数据集 | 通过 |

## 额外场景与边界

- 同花顺真实观察增量依赖：新值更新、后到的旧观察不覆盖新值、重复处理不写业务文件。
- 基金持仓历史完整报告：非法成员限制整份报告；完整修复后恢复读回。
- 原生刷新失败：保留目标限制；新的有效观察与定向重试解除限制。
- 指数成分：明确空集合可发布；缺少 `item` 属于错误并限制读取，修复后恢复。
- Tushare ETF 日线：本地表更正可读；非法 OHLC 被限制，修复后恢复；测试禁止供应商 HTTP。
- 代表性字段限制已读回验证：报告覆盖范围、未归一币种、复权锚点、交易日开闭市语义和缺失前收盘价。

本地验证：414 项相关存储回归通过（含本文件对应的 68 项验收）；定向 `retry` 的两条修复场景在调整后另行复测通过。Linux 测试容器、隔离 PostgreSQL 17、真实 Parquet 与 DuckDB；本地 Docker overlay 使用仓库现有的文件系统探测注入，因此此处不宣称文件系统持久性认证。
CI 最终结果以 [PR #117](https://github.com/Li2Wh1te/quant-foundry/pull/117) 的最新检查记录为准，合并前须全部通过。
B05 千万条 tick 测试按用户要求不执行，也不计为通过。公共 HTTP/UI 属于 D04；生产部署、迁移和真实数据验收由维护者负责。
缺少可追溯的供应商原件时，这些合成样本只能证明当前已声明契约与代码路径的正确性；不能证明供应商全部字段形态。

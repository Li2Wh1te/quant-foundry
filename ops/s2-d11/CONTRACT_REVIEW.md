# D11 契约依据与工程接线 · 2026-10-13

本次核对的公开原始说明和现有代码没有建立 E69→E70 的基金累计变换许可，**生产 pre/post 继续 restricted**。工程链已另行完成：明示人工隔离契约经过默认应用、真实隔离正式存储、共同窗口和固定锚点分页验证。人工契约不能授权生产 E69。

只读官方说明页及现有源码；未执行文档内 API 示例、供应商数据请求、真实市场/用户数据读取复制、采集、生产访问或数据修改。

| 官方依据 | 支持的结论 | 未取得的承诺 |
| --- | --- | --- |
| [fund_adj，doc199](https://tushare.pro/document/2?doc_id=199) | 基金因子观察，证券代码、交易日期和因子 | 基金累计式、OHLC适用关系、缺日有效性或原始归一化基准 |
| [fund_daily，doc127](https://tushare.pro/document/2?doc_id=127) | ETF每日成交OHLC，元/手/千元 | 与fund_adj组合后的价格政策 |
| [复权行情，doc146](https://tushare.pro/document/2?doc_id=146) | 明示乘当日因子及前复权除查询端因子的算法 | 该说明限定A股，不能移为基金E69契约 |
| [pro_bar，doc109](https://tushare.pro/document/2?doc_id=109) | 基金行情分类可表达，复权参数另限定股票 | FD分支按股票算法应用fund_adj的承诺 |
| [FAQ，doc122](https://tushare.pro/document/1?doc_id=122)、[更新/权限，doc108](https://tushare.pro/document/1?doc_id=108) | 通用查询终点说明和更新节奏 | 基金专属累计模型或本地逐日覆盖证明 |

这些是所核对页面的范围判断，不宣称所有可能存在的来源材料均无相关规定；未使用第三方转载/摘要或文档数值样例作为正式证据。

仓库的 `AdjustmentFactor` 保留正十进制原值和 `tushare_fund_adj` 标签，原适配固定 `anchor_date=None`，限制明确未派生价格、锚点未核验。E69/E70绑定保持原default representation及layout字段解析；人工契约使用不同的 variant 和业务键，不能由默认来源补齐。SDK1.4.29的FD分支只取fund_daily；股票分支公式不能授权基金。现有可信身份窗口继续约束同一标的的价格/因子映射。

| 项目 | 工程状态 | 生产恢复条件 |
| --- | --- | --- |
| 来源政策解析 | `policies.py` 代码声明，HTTP不能传公式、来源、representation或锚点 | 获准基金来源契约明确价格输入、累计关系、OHLC适用范围与日期有效性 |
| 精确日期因子覆盖 | 每根实际必要日线同日匹配；缺日拒绝，不前填；共同预算内批量预取 | 先建立所选源的日期有效策略，再逐请求核查真实窗口；分段有效需独立依据 |
| pre查询锚点 | 固定as_of内最后被正式日历证明的open日期，要求精确同日因子；未知日期拒绝；续页/视口保持同锚点 | 累计关系和有效对应因子。**原始归一化日期未知不会单独阻断pre比值模式** |
| post原始基准 | 独立明示日期/值和适用截止；缺失或撤销只关闭post | 来源规定的原始基准及适用范围，不能从第一行/值为1的样例推断 |
| 默认HTTP共同窗口 | 已接入D08唯一工厂/read_scope，价格+因子+身份+适用日历+政策；先逐日变换再完整桶分页，最终短锁复核 | 上述来源许可成立后新增明确获准生产政策；现有E69仍拒绝pre/post |
| 实际数据/发布 | 未执行，`live_verified=false`、`deployed=false` | 后续获授权真实窗口及成套发布验收 |

S3仍使用同一纯Decimal算术，公开/生效时点、研究身份、原始基准和执行约束均未放宽。S2显示当前修订数据，**S3历史公开时点未知不是当前浏览独立不可用理由**。

人工正例在 `backend/tests/test_s2_market_adjustment_http_contract.py`。服务器必须是 `environment=test`，并明示 `market_test_adjustment_contract=cumulative-exact-date-fixture@1`；价格/因子只读取 `s2-cumulative-exact-date-fixture-v1` 变体。其他环境配置拒绝，运行时再检查环境/声明；这些设置不进入部署配置、HTTP输入或新事实表。

用例通过默认create_app与真实私有PG/Parquet/DuckDB读取人工正式输入；只使用既有隔离DB依赖和受校验服务器配置，未注入成功provider、市场service、查询结果或HTTP响应。验证pre/post独立许可、日/周/月/年跨页同锚点、跨年完整/截断桶、512根有界读取、回放截止、缺日拒绝、原单位和量额、源/variant隔离、价格/因子/政策并发改变及共同容量边界。原先只在测试中组装窗口的重复脚手架已由这套真实默认HTTP用例替换。

UI证据另为明示模拟接口，不能当成正式后端贯通浏览器或生产来源语义证据。生产政策缺项、默认HTTP工程验证、模拟UI验证和真实联调分别报告于RESULT及PR。额外全量CI仅只读观察；没有再次取消、改凭据/路由或专门重跑。必要工程提交按现有规则触发检查，未更改全量workflow的触发或并发设置。

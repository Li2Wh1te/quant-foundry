# S2-D11 RESULT — 部分交付，复权政策仍缺依据

依赖 D10 已按父授权复核并合并：[PR183](https://github.com/Li2Wh1te/quant-foundry/pull/183)，merge `e6ab8ea745032328301cdbf6be96e3d0349bddd2`。精确 head `07f96039b66a2ad763d07d038911589c3f246c06` 的四条 CI 全通过，原 worker 失败及根因未定记录保留于 `ops/s2-d10/CI_FAILURE.json`。D11 从该 merge 建分支 `s2/d11-formal-adjustments`，仍在同一 Standard 会话，没有新 agent/session 或 fast。

**本包不是完整复权接入完成。** `code_ready=false`（完整 D11 默认 pre/post 正例及相关一致性接线未完成），`live_verified=false`，`deployed=false`。下列因子读取、UI 与公共算术已实现并隔离验证；草稿 PR 不合并、不部署。

已实现：

- 默认应用的有界 `/api/admin/market/instruments/{key}/factors`，复用 D08 唯一工厂、真实 DB、CurrentStore 请求准入、完整对象、精确 representation 与既有身份有效窗口。固定 start/end/as_of，默认512、最多2000，向更早日期的签名游标；Decimal 原文、升序、同名其他 variant 不替换、更新/质量变化使旧游标失效。不读取采集行情、供应商或执行资料。
- 因子原值表独立于 raw：日期范围、分页、重新读取、基准/截至/来源及未知覆盖说明，10,000条内存上限。网络同条件旧值标旧禁分页；日期/口径改变和 DATA_CHANGED/失效游标撤下；401/403 使用既有全页失效机制。原图表与自选功能保留，浏览器没有复权公式。
- BasisControls 通过 D10 插槽接入，键盘选择和说明/Escape可用，只有实际可用口径可选；pre/post 的 `mapping_implemented=false` 与缺项明示，没有暗中改选 raw。三尺寸截图复用既有深色图表布局、字体和控件。
- 抽出原 S3 `price.checked_mul(factor)?.div_rounded(anchor,12,HalfEven)` 为 `qf_core::data::adjustments::adjusted_price`，S3 原调用者使用同一函数；增加有界 native `adjust_prices_json` 与 S2 内部薄边界。S3 公开/生效时间、身份、原始锚点、覆盖、未来因子/执行限制均未放宽。纯数学示例与先调整日线再聚合、量额保持原值有独立 Decimal 对照。

必要事实与准确缺项：

- `backend/app/data_store/adapters/record_schemas.py:AdjustmentFactor` 只承诺来源因子原值，声明 `adjustment_anchor_unverified / adjusted_price_derivation_not_supported`；`record_adapters.py` 始终写入 `anchor_date=None`。S3 `ResearchBinding` 也明确保持 E69 因子公式／原始基准／公开时点未核验。现有资料不能证明 E69 的累计模型、逐日或分段有效覆盖及归一化基准。
- 未从字段名、首条/末条因子、同名代码、股票 SDK 公式或合成数值推断基金源语义，也未新造政策审批/事实表、改 R01 正式模型或注入成功 provider。已向父询问既有获准依据；当前没有新增可靠依据。本轮禁止真实供应商/行情/生产访问，不能为补证而访问它们。
- 因此 **11-01 默认 pre/post 正例未完成；11-02 复权固定锚点翻页、11-03 价格+因子共同复权窗口、11-05 截止内因子变换后周/月聚合未完成**。仅因子原值分页和 S3 已有严格拒绝/数学回归通过，不将其代替这些验收。
- 11-04 已验证 E51/其他序列拒绝及同名其他 variant 不替换；11-06 因子缺失/坏值/网络/改变不关闭独立 raw；11-07 共享算术同输入 Decimal 对照及 S3 严格研究回归通过。未声明生产行情或默认正式服务贯通浏览器。

实际检查（最终精确 head CI 以草稿 PR checks 为准）：

- 关联后端 D08/D09/D10/D11/S3 网关/聚合：102 passed、11 skipped；最后能力标记变更后 D08/D10/D11 34 passed，新增专项最终 **7 passed**。全部正式输入来自人工合成，真实私有 PG/Parquet/DuckDB，不注入成功查询结果。
- S3 研究 API／正式研究运行：39 passed，包含公开时间、原始锚点、过期/未来因子及跨有效边界保护。
- 锁定 Rust1.90：core 全部测试、fmt、clippy通过；安装的正确 SDK wheel **95 tests passed**（含2个新增数值对照），根29测试／版本检查通过。wheel只为隔离检查构建安装，实际发布打包仍交D13，没有请求时编译/安装。
- `pnpm --dir frontend test`：175 passed；最终 build通过。
- 模拟 Chromium：8组交互/失效检查，0浏览器错误、0旧详情价格/因子请求。[UI_RESULT.json](UI_RESULT.json)、[1440×900](screenshots/1440x900.png)、[1024×768](screenshots/1024x768.png)、[390px](screenshots/390px.png)。浏览器成功响应全为明示人工模拟，不能作为真实联调证据。

可交互模拟预览：

```sh
node ops/s2-d11/mock-api.mjs
QF_DEV_BACKEND_URL=http://127.0.0.1:8010 pnpm --dir frontend dev --host 127.0.0.1
# http://127.0.0.1:5173/admin/data/etf-basics/510300.SH?tab=factors
# 登录只输入合成测试 token；不读取真实账户或行情。
node ops/s2-d11/ui-check.mjs
```

保留原采集后端、`dataCollections` 共享客户端、自选、S2/S3/R01；旧浏览器复权/全历史算法已在 D10 删除，本包没有再建一套。无生产/供应商调用、真实数据读复制、部署、reset/rebuild、采集或数据写入端点。补齐获准政策事实后才能关闭上述 D11 缺项。

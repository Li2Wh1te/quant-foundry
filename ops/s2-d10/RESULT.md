# S2-D10 RESULT

当前状态：D10 自身实现、合成隔离验证及模拟 UI 已完成；组合入口窄修待父确认，因此当前 `code_ready=false`，`live_verified=false`，`deployed=false`。本包交草稿 PR，合并仍交父；没有部署、生产访问、供应商调用或真实用户/行情数据读取复制。

D10 最初从 D08 精确 head `055a654e2aae7e6b7557b8b77d5b370615c1015d` 开始，后安全快进到 PR181 merge `96af283c49b5809561e62ddb524aadbfc91a7275`。按父追加授权，复核 D09 精确 head `48768ed0a22a12178b40c3376388cd68122ca814` 的四条 CI、最终差异和评审状态后合并 PR182；merge 为 `253e12ac5ae1e56908aa68d54e56d67f67d983ab`。D10 基线现为该合并提交，没有把未提交 D10 改动带入 D09。

已实现：

- 默认应用 `/instruments/{instrument_key}/series` 与 `/bars(adjustment=none)`，复用 D08 唯一工厂、真实 DB、请求准入和 CurrentStore 质量检查。精确 namespace、真实 partitioner、完整 scalar objects、既有可信身份窗口、当前修订知识边界均保留。日图不读因子、账户/费用、worker、原采集表或供应商。
- 日/周/月/年完整桶先聚合后分页，固定 end/as_of，输出升序，游标向更早桶。共享 S3 实现只支持分钟周期，未复制它；本包补有测试的日历周期纯函数，供 D11 在确定变换后复用。缺已知会话、窗口/回放截断、未核验覆盖分别表示；缺整桶也不会消失成连续完整图。缺必要日线时量额未知，不补零/前填/推为停牌。
- 流式输入遵守 D08 累计 100,000 行 / 4 MiB（及 store 更小值）、4 MiB 输出、30 秒 deadline；此外累计文件/扫描预算保留。日历只读选定输出跨度，超预算拒绝，不给不完整空成功。
- 详情消费 D09 稳定身份 Context，资料/价格/口径分别读取，512 根起步、按需历史、显式日期范围、有界 10,000 根驻留。保留原图表库、十字线、缩放、指标、绘图、全屏、自选跳转和当前修订回放；数据/权限改变撤下窗口，网络旧数据明确标旧禁续页。截止/周期/口径改变的 effect 前帧也不显示旧价格。
- Decimal 原文保留到绘图边界。精确二进制有理数误差检查只供显示；tooltip/原值表/复制仍用精确文本。超精度量额不影响独立 OHLC；已知缺日时连续指标/量能副图暂停。业务日期使用 ISO 和 UTC 绘图坐标，避免浏览器时区漂移。
- D11 出口：`basisSlot.ts` 的 `BasisControlsProps`、`FactorTableProps`；本包只提供 none，前/后复权仍明确受限。旧浏览器复权/全历史聚合 `buildSeries` 及其重复算法断言已删除；`dataCollections` 公共 API、采集后端和自选消费者保留给 D12 按引用退出。

E70 口径依据（显示契约推断，未改 R01 正式模型）：[官方 fund_daily 文档](https://tushare.pro/document/2?doc_id=127) 将其说明为每日收盘后成交价格，声明元/手/千元；仓库采集及正式适配逐值保留这些字段。本环境锁定 SDK **1.4.29** 的 `pro_bar` 基金日线分支直接返回 `fund_daily`，没有价格复权公式，函数源码 SHA256 `a8d6144cf9e8603c9b4f5ee3c7d8daba59990f16acea78e8b18071907bdd713d`。据此 D10 声明 `tushare-fund-daily-unadjusted@1`，只表示该固定本地成交价视图不施加复权；底层仍是 `provider_reported`，不把 E51 当 raw，也不授予回测执行能力。官方文档在本轮范围收窄前读取；恢复后仅核验已安装 SDK 源码，没有真实行情 API 调用。

实际检查：

| 检查 | 实际结果 |
| --- | --- |
| 默认应用及关联后端 | **116 passed, 13 skipped**：D08、D09、D10、S3 网关、原自选和运营总览；合成正式输入经真实私有 PG/Parquet/DuckDB，不注入成功 provider/HTTP 结果。13 跳过需要 Rust IPC/安装 SDK，未算通过 |
| 前端 baseline | **171 passed**，包括 8 项 D10 绘图/窗口测试 |
| 前端 TypeScript/build | **通过**；现有大 bundle 提示仍在 |
| 根测试 / release check | **29 passed**；版本一致 |
| 模拟 Chromium | **12 组交互与限制检查、3 张截图**；[UI_RESULT.json](UI_RESULT.json)。三页、稳定键、回放延迟/取消、资料失败、网络标旧、DATA_CHANGED、401/403 及窄屏均验证，无旧详情价格/因子请求和 page error |

截图：[1440×900](screenshots/1440x900.png)、[1024×768](screenshots/1024x768.png)、[390px](screenshots/390px.png)。浏览器成功数据全部来自明确标注的人工夹具/模拟接口，不能替代“默认正式服务→浏览器”的真实联调。test-only overlay 探针只验证隔离存储机制，不证明生产挂载。所选环境只沿用原会话，没有新 agent/开发会话或 fast。

可交互模拟预览：

```sh
node ops/s2-d10/mock-api.mjs
# 另一个终端，同一环境：
QF_DEV_BACKEND_URL=http://127.0.0.1:8010 pnpm --dir frontend dev --host 127.0.0.1
# http://127.0.0.1:5173/admin/data/etf-basics/510300.SH
# 登录仅输入合成测试 token；mock API 不读真实账户/市场资料。
# 浏览器自动检查：node ops/s2-d10/ui-check.mjs
```

准确缺项与公共接缝：

1. D09 的初始图表挂载仍要求 profile 成功，稳定身份有效但资料首次失败会挡住独立日图。本包已提交可审查的 [ENTRY_INTEGRATION.patch](ENTRY_INTEGRATION.patch)：仅使 Context.profile 可空、按可信身份挂载，保留 401/403 撤下和资料独立页。D09 文件有明确负责人，待父确认由谁应用；当前未应用，未把此场景算通过。已挂载图的资料失败则实际模拟通过。
2. D08 全局 `/capabilities` 的 raw 仍为旧的 unknown；D10 `/series` 已有明确显示解释和逐请求状态。该公共能力描述应由父/D08对齐；本包未修改工厂/DTO/client/bindings。共享 router 仅追加 D10 域注册。
3. 真实身份/正式数据联调、默认正式服务贯通浏览器、生产挂载、部署及全批退出验收均未执行。本轮用户明确禁止这些外部动作。D11 复权/因子接线、D12 共享旧路径退出不是本包完成项。

# S2-D09 RESULT

状态：`code_ready=true`（实现和隔离验证）；`live_verified=false`；`deployed=false`。
所有正例使用合成来源输入经原正式流水线、实际 PostgreSQL / Parquet / DuckDB 和默认应用工厂读取；没有注入成功 provider 或 HTTP 成功响应。没有生产访问、部署、采集、reset/rebuild 或 R01/S3 数据变更。

- 起点为 D08 精确 head `055a654e2aae7e6b7557b8b77d5b370615c1015d`。按父任务追加授权核验四条远程工作流成功及最终差异后，PR181 已合入 main：`96af283c49b5809561e62ddb524aadbfc91a7275`。D09 分支随后快进到同一 main；本包留 draft，合并仍交父统一处理。
- 已接通 `/instruments`、`/instruments/{instrument_key}`、`/overview`、`/calendar`、`/calendar/{exchange}/{date}`。只有自己的领域路由，复用 D08 的唯一 `get_market_service` / `read_scope`；main 仅增加本领域的 import 和注册。D08 一处“不存在 instruments 路由”的旧断言改为未知业务路径拒绝，以适配真实领域安装。没有修改公共工厂、DTO、客户端、D10/D11 文件或采集模块。
- 列表完整处理有界的选定 E67 元信息后再搜索、排序、计数、分页；1,107 个真实隔离正式对象验证末页搜索、三页与往返。没有逐标的价格/因子扫描。默认 namespace 不存在时拒绝，其他 variant 不替代、不伪造零。输入/输出、文件/扫描和时间预算保留。
- `etf-current:<source_code>` 是明确声明的来源目录资料键，只能查当前资料，不是跨代码稳定证券身份，也不授予历史图表能力。稳定 `instrument:<UUID>` 资料读取必须检查既有可信映射；旧代码链接通过 D08 唯一解析。E67 未提供的状态/指数/管理人/费率均为 null；上市状态筛选明确受限，没有从上市/终止日期猜状态。上市日期范围和读取时点均保留准确标签。
- 月历和日期详情独立读取 E68；正式 open/closed 和缺事实 unknown 严格区分，未来已记录日期可显示。没有周末推断或扫描日期自证完整；前后交易日缺依据为 null。资料、概览、日历各自失败互不阻断；401/403、质量/schema/数据改变撤下内容，只有同条件网络/超时可标旧保留，不能继续分页。
- 保留原表格/筛选/页大小/返回位置、原路由和自选契约。游标仅保存在当前挂载页面有界内存；URL/位置存储只有筛选、位置和显示代码。modal 焦点、日期方向键、交易所键盘筛选、窄屏滚动均验证；修正了资料页能力说明被容器裁掉的问题。

实际验证：

| 范围 | 结果 |
| --- | --- |
| 最终默认 D08 + D09 路径 | **26 passed**：D09 9 项，真实默认正例、缺事实、其他 variant、预算、游标/输出变化及旧仓储阻断 |
| 关联后端回归 | **253 passed, 13 skipped, 31 subtests passed**；覆盖正式目录/质量/覆盖、S2、S3 网关/身份/账户、自选和原运营总览。跳过项需要实际 Rust IPC/已安装 SDK 或专用 S3 CI，不计为通过 |
| `pnpm --dir frontend test` | **170 passed** |
| `pnpm --dir frontend build` | **通过**；现有大 bundle 提示仍在 |
| 根测试 / release check | **29 passed**；版本一致 |
| 实际浏览器 | **17 场景，13 截图，133 次正式 GET，0 旧市场请求**；[UI_RESULT.json](UI_RESULT.json)。全部成功数据来自正式 GET；失败/晚到控制仅用于前端反例 |

UI 截图：1440×900、1024×768、390×844 的列表/资料/月历，以及窄屏表格和能力/日期详情滚动补图在 [screenshots](screenshots/)。夹具名称明确显示“隔离夹具”，不代表生产当前数量或可用性。浏览器使用环境已有 Chromium；下载 CDN 被环境拒绝后未改变生产或全局浏览器安装。

可交互预览与复跑（所选环境已有临时 DB wrapper；其只启动/停止自己新建的回环测试库）：

```sh
bash /workspace/.setup/cloud-dev/run.sh --db -- python ops/s2-d09/preview.py
# http://127.0.0.1:5173/admin/data/etf-basics；使用 wrapper 的临时测试凭据
# Ctrl-C 关闭预览；--check 则运行完整浏览器用例、截图后关闭自己创建的进程/数据库
```

读取预览只经默认应用工厂，调度器关闭，旧市场/执行表访问被测试阻断；环境的 test-only overlay 探针不证明生产文件系统。临时凭据、数据库、正式夹具文件和巨量日志均未提交。

未决接缝：D10 从 `features/market-data/catalog/MarketInstrumentContext.ts` 的 `useMarketInstrument()` 消费 `instrumentKey`、`symbol`、`profile`、`resolution`，仍由其实现图表默认正式路径。App 只由 D09 维护一个 `MarketDetailEntry`；`tab=profile` 为独立正式资料入口，其余已有图表 tab 在可信身份检查后挂载原 D10 children。原 EtfDetailPage/etfData、dataCollections、后端旧市场/采集端点仍按 D10/D12 的迁移顺序保留，自选没有改动。本包不声称 K 线或全批退出已经完成。真实身份映射与正式数据联调、生产挂载、部署和全批组合验收交 R01 / S2-P01 / 父任务。

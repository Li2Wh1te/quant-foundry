# S2-D08 RESULT

- PR: [draft #181](https://github.com/Li2Wh1te/quant-foundry/pull/181)，合并交父检查。
- 代码提交：`0a5d6372223a20d9d494c35a7d9222fc1e521b9d`；基线为 PR180 合并后的 `dcdb424b13547da3e33d33d34c4e94382fde5a71`，保留全部有效 S3 修改。
- 状态：`code_ready=true`（D08 实现及隔离测试通过）；`live_verified=false`；`deployed=false`。精确最终 PR head 的 CI 交父核验。

默认 `create_app()` 的鉴权入口已提供 `/api/admin/market/capabilities` 和 `/instruments/resolve`。隔离 E67 正式输入通过原流水线写入真实 Parquet，默认工厂经 CurrentStore 与已有可信映射返回已有稳定身份，未替换成功 provider 或 HTTP 响应。源停用、E40 缺资料、因子/费用/worker 缺失不关闭独立 ETF 资料/日线观察读取。`reset_done` 下独立 E67 准入也已实际验证。

公共所有权与 D09–D11 调用约定见 [INTERFACES.md](INTERFACES.md)。D08 负责公共服务、映射、DTO、路由鉴权、客户端与轻量读取会话；领域模块使用唯一 `get_market_service` 和 `read_scope`。当前变化标识/签名游标绑定身份、来源序列、筛选、口径、固定窗口及当前质量/覆盖依赖；读取后/输出边界再次检查，质量变化即使 generation 不变仍失效。未知查询参数与重复参数拒绝，传输只读且无缓存，客户端校验 Decimal 字符串、窗口、游标与业务 DTO。

实际验证（Python 3.12.2/仓库锁定环境，PostgreSQL 17 私有测试库）：

| 检查 | 实际结果 |
|---|---|
| `uv run --locked --no-sync python -m pytest -q -p no:cacheprovider tests/test_s2_market_read.py` | **17 passed**；默认正式正例、歧义/缺事实、对象质量、维护、独立准入、组合依赖与游标、取消/预算、输出边界 |
| 下列受影响后端组合，`pytest -q -ra -p no:cacheprovider` | **201 passed, 13 skipped, 31 subtests passed**，含上述 D08 用例 |
| 自选/总览：`test_etf_watchlist.py test_etf_watchlist_postgresql.py test_operations_overview.py` | **43 passed** |
| `pnpm --dir frontend test` | **170 passed**，其中 D08 **17** 项：取消/卸载/晚到、401/403 原拒绝与跨面板撤下、凭据轮换、非 JSON/秘密兜底、读取预算、精度、失效/旧数据撤下 |
| `pnpm --dir frontend build` | **passed**；既有大 bundle 提示仍存在 |
| `python3 -m unittest discover -s tests -v` / `python3 scripts/release_version.py check` | **29 passed** / **passed** |

后端组合文件：`test_s2_market_read.py`、`test_data_store_api.py`、`test_data_store_inventory_api.py`、`test_data_store_error_envelope.py`、`test_data_store_full_coverage.py`、`test_data_store_local_pipeline.py`、`test_market_workspace.py`、`test_s3_data_gateway.py`、`test_stable_instrument_identity.py`、`test_task13_identity_pit.py`、`test_account_workspace.py`、`test_backtesting_pagination.py`、`test_s3_run_service.py`。

13 项跳过为 11 个需构建 `consume_gateway` 的 Rust IPC 案例、1 个需安装 SDK wheel 的案例和 1 个 S3 专用私有 DB/wheel 测试模块；由现有 S3 CI 执行，未冒充本地通过。组合初次缺测试库旧业务表导致失败；按现有 CI 在这个私有库执行已有 Alembic 迁移后，上述重跑通过。底座机械测试使用仓库既有 `LF_D01_ALLOW_OVERLAY_TEST=1` 隔离探针，生产文件系统门禁未改变，也未把本地证据写成真实环境验收。

前端一次重跑触发现有 S3 double-submit 用例的 30ms 等待断言（HTTP 调用尚为 0）；未修改 S3 文件，随后同命令完整重跑 **170 passed**。`uv sync --locked` 实际检查 53 个已安装包，无依赖变更。

具体限制：E70 正式模型仅声明 `provider_reported`，raw 解释仍须 D10 按证据确认；E51 未登记成 raw。E69 的复权锚点/策略/覆盖须 D11 独立核验。E67 未提供费用、列表状态、指数/管理人等字段，保持 null。缺可信映射与历史映射覆盖仍按对象拒绝，不制造 UUID、不拼接显示代码历史。

本包无新 UI 页面，预览/截图不适用；旧市场与采集/自选路径完整保留，后续退出由 D12 执行。未部署、未访问内网生产、未调用供应商、未 reset/rebuild、未增加收费权限；只处理隔离测试输入，未修改生产数据。

# S2-D07 集成与交接

开发与隔离验证完成，真实业务数据闭环仍待授权读取环境。没有合并 main、部署、生产操作、供应商调用或门禁绕行。

独立分支：`codex/s2-d07-integration-handoff`。最新 main `4c9e8167d49ef1d6bbc49afc6578208d7a339f97` 是组合祖先；精确交付提交全部保留为祖先：

| 输入 | 精确提交 |
|---|---|
| D01 | `1e7643a96440602575b31995fbec8c03304aa6ab` |
| D02 / PR153 | `9dafeb6eb052796d3b4783785feb104f1de3cf5b` |
| D03 / PR155 | `ba9f3c5558bf9a1137b6f3897262faf83efe58ac` |
| D04 / PR154 | `79cbd18c6bebe33d3ab884c28c3c9388ee32e8ab` |
| D05 / PR156 | `d19bf3c002b60b3733f5d66be13fa7952a18b0cb` |
| D06 / PR157 | `e6d755c6773d351a34dbb9203ab639db55818594` |

原 ZIP 保留于 D02 交接，SHA256 `bcd5a001be9c5a08e47d31e19ebe4133735080fdc43d7d567171145fc9afb344`；外包 15 项及 D07 子包 8 项清单均通过。已读 D07 TASK、COMMON、INTEGRATION、API_NOTES、UI_RULES。工作区及这些提交未发现 `AGENTS.md` / `.agents/skills`。

## 本次集成修复

- 详情 GET 刷新返回维护、限制、代次变化或对象移除时，撤下旧对象面板、结果和页栈。修复前维护拒绝仍显示旧预览，见 [复现](evidence/metadata-before-fix.json)；修复后对应 5 个浏览器场景通过。
- 原生 PostgreSQL HTTP 暴露 `/issues` 的参数类型推断错误（500）。只为可空筛选参数添加 text cast，保留数据集筛选、全局限制和成员计数；没有修改 `require_ready` 或权限。真实 PostgreSQL 筛选、分页、空参数和认证测试通过。
- D07 使用 D04 的 3 个新版呈现断言，并验证 D05 的单次导航即读取行为与 D06 问题分页共同工作。D06 交付文件未编辑；旧包截图与脚本保留为各自阶段的历史证据。

## 本组合实际验证

| 命令 / 证据 | 结果 |
|---|---|
| `pnpm --dir frontend test` | **140 通过**，其中 D07 7 项；此数已包含各前端包测试 |
| `node --test frontend/src/features/data-assets/data/tests/*.test.cjs` | **31 通过**（独立 D01 客户端目录） |
| `pnpm --dir frontend exec tsc -b --force`；`pnpm --dir frontend build` | 通过；保留既有 bundle 大小提示 |
| D07 `preview/acceptance.cjs` | [28 场景](screenshots/acceptance.json)，零页面错误 |
| D07 `preview/routes.cjs` | [22 路由 / 重定向](screenshots/routes.json)，零页面错误；仅 GET |
| 完整组合重跑 D05 `preview/acceptance.cjs` | [48 场景](evidence/d05-combined-summary.json)，含忽略取消的晚到响应 |
| 隔离 PostgreSQL `pytest -q tests/test_data_store_issues_api.py` | **2 通过**，没有文件系统例外 |
| D07 `preview/native-http.py` | [14 项原生 HTTP](evidence/native-http.json)及 [3 种宽度流程](evidence/native-screenshots/native-browser.json)通过 |
| `python3 -m unittest discover -s tests -v`；release version check | **17 通过**；版本一致 |

目录→筛选→详情→预览→问题→返回、页替换、键盘/焦点、切换/取消/晚到、401/403 清缓存、维护/限制/代次/字段变化、Decimal/大整数/纳秒/null 精度已覆盖。检查没有生产可达夹具开关、新轮询、全库下载或写操作；原有其他工作区路由仍可导航。

这些套件覆盖有交集，不累计为独立测试总数。日志摘要见 [local-validation.json](evidence/local-validation.json)。最终精确 head 与正式 CI 链接写入草稿 PR；以该 head 的检查为准。

D02 独立 PR153 的 [Validate #376](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38038179548/job/114172910644) 在 `9dafeb6…` 上为 90 pass / 1 fail：壳测试导入缺少 D01 `data` 模块。组合工作树的测试不能证明这个独立 PR CI 通过。本组合保留 D01 原始提交并运行完整测试，没有重跑缺依赖分支。

## 预览与截图

使用既有依赖，在三个终端分别运行：

```sh
node frontend/src/features/data-assets/handoff/s2-d05/preview/mock-api.cjs
node frontend/src/features/data-assets/handoff/s2-d07/preview/fixture-api.cjs
QF_DEV_BACKEND_URL=http://127.0.0.1:18768 pnpm --dir frontend dev --host 127.0.0.1 --port 5195 --strictPort
```

[可交互目录](http://127.0.0.1:5195/admin/data-assets) · [当前预览](http://127.0.0.1:5195/admin/data-assets/fixture.daily?view=preview) · [更新与问题](http://127.0.0.1:5195/admin/data-assets/fixture.daily?view=updates)。使用固定公开夹具值 `s2-d07-public-ui-fixture` 登录；只适用于 loopback。本地端口不提供外网托管地址。D07 代理与 D06 单包夹具共用 18768，分开启动。

[设置分页问题场景](http://127.0.0.1:18768/__scenario?name=issues-paged)后刷新页面；[设置维护拒绝](http://127.0.0.1:18768/__scenario?name=metadata-maintenance-error)后刷新详情。所有场景均为合成夹具，不证明真实数据获准读取。

| 视口 | 目录 | 预览 | 更新与问题 |
|---|---|---|---|
| 1440×900 | [截图](screenshots/catalog-1440.jpg) | [截图](screenshots/preview-1440.jpg) | [截图](screenshots/updates-1440.jpg) |
| 1024×768 | [截图](screenshots/catalog-1024.jpg) | [截图](screenshots/preview-1024.jpg) | [截图](screenshots/updates-1024.jpg) |
| 390×844 | [截图](screenshots/catalog-390.jpg) | [截图](screenshots/preview-390.jpg) | [截图](screenshots/updates-390.jpg) |

图片来自实际 Chromium，已查看代表图；14px 正文、主纵向滚动区、局部横滚、焦点与错误提示可达。另有明确标注“原生 HTTP · 隔离维护门禁 · 无业务数据”的 [1440](evidence/native-screenshots/native-overview-1440.jpg)、[1024](evidence/native-screenshots/native-overview-1024.jpg)、[390](evidence/native-screenshots/native-preview-gate-390.jpg)截图。

浏览器脚本使用现有 `PLAYWRIGHT_MODULE` / `CHROMIUM_PATH`，未新增应用依赖。云端原生 HTTP 复现：在 `backend/` 执行 `bash /workspace/.setup/cloud-dev/run.sh --db -- python ../frontend/src/features/data-assets/handoff/s2-d07/preview/native-http.py`；它创建/销毁独占测试 DB 和自己的本地进程，调度关闭，不连接供应商。

## 尚未完成 / 阻塞

- **真实业务数据联调未完成**：本任务未获得已授权可读端点/会话；云环境 overlay 被实际 CurrentStore 拒绝为 `UNSUPPORTED_FILESYSTEM`。未修改支持名单或强写 ready。原生 HTTP 只证明隔离认证、真实 registry/schema、问题 SQL 和关闭门禁路径；没有生产业务文件。
- 当前 checkout 的 `/query` 仍使用全局 `require_ready`。健康 A 与受限 B 的按领域读取、真实业务分页/游标 DATA_CHANGED、实际 403 权限撤销须由 D01/R01 在授权读取环境验证。UI 夹具和本地 PostgreSQL 测试不替代这些路径。
- D03 到 1000 项预算后不假装全量；继续读取依赖 D01 后续契约。问题 offset 分页没有稳定快照令牌，相同计数下的重排不可证明稳定。业务覆盖、截至日及单位语义仍未声明。
- 登录 token 的 sessionStorage 行为来自既有 `auth/tokenStorage.ts`（引入提交 `1fb5a3b0cafc2cffdf509f1202853614c701c483`），本批未改变。新预览游标/结果未写入 URL 或持久存储；未扩大认证或安全架构范围。
- R01 的 d94 健康/接口回报不等于普通 v24 终态、清理或唯一交接完成；本批开发通过也不代表底座全量验收或第二阶段全部完成。

本地完整后端/容器大套件未重跑：文件存储受支持挂载条件不足，保留正式 CI 的原生执行结果。没有绕过 gate、连接内网、部署、任务启停或凭据/安全设置变更。

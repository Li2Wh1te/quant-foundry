# 编包依据与事实边界

编包日期：2026-10-10。仓库：`Li2Wh1te/quant-foundry`。本轮只读核对 main：`4c9e8167d49ef1d6bbc49afc6578208d7a339f97`。

这个提交只用于说明“任务从什么实际代码出发”，不是要求开发回退版本，也不是新增部署台账。执行时从最新main接续，已有修复与用户最新决定优先。

| 已读取材料 | 本包采纳的事实／规则 |
|---|---|
| `frontend/src/App.tsx` | 既有数据资产列表和详情路由、认证与OverviewShell接线 |
| `frontend/src/components/OverviewShell.tsx`、`Overview.css` | 已有公共壳、导航、折叠状态、快捷跳转和样式变量；不重做 |
| `frontend/src/pages/DataAssetsPage.tsx` | 已有Catalog、DatasetDetail、字段、基本预览、更新问题；本批重组这些内容 |
| `frontend/src/api/dataStore.ts` | 已有当前目录、预览及错误封装；统一完善而非新建并行客户端 |
| `backend/app/data_store/router.py` | 目录、状态、问题与当前查询；基线查询仍调用require_ready，覆盖标志不自动为true |
| `frontend/package.json` | 现有React/TypeScript/Vite工具链；test为node --test，build包括tsc与Vite |
| 已确认设计01–03及组件04文件 | 工作流、Sheet、Metric Band、Tabs、正常字号、Local First |
| `R01_执行纠偏与收尾指令_v1.0.md` | R01负责安全/质量/完整覆盖、按领域读取及更新交接；不手工改ready |
| `R01_进度耗时与数据现状审计_2026-10-04.md` | 处理、质量、覆盖不是同一口径；只作历史解释，不把旧计数写入实时UI |

本批范围、7包拆分、建议新文件路径、默认标签与元数据加载预算，是本次任务安排；不是把源码未有的能力写成已经实现。未重新验收生产、未审核全部R01候选代码。

固定源码浏览根：
`https://github.com/Li2Wh1te/quant-foundry/tree/4c9e8167d49ef1d6bbc49afc6578208d7a339f97`

默认测试命令（已核对frontend/package.json）：
```sh
pnpm --dir frontend test
pnpm --dir frontend build
```
新增浏览器自动化应沿用项目已具备的方式；若需要极小测试依赖，说明用途即可，不更换整个测试框架。所有功能包都自行测试，D07不是第一次测功能。

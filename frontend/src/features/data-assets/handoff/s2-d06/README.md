# S2-D06 · 更新状态与问题解释

实现范围：`UpdateIssuesPanel.tsx`、D06 局部样式、分页会话和测试。D01 `data/*`、D02 公共壳、D03 目录、D04 概览/字段与 D05 预览均未修改。

## 基线与包校验

- 独立分支：`codex/s2-d06-update-issues`。
- 基线：D04 `79cbd18c6bebe33d3ab884c28c3c9388ee32e8ab`，已包含 D01 `1e7643a96440602575b31995fbec8c03304aa6ab` 和 D02 `9dafeb6eb052796d3b4783785feb104f1de3cf5b`。
- 核对远端 main `4c9e8167d49ef1d6bbc49afc6578208d7a339f97`：它是基线祖先，没有尚需引入的有效变更；本分支没有再合并 main。
- 仓库基线及工作区未发现 `AGENTS.md` 或 `.agents/skills`。按核验后的 D06 要求执行。
- 原包：`../s2-d02/Quant_Foundry_S2_Batch1_Small_Tasks_v1.0.zip`，SHA256 `bcd5a001be9c5a08e47d31e19ebe4133735080fdc43d7d567171145fc9afb344`。
- D06 子包 SHA256 `b6ab79413507bc604e2bc14328c650a4b493290f0730de592d73352212b7cca8`；外包全部内容哈希、子包全部 8 份文件哈希通过。原文保留在 [requirements](requirements/TASK.md)。

## 用户行为

当前目录状态、最近处理结果、处理是否完成、本次质量确认、业务日期覆盖和实际读取权限分别显示。优先消费 descriptor 的 `last_update`；缺失显示暂无记录，不请求全入口 `/status` 来猜阶段。来源刷新失败不撤销全部旧数据；范围未确认不表示全部数值错误。更新时间保留精度，并与业务截至日区分。

通过 D01 `dataAssetsClient.listIssues` 调用真实已有的 `GET /api/admin/data-store/issues?dataset=…&limit=20&offset=…` 接缝。首次始终读取第一页，包括摘要问题数为零或未知的情况；只在用户翻页时读取下一页，不拉全量、不按文案去重、不累计拼接页结果。问题记录数、当页记录数、受影响成员数分别标记；成员可能重叠，合计不是错误率或去重对象数。未指定数据集的记录明确说明由服务端纳入筛选。

合法空响应、读取失败、缺失计数和未知原因分开呈现。普通失败可保留带原读取时间的旧非空列表；旧空列表刷新失败时不显示零问题成功状态。重新读取重置到第一页，旧分页在成功前禁止继续。代次/对象变化、401/403、读取限制、数据变化和退出登录清除会话，取消并拒收晚到响应。

现有壳的“刷新页面”通过 `refreshVersion` 刷新元数据和问题。面板的“重新读取问题”仅 GET 问题；“调整当前查询”保留列表上下文；数据源和采集任务链接仅导航。D06 浏览器请求记录确认只 GET，没有检查、更新、发布、retry、finish、清理或采集调用。

## 接缝与真实联调缺口

- 已接入实际源码协议：D01 客户端、D02 props/认证失效处理、descriptor/问题分页 DTO。没有改后端、安全设置、凭据或供应商请求。
- D01 共享原因字典尚缺 `SOURCE_REFRESH_FAILED` / `FULL_RANGE_UNPROVEN` 的完整说明。本包在面板局部补中文解释和只读下一步。D07 如需跨视图统一，交由 D01 集中维护。
- 当前 DTO 未提供可靠业务覆盖或业务截至日字段，因此明确未声明；不由 `qualified`、`complete`、HTTP 200 或更新时间推断。
- offset 分页没有稳定快照令牌。检测到声明总数或成员合计变化时清除旧分页；相同计数下的记录重排不能由前端证明稳定，界面提示手动刷新。没有假造跨页原因统计。
- 开发与隔离测试：已完成。真实后端联调：未执行，需要 D01/R01 在授权的非生产环境验证 `GET /datasets/{dataset}`、`GET /issues` 的实际认证、问题集合和分页；夹具不能证明生产权限、覆盖或 R01 完成。生产操作：未执行；没有合并或部署。

## 实际验证

在 `frontend/` 执行：

```sh
node --test tests/data-assets-update-issues.test.cjs
npm test
node --test src/features/data-assets/data/tests/data-assets.test.cjs
node_modules/.bin/tsc -b --pretty false
npm run build
```

结果：D06 10 项；前端 110 项（含 D06）；D01 31 项全部通过。`tsc` 和 build 通过。Vite 提示单个 bundle 超过 500 kB，未扩大范围拆包。

[D06 浏览器报告](screenshots/acceptance.json)：16 组检查，1440×900、1024×768、390×844；含状态语义、全分页序列、同属性重复记录、空集/失败、缺失计数、未知码、权限撤销、对象/代次切换、晚到响应、24 次 Tab 切换、15 次快速刷新、仅 GET/导航及键盘焦点。页面错误为 0。所有图片标注隔离夹具，三种视口检查无整页横溢或新增纵向滚动容器，正文约 14px，小计数不折行。

[D02 壳回归](screenshots/shell-regression.json)：27 项全部通过。使用原 D02 夹具与验收脚本，仅适配 D04 的 3 条说明选择器和 D06 的 2 条标题/错误文案选择器；没有删除行为断言。既有预览 GET/只读 POST 行为由原脚本覆盖，未编辑 PreviewPanel，也不替代 D05 验收。

典型截图：[当前状态 1440](screenshots/status-1440.jpg)、[更新 1024](screenshots/update-1024.jpg)、[问题 390](screenshots/issues-390.jpg)、[空集](screenshots/empty-390.jpg)、[刷新失败](screenshots/refresh-failure-390.jpg)、[空集刷新失败](screenshots/empty-refresh-failure-390.jpg)、[权限撤销](screenshots/forbidden-390.jpg)。

## 可交互隔离预览

已有前端依赖时，在仓库根目录启动夹具：

```sh
node frontend/src/features/data-assets/handoff/s2-d06/preview/mock-api.cjs
```

另一个终端在 `frontend/` 启动页面：

```sh
QF_DEV_BACKEND_URL=http://127.0.0.1:18768 npm run dev -- --host 127.0.0.1 --port 5186 --strictPort
```

浏览器脚本只在 loopback 注入固定假令牌，阻止外部网络，并将场景截图写入 D06 `screenshots/`：

```sh
PLAYWRIGHT_MODULE=/path/to/playwright node frontend/src/features/data-assets/handoff/s2-d06/preview/acceptance.cjs
```

入口：`http://127.0.0.1:5186/admin/data-assets/fixture.daily?view=updates`。假令牌仅用于本隔离夹具，不是应用凭据。测试完成后终止两个本地进程。

壳回归另启动原 D02 `preview/mock-api.cjs`（18765），以 `QF_DEV_BACKEND_URL=http://127.0.0.1:18765` 启动 Vite 5187，再执行 D06 `preview/shell-regression.cjs`。它的截图默认写到 `/tmp/qf-d06-d02-regression`，摘要已经随本包保存。

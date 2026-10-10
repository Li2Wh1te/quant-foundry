# 并行开发接缝与文件归属

以下路径是本批建议新增的落点；并非声称仓库已存在。如执行时已有等价模块，复用等价模块，保留相同职责。

## 稳定接缝

D01 负责 `frontend/src/features/data-assets/data/` 与 `frontend/src/api/dataStore.ts`。提供 `client`（目录、详情、状态、问题、预览的类型化封装）、`presentation`（安全文案和格式化）、`types` 以及测试夹具。视图接收数据和状态，不复制数据请求逻辑。数据合法性、覆盖、价格单位和权限仍由后端决定。

D02 负责将原 `DataAssetsPage.tsx` 的 Catalog、DatasetDetail、预览、更新问题区域做纯模块拆分，先保留原行为；然后交付页面壳、Tabs与少量无业务逻辑的呈现组件。**D02合入前，D03–D06可以写独立模块和测试，但不同时编辑原单文件页面。**

拆分后的默认归属：

| 文件／目录 | 唯一主要负责人 |
|---|---|
| `data/*`、`frontend/src/api/dataStore.ts` | D01 |
| `frontend/src/pages/DataAssetsPage.tsx` 路由分派、`App.tsx`必要接线、`OverviewShell.tsx`目标路由高亮 | D02 |
| `components/*` 基础页面组件、`DataAssetsLayout.tsx`、布局样式 | D02 |
| `CatalogView.tsx`、目录局部样式 | D03 |
| `DatasetView.tsx`概览/字段内容、`FieldTable.tsx` | D04 |
| `PreviewPanel.tsx`、预览局部样式 | D05 |
| `UpdateIssuesPanel.tsx`、更新问题局部样式 | D06 |
| `frontend/tests/data-assets-integration*.test.cjs`、批次结果汇总 | D07 |

D02为详情容器预先接入 D04/D05/D06 的组件出口；之后各包替换自己的模块，不反复修改全局路由。D01与D02按现有 DTO 与本页接缝并行，不互相等待完整实现。

## 路由

保留 `/admin/data-assets` 与 `/admin/data-assets/:datasetId`。详情视图采用 query 参数 `view=overview|preview|updates`，默认overview。列表筛选继续用既有 `search/status/page`；本批增加 `source/frequency` 时在一个共享参数辅助模块中白名单处理。切换详情视图不丢列表返回上下文。

浏览器URL和sessionStorage不保存Bearer token、游标、业务原件或完整预览结果。游标仅留在当前预览会话内。对象标识变化时立即取消前一请求、清除对应预览与页栈。

## R01交接

不要多人直接改 `backend/app/data_store/router.py` 的门禁。发现服务端缺字段或R01正在变化的返回，D01统一登记一条具体接口依赖并与R01负责人对齐，其他视图先完成可信的未知/失败状态。普通页面增补不得变成R01新的全域关单条件。

D07在最后集成。若某一真实读取路径被R01挡住，只列该路径待联调，不伪造通过，也不要求把目录等已完成代码全部退回。

# 已核对接口与使用注意

本页是编包时的源码事实，不是新增的后端协议。执行时若 main 已有 R01 新接口，以实际实现为准，更新一个共享前端适配点；不得静默假设新字段存在。

## 已有接口

前缀：`/api/admin/data-store`；沿用项目现有认证。

| 接口 | 本批用途 | 基线行为／边界 |
|---|---|---|
| GET `/datasets?limit=…&offset=…` | 数据目录 | `items/total/phase/next_offset`；未提供服务端文本、频率、来源筛选参数 |
| GET `/datasets/{dataset}` | 当前数据集说明 | 字段、限制、状态、`last_update`、`preview_key` 等；路径编码数据集标识 |
| GET `/status?limit=…&offset=…&state=…` | 当前入口操作状态 | 与业务目录不是同一计数口径；包含非业务入口；按 `dataset` 关联，不能按数组位置拼接 |
| GET `/issues?dataset=…&limit=…&offset=…` | 当前问题分页 | 返回问题集合；后端有 `affected_objects`，前端已有类型需补齐；不是去重错误率 |
| POST `/query` | 有界当前数据预览 | 正常请求必须 `allow_partial=false`；查询不是发布操作 |

`CurrentQuery` 基线：`dataset, frequency, representation, subject, from_key, to_key, columns, page_size, cursor, allow_partial`；`columns≤32`、`page_size≤100`。后端还有可选 `partitions`，普通用户不需要输入。

`CurrentDataset.representation` 当前实际装的是 `row_layout`，**不是查询所需的来源表示键**。默认查询使用 `preview_key.representation`；不要把 `typed-object-nodes-v2` 直接传给查询。`preview_key` 只是一个实际当前对象起点，不是所有主体选择器。

基线查询响应始终把 `request_satisfied` 与 `business_date_coverage_verified` 保持为 false。不能以 HTTP 200、满页、连续日期、存在 `next_cursor` 或“目录可用”改成“全范围验证通过”。

`actual_range` 仅为当前返回页的业务键范围。报告成员可能跨页，不能把单页节点表显示成完整报告。`allow_partial=true` 可能返回受限、无效或撤回内容，不是“只返回安全部分”的保证。

## 状态映射

| 后端事实 | 用户显示 | 不允许的推断 |
|---|---|---|
| `not_checked` | 尚未检查 | 不能显示为空或无数据 |
| `empty` | 当前为空 | 不承诺市场上没有这种数据 |
| `available` | 当前可用（按服务端声明） | 不证明任意请求完整，也不证明 R01 完成 |
| `restricted` | 存在限制 | 不一律推断全领域不可读；请求以服务端判断为准 |
| `rebuild_required` | 需要重建 | 不自动执行 rebuild |
| `rebuilding` 或查询 `DATA_STORE_REBUILDING` | 暂不可读取／处理中，以实际响应为准 | 不通过前端解除 |
| 未知／缺失状态 | 状态待确认 | 不回落成 available、empty 或 0 |

最近更新结果与上述当前数据状态分列。`processed/incomplete/running/backoff/deferred/failed` 等只在实际响应存在时翻译；缺失就“暂无更新记录”。不要从“失败原因”自行生成逐域读取许可。

## 读取客户端

统一复用 `frontend/src/api/dataStore.ts`；调用层检查响应基本形状，不能只用 `as T` 声称验证。401 沿用现有认证失效处理；403 显示安全的权限失败，不新增登录／权限系统。未知错误显示通用文案与安全原因码，不输出密钥、SQL或原件。

目录元数据可在有界范围内翻页补全。本批默认每页最多100、最多10页／1000项；这是**前端元数据加载预算**，不是底座规模上限。达到预算或中途失败要显示“目录未完整加载”，不能把已加载项的计数当全局统计，也不能把最后一页误当终点。

问题列表按需分页，不拉完整问题表。数据预览按需查询，不下载全库。默认手动刷新页面，无永久轮询。切换对象、条件、退出登录或卸载时取消请求并拒收晚到响应。

## 后续 R01 接口衔接

本批可以用已核对接口完成大部分开发。按领域可读、精确业务日期覆盖、可搜索的统一标的及单位语义，并非本批通过字段名就能推导的能力。接口尚未提供时如实展示“未声明／待确认”，或将具体功能列为联调依赖；不因此新增底层治理功能。

# LF-R01 前置工具兼容性阻断

状态：`partial`，任务包未完成。`reset_applied=false`、`rebuild_complete=false`、`domains_accepted=false`。

## 阻断原因

现有 `backend/app/legacy_reset/rescue.py` 的 `_finish_baseline` 未分类处理以下旧基线类型，原件保全守卫会返回 `ORIGINAL_UNCLASSIFIED`：

- `foundation / empty_local_scope`
- `foundation / local_table_bootstrap_receipt`
- `tonghuashun / report_identity_directory`

这些类型的定义可以在仓库历史代码的 `scope_settlement.py`、`table_bootstrap.py`、`holding_commands.py` 中核对。前两类需要明确的运维回执分类；第三类包含审核身份、有效期和依据，应完成保护与读取设计，不能与旧派生对象一并忽略。

目录与依赖计划通过不等于原件保全成功。在完整 export/verify 通过前，不执行 apply，也不按缩小输入范围的方式宣布重建完成。未知原件仍须拒绝处理。

## 恢复条件

前序工具需在隔离环境补齐精确分类、必要的原件格式及 reader 支持，并覆盖 export、verify、apply 锁内重校验。修复制品经验证和维护部署后，重新生成目标环境计划与保全证据，再继续 R01 的清退、全量重建、更新交接、只读验收与尾部清理。

本交付不修改产品运行代码，不把开发测试或制品部署等同于真实数据验收。B06、reset apply、业务 rebuild/update、audit-export 和最终 cleanup 均未取得完成证据。

## 证据边界

现场证据含运行元数据，保留在维护者本地，未纳入公开仓库。公开提交仅记录可以从已有源代码及合成基线复现的工具兼容性缺口；它不是完整生产验收报告，也不声明 R01 已通过。

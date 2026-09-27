# 轻量数据底座开发进度

## 当前状态（2026-09-27）

D01–D05 已合入 main，复核基线为 `4c3e094c17aafadcb4303589d8e123c7f5e6de72`。
本轮在 `codex/lf-d02-snapshot-resume` 修复 main 复核发现的三个 D02 缺口。

| 范围 | 状态 |
| --- | --- |
| D01 存储内核 | 已合并；本轮只扩展有界暂存槽的续作保留规则，重跑内核和跨容器验收 |
| D02 来源适配及更新 | 多分区持久续作、可变表无变化 no-op、完整快照缺失键删除已实现；专项与 1000 自然分区回归已通过 |
| D03 旧体系维护及原件保全 | 已合并；生产 reset 未执行 |
| D04 应用接入 | 已合并；生产部署未执行 |
| D05 最终复验 | 固定代码 CI、跨容器、B01–B05 与 1000 自然分区复验通过；`code_ready=true` |

当前修复与验证边界见 [本轮结果](lf-d02-d05-review-fixes.md)。
[原 D05 结果](lf-d05-result.md)保留历史证据，不能替代本轮复验。

`lf01_code_ready=true`；`P01_deploy=not_started`；`reset_applied=false`；
`rebuild_complete=false`；`domains_accepted=false`。

本轮没有执行生产部署、停写、迁移、reset、重建、原件删除或生产容器/卷清理。


## LF-D06（开发验收中）

D06 基于已合并 main `08c10fd`，删除正式行可重算字段并升级 row-layout v2。
实现、专项/本地全量、100k envelope 和 1000 自然分区通过；完整 10M B05 与最终 CI 待完成。
D06 `code_ready=false`，不改变上轮 D01–D05 已验收的历史状态。生产阶段均未开始。
当前进展见 [LF-D06 结果](lf-d06-storage-slimming.md)。

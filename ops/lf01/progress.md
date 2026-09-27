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


## LF-D06（隔离开发验收通过）

D06 基于 main `08c10fd`，删除正式行可重算字段并升级 row-layout v2。
专项/本地全量、两组代码 CI、真实 ext4 跨容器、100k envelope、1000 自然分区与最终镜像完整 10M B05 全部通过。
10M 最终正式数据 416,616,377 bytes，为旧格式同口径的 54.6665%；精度、热修正及未触及文件保持通过。
D06 `lf01_code_ready=true`；`P01_deploy=not_started`；`production_deployed=false`；`reset_applied=false`；`rebuild_complete=false`；`domains_accepted=false`。
PR #122 的最终证据提交须通过必需 CI 后才能合并；生产部署/reset/rebuild 仍未执行。
当前结果见 [LF-D06 结果](lf-d06-storage-slimming.md)。

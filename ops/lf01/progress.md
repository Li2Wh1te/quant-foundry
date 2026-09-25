# 轻量数据底座开发进度

## 当前分包状态（2026-09-25）

维护者已经把原 LF-01 整体工作拆为独立开发包，并明确授权 LF-D01 v1.1 完成后单独合并。本次仅交付 D01，不再使用“整个 LF-01 一次合并”的早期计划来阻止已独立验收的 D01，也不把 D01 成功当成整个重建完成。

| 范围 | 实际状态 |
| --- | --- |
| D01 当前存储与安全 IO | 代码已推送 PR #115；完整仓库 CI、ext4 真实跨容器验收、59 项原生内核/schema测试和11项B01–B04合成场景通过 |
| D02 来源适配、顺序和完整性语义 | 不在本次交付范围 |
| D03 旧体系维护、原件保全、reset/退役 | 不在本次交付范围；没有执行 reset |
| D04 公共 API、前端与默认调度接入 | 不在本次交付范围；没有切换线上入口 |
| D05 最终集成验收 | 不在本次交付范围；仍须在最终集成提交复验 |

实际接口见 [lf-d01-interfaces.md](lf-d01-interfaces.md)，当前结果和可核对的运行ID、指标、限制见 [lf-d01-result.md](lf-d01-result.md)。前一轮 local-only partial 结果原文保存在 [lf-d01-local-result.md](lf-d01-local-result.md)，其未推送/未补验说明是历史阶段，不代表当前 PR 状态。

## 开发祖先与合并纪律

开发前序是真实 `e09f41d093c665ea6ca34af05168864f79445f9f`，包括已测试的精确数值、资源策略和锁模块；没有回退覆盖。D01 五个补丁基于这个真实 Git 祖先应用，完整功能树 `998ff5da39af4d1a77f142b7052770370b3f27ef` 与原本地交付一致。没有推送合成快照根或 force push。

独立开发分支 `codex/lf-d01-current-store`，PR #115。文档记录成功补验后，仍需确认最终 head 的 Validate 与 Current store isolated acceptance 都成功才合并。广义 LF-01 草稿 PR #114没有作为整个任务完成而合并。本次不创建自动生产部署。

## 权限和状态

`lf_d01_acceptance_complete=true` 表示独立开发包已通过实测，不表示 GitHub 合并记录或生产验收；实际合并状态看 PR #115。`lf01_code_ready=false`；`reset_applied=false`；`rebuild_complete=false`；`domains_accepted=false`。

生产访问、部署、停写、重置、迁移、清理、重建和业务数据写入均未执行。生产操作继续由维护者负责。

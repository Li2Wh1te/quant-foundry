# 轻量数据底座开发进度

## 当前分包状态（2026-09-25）

原 LF-01 已拆为独立开发包。维护者对本轮 D02 的最新要求是“代码写完之后先提交，不用做太多测试”。本轮实际只提交开发分支，不创建 PR、不合并、不部署；未执行的完整验收不能标为通过。

| 范围 | 实际状态 |
| --- | --- |
| D01 当前存储与安全 IO | PR #115 已合并，main 基线为 `1f155a8a975e9107f1246a1698a5a21cb7a21642`；该包完整 CI、ext4 跨容器及 B01–B04 证据保留 |
| D02 来源适配、顺序和完整性语义 | 代码已推送 `codex/lf-d02-local-adapters`；30 项小型检查通过，`ready_for_review`；完整验收未执行，尚未合并 |
| D03 旧体系维护、原件保全、reset/退役 | 不在本轮范围；D02 仅提供自包含救回输入接口，没有执行 reset |
| D04 公共 API、前端与默认调度接入 | 不在本轮范围；D02 仅交付内部接口，没有切换线上入口 |
| D05 最终集成验收 | 不在本轮范围；仍须在最终集成提交复验 |

## D02 已推送的代码

真实开发基线为 `1f155a8a975e9107f1246a1698a5a21cb7a21642`，源码树 `94af1830709a5270209681962af737ca5d9740b6`。

1. `28759201dfd33dc95fb2a3dcc16b56e1b22e20cb`：所有本地领域适配、完整入口处置及来源确认读取。
2. `7ce692567af01e403830365d283382fdc7c4c4e2`：有界本地 rebuild/update/retry、CLI、当前问题/完整对象处理和评审交付。

两次代码提交包含 55 个改动文件，推送前逐文件校验内容 SHA 和模式；代码树为 `70e7a4fb5baf0ab5abec7388a455af002e818952`。本进度文件是随后单独的文档更新。没有推送合成快照 Git 根、强制推送或覆盖已有分支。临时传输工作流位于单独分支，完成后已移除，不在本交付中；产品 CI 权限和触发规则未改。

71 个原映射入口均有处置：60 个业务入口、7 个来源辅助入口、3 个导入通道、1 个空状态。不是 71 个新业务数据集。

- [D02 内部接口、命令与限制](lf-d02-interfaces.md)
- [D02 结果、实际检查与待验收项](lf-d02-result.md)
- [逐入口处置清单](lf-d02-disposition.csv)
- [小型检查原始摘要](evidence/lf-d02-smoke.txt)

## 本轮验证边界

30 项小型检查通过，9.33 秒；模块编译、所有业务 schema 生成及 CLI 离线帮助/describe 检查通过。使用真实隔离 PostgreSQL、文件、flock、Arrow 和 DuckDB；本地 overlay 测试沿用 D01 专用文件系统探测替换。这不是本轮 ext4 持久性或跨容器验证。

全迁移链、全部后端/前端回归、全领域原生 fixture、跨容器、B05 千万条负载和最终 CI 均未执行。B05 执行器已提交，仅执行小型代码冒烟。完整验收须在后续合并前补足；D01 的通过结果不能代替 D02 验收。

旧合并窗口缺少实际返回键凭据时保留 `SOURCE_CONFIRMATION_UNPROVEN`；未知单位、时间、复权等限制不猜测补齐。详见 D02 结果说明。

## 历史证据

D01 接口见 [lf-d01-interfaces.md](lf-d01-interfaces.md)，开发验收记录见 [lf-d01-result.md](lf-d01-result.md)。早期 local-only partial 报告原文保存在 [lf-d01-local-result.md](lf-d01-local-result.md)，不代表当前 D01 合并状态。广义草稿 PR #114没有作为整个 LF-01 完成而合并。

## 权限和状态

`lf_d01_acceptance_complete=true`；`lf_d02_status=ready_for_review`；`lf_d02_acceptance_complete=false`；`lf01_code_ready=false`；`reset_applied=false`；`rebuild_complete=false`；`domains_accepted=false`。

本轮没有连接生产、部署、停写、执行生产迁移或 reset、运行生产 rebuild、删除原件/旧发布或清理生产容器/镜像/卷。main 未修改，未开启自动部署。生产事项继续由维护者负责。

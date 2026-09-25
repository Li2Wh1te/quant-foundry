# LF-D03 结果

状态：ready_for_review

前序输入：`75e86df`（LF-D02 领域夹具验收；分支 `codex/lf-d02-domain-fixture-acceptance`）。本包分支：`codex/lf-d03`。提交与 PR 以本分支最终回传为准。

实际执行环境：开发隔离。未接入内网部署或内网数据维护。

## 已完成

- 增加非破坏性迁移 `20261007_01`，只创建维护进度、任务原状态和最小限制三张小表；旧库升级与新空库历史迁移都成功，未通过普通迁移删除数据。
- 实现独立的 `python -m app.legacy_reset`：精确目录计划、数据库身份和摘要核对、旧任务暂停/恢复、原件 gzip JSONL 导出与双重校验、分组 `RESTRICT` 清退和进度续作。工具不导入旧 `app.data_foundation` 运行模块。
- D02 本地 reader 可把救回来源与原生来源并读，保留原生更新与救回原件的不同身份；旧表变更日志不会被解释为当前来源行。
- 旧当前限制在删除事务内从最新 issue 捕获；能证明的旧领域、目标、字段和理由保存到独立小表，未定位者继续阻止 `finish`。
- 操作接口、退出码、前置条件、中断恢复和阶段界线见 [lf-d03-maintenance.md](lf-d03-maintenance.md)。

## 检查结果

| 项目 | 结果 | 隔离证据 |
| --- | --- | --- |
| C17：旧库先升级后显式 reset | 通过 | 在隔离库先升级至 `20261006_01`，写入旧派生行与保护表标记，再升级 D03；升级后两者均在。显式 apply 后旧表清退，保护标记与原生表仍在。 |
| C17：新空库历史安装 | 通过（安装与显式清退） | 新建 `lfd03_final_test` 从空库 `alembic upgrade head` 到 D03，plan 见 80 张旧表、30 个旧函数、137 个触发器、无 blocker；显式 apply 后旧表数为 0，phase 为 `reset_done`。 |
| C17：中断续作与重复 apply | 通过 | 首次只完成 hooks，下一进程用同一计划续作其余组；全量完成后再运行同一 apply，五组均 `already_complete`。 |
| C18：保护性拒绝 | 通过 | `backend/tests/test_legacy_reset.py` 覆盖错库、未知同前缀表、外部 FK、动态函数、活动 writer、归档符号链接、未保全原件、校验后变化和重复执行时对象变更。 |
| C18：唯一原件救回 | 通过 | 合成旧基线导出、SHA-256/行数核对、D02 `RescueSources` 读取；旧暂存 dump 使用原始 `collected_at`，晚于保存时间即拒绝。 |
| 本地 reader 回归 | 通过 | 隔离 Docker 中 LF-D03 7 个 PostgreSQL 测试；与 D02 schema/local adapters/pipeline/domain 样本组合共 105 个测试通过。容器使用 `LF_D01_ALLOW_OVERLAY_TEST=1` 的测试注入，仅模拟持久文件系统，不作为真实宿主文件系统认证。 |
| 生产计划/原件状态/真实删除/部署 | 未执行 | 本包禁止生产接入；真实计划必须由 R01 在目标库重新生成。 |

## 交付与后续输入

代码：`backend/app/legacy_reset/`、`backend/app/db/migrations/versions/20261007_01_legacy_maintenance.py`、D02 `local_sources.py`/CLI 接口；验收：`backend/tests/test_legacy_reset.py`；操作说明：`ops/lf01/lf-d03-maintenance.md`。隔离计划文件已清理，不作为生产 allowlist。D04 应接应用和 scheduler 维护门禁并处理未定位的限制；D05 固化部署步骤；R01 才进行真实环境取证与清退。

## 未解决问题

- 本包未在真实来源上证明救回文件大小或每个旧基线低于 64 MiB/4 GiB 上限；一旦超限会拒绝执行，需要先设计分段有界救回并复验。
- 旧 issue 范围若无法严格映射到当前原生对象，保留 `located=false` 并阻断 `finish`。D04/R01 需定位与验证，不能将其直接移除。
- 新空库沿用历史迁移链，因此安装时会产生空旧表；本包通过显式 reset 清退。应用 `READY` 门禁和自动化安装顺序由 D04/D05 集成。

## 权限自述

生产访问：未执行。生产部署：未执行。真实任务停写：未执行。生产数据库/文件删除：未执行。生产业务写入：未执行。生产清理：未执行。所有对象清退只发生在 `qf-lf-d03-isolated` Docker 项目的临时数据库或测试 schema。

# LF-D04 结果

状态：ready_for_review（开发隔离验收；不代表内网部署或正式数据验收）

前序输入：`287cc7e`，分支 `codex/lf-d03`。本包分支：`codex/lf-d04`；实际提交与 PR 以最终回传为准。

实际执行环境：开发 worktree、任务专用 PostgreSQL 17 容器与本机共享 Docker volume、隔离浏览器模拟 API。未接入生产数据库、卷、任务或密钥。

## 已完成

- 当前数据 API 接入 `/api/admin/data-store`：`GET /datasets`、`GET /datasets/{id}`、`POST /query`、`GET /status`、`GET /issues`。沿用 Bearer 认证；目录和字段说明来自 D02 静态注册；读取走 D01 的显式当前文件、共享锁、代次游标和资源预算。查询断开会触发内核取消检查，输入不接受旧 `release_id` 或任意 SQL/文件路径。旧 `/api/admin/data-foundation` 统一认证后返回 `410 LEGACY_FOUNDATION_REMOVED`。
- 旧库仍含旧任务或旧表记录、以及 D03 receipt 尚非 `ready` 时，新查询返回明确 `DATA_STORE_REBUILDING`。真正空的新库可直接进入 `ready`；一般升级和 API 启动不执行 reset、rebuild 或删除。最近更新失败、当前数据可用性、问题限制分别展示；无从证明逐日连续覆盖时 `request_satisfied=false`，分区范围只声称分区精度。
- 注册单个 `data_store.update_local` 任务（本地数据更新 / Local Data Update），无旧执行 ID 和供应商开关依赖。旧任务定义、等待恢复计时器、旧排队运行及手动重启路径退役；维护态不领取本地更新。CLI `python -m app.data_store` 提供 `describe/status/rebuild/update/retry/cleanup/audit-export`，复用 D02 pipeline，需显式本地目录与数据库配置。
- 数据资产页替换旧五步候选/批准/发布界面：目录筛选与分页、分区范围、频率和口径、更新时间、独立更新状态、问题明细、字段说明、带代次游标的当前预览。保留已有市场/账户/策略/结果模块职责，未将旧回测输入伪接到新底座。
- Backend 与 Runner 共挂载 `current_store` volume，含同一数据及锁目录；移除旧归档运行挂载和 GID。`make selfhost` 旧 `.env` 升级会补入新的非密钥默认值，不覆盖已有配置或密钥；删除整卷 reset 快捷命令。

## 旧运行位置处置

| 分类 | 位置与结果 |
| --- | --- |
| 删除 | `backend/app/data_foundation/` 全部运行内核、router、模型、调度任务；`app/runner/coordinator.py`；旧数据资产/记录/报告页面与客户端；旧更新和备份脚本及其专属测试。 |
| 纯逻辑迁移 | D02 `app/data_store/adapters/` 的领域解析、单位、记录 schema 与布局继续承担原纯函数职责；当前运行路径无 `app.data_foundation` import。 |
| 共享保留 | 采集原件与来源配置、共享调度、日志、账户、策略、回测结果、数据库与日志卷、既有市场页面和其原采集 API。其他业务中的独立快照或候选字段不按旧底座名字误删。 |
| 仅历史/限期工具 | Alembic 历史迁移；D03 `app/legacy_reset/`、其专用 `scripts/archive_foundation_runtime.py` 与测试暂留到 R01 完成原件保全、显式 reset/export 验收后再退役。旧任务名还用于维护门禁与退役断言，旧事件中文标题仅供历史 `TaskRun` 展示。旧环境键仅由配置解析器惰性接受以便旧 `.env` 升级，不参与运行。 |

## 检查结果

| 项目 | 判定 | 隔离证据 |
| --- | --- | --- |
| C11：代次变化与旧发布参数 | 通过 | `tests/test_data_store_api.py`：跨页 generation 改变返回 `DATA_CHANGED`；旧查询参数与 body 字段均拒绝；旧路由认证后 410。 |
| C12：双写者、跨容器锁与清理 | 通过 | D01 内核套件在真实 Linux ext4 的任务专用 Docker volume 通过；三容器同卷实测相同数据集第二写者 `LOCK_TIMEOUT`、不同数据集独立取得写锁。未在生产目录运行。 |
| C14：供应商停用后的本地处理 | 通过 | D02/D03 本地来源和 domain 样本测试；新任务 `source_key=None`，仅调用 `NativeSources`，不创建供应商客户端或修改采集开关。 |
| C17：新空库与旧库维护态 | 通过 | 两个任务专用数据库从空库 `alembic upgrade head`；新空库 `phase=ready`，旧任务及排队运行存在时真实应用生命周期启动后 `phase=rebuilding`、旧运行 `skipped`、旧 job 缺席、本地队列保留。D03 显式 reset 的清退/保全测试通过。 |
| 当前 API、共享业务回归 | 通过；1 项跳过 | 带 `--init` 的任务专用容器运行后端整个 `tests/`：2677 passed、1 skipped、236 subtests passed。另有当前 API/调度 31 项、共享业务选定 228 项、D01–D03 选定 137 项通过。跳过项未计为通过。 |
| 前端和自托管配置 | 通过 | TypeScript 检查、Vite 构建、前端 85 项、根级 17 项；桌面与 390px 窄屏在隔离模拟 API 验证目录/详情/预览、筛选、空与错误状态。真实内网页面仅查看登录页，未使用真实 token。 |
| 继承源码扫描器 | 通过（仅源码范围） | 附件原版只读扫描器对已暂存代码扫描 644 个跟踪文本文件，`scan_complete=true`、`errors=[]`、`skipped=[]`、71 个来源条目；剩余命中按上表分类，扫描器不连接数据库也不构成生产删除清单。 |
| 内网部署、付费全库、真实 reset/rebuild/清理 | 未执行 | 开发包无生产执行权限；由 P01/R01 在目标环境按各自权限和新证据执行。 |

## 交付与后续输入

- API：`backend/app/data_store/router.py`、`availability.py`；任务：`scheduler_tasks.py`；CLI：`python -m app.data_store describe|status|rebuild|update|retry|cleanup|audit-export --root <受控绝对目录> [--entry E07]`。CLI 的处理/清理命令需要显式操作者选择，不是部署启动步骤。
- 自托管使用 `compose.yaml` 的 `current_store` volume；`backend/.env.example` 新键 `QF_DATA_STORE_ROOT=/app/data/current-store`。部署时 backend/runner 被固定到共同挂载路径。
- 页面：`frontend/src/pages/DataAssetsPage.tsx`；隔离测试：`backend/tests/test_data_store_api.py`、`backend/tests/test_scheduling.py`、`tests/test_selfhost_env.py`。设计规范仅在本地 `docs/` 更新，未纳入提交。
- D05 应复验最终集成与部署步骤；P01 只部署维护就绪状态；R01 先核对原件与当前限制，再以目标环境新计划执行精确 reset、重建和验收。D03 工具在 R01 完成后退出。

## 未解决问题

- 当前目录能说明已提交的分区与本页业务键，不能从存在的分区推断请求范围每一天均覆盖；API 明确返回 `business_date_coverage_verified=false` 和 `request_satisfied=false`。需要逐日完整性时，后续应依据领域日历与来源契约实现专门证明。
- 真实内网业务数据未用于 UI/性能验收；本包没有验证目标主机的文件系统、容量、既有旧限制的具体映射。D05/R01 按真实环境复核。

## 权限自述

生产访问：未执行。生产部署：未执行。真实任务停写：未执行。生产数据库/文件删除：未执行。生产业务写入：未执行。生产清理：未执行。开发隔离环境仅创建并操作任务专用测试容器、测试数据库与测试 volume。

# 数据底座吞吐修复：部署、切换与验收

本次是程序部署，不是重新定义固定数据范围。生产部署由操作者执行；合并代码和 CI 通过不代表生产回填已经完成。不得覆盖旧来源、工作清单、发布块、正式发布或运行归档；根目录 `docs/` 中的本地交接材料不随本 PR 提交。

## 1. 改动与边界

| 路径 | 改动 | 不改变的保证 |
| --- | --- | --- |
| A 标准化 | 来源预处理页持久化；全部页完成后统一封存跨页重复键；候选阶段只读取当前 200 行 | 原始行输入摘要、业务键、Decimal 精度、字段质量、隔离口径与候选顺序 |
| 发布块验证 | 独立的显式校验语义依赖；完整执行身份仍单独检查 | 真正改变校验依赖必须重新验证；不复制或重命名旧凭据 |
| 冷验证 | 冻结草稿引用；每事务最多检查 200 条完整成员，保存不可变页凭据，最后复算原始块摘要 | 未完成的草稿不发布；租约代次、取消、问题状态、父发布及来源指针仍在原门禁核查 |
| 等待回填 | 固定范围的幂等发布凭据和有界轮转重建；增加在线反向查询及非 ready 索引 | 已处理不等于全部合格；隔离及 OLDER_SOURCE_RETAINED 不算合格结算 |
| 临时暂停恢复 | 显式 ID/版本接管计划；合格完成后恢复；调度同步失败保留待确认状态重试 | 不擅自恢复人工暂停；操作者修改任务后旧接管计划失效 |

正常无中断路径中，2,000 行来源只转换 2,000 次，而不是候选 10 页各转换 2,000 次。发生进程中断时，仅尚未提交的预处理页可能重算。原始来源适配器仍可能一次物化完整来源；末次块摘要核对仍需流式读取完整块的窄字段，但不重复加载/校验全部正文，也不在读取时占住工作行锁。这不是对任意大原生对象的硬实时承诺。

## 2. 部署前检查

保存当前 Git 提交、镜像摘要、执行 ID、固定同花顺 campaign、固定 Tushare capture、暂存代次、活动驱动器名单和调度任务版本。备份数据库与运行归档，并记录原来哪些任务 active、哪些是本次等待回填而临时 paused、哪些是人工长期 paused。

暂停正式化调度任务的入口应使用既有调度 API/UI，等待排队和运行中的调用结束。全量驱动器停止领取新工作并在安全边界退出。旧执行已有的持续更新批次可用仓库已有 `scripts/drain_foundation_updates.py` 沿旧镜像检查点收尾；该脚本只覆盖同花顺持续更新，不能替代 Tushare、暂存导入或全量驱动器的核查。

不要强杀正在提交的进程，也不要把未完成工作改成 succeeded。无法完成的旧工作保留原身份与检查点，明确停用其旧驱动器；需要继续时只能使用匹配的已核验旧运行归档，不得让新镜像冒充旧执行。切换过程中保持通用数据底座 worker 暂停，避免新镜像无范围地领取旧执行工作。回测或其他采集进程不需要因此删除或重建数据。

## 3. 构建与迁移

以下命令在部署主机的仓库目录执行。先按本仓库既有 selfhost 规范处理 `.env`、数据库就绪和服务暂停，不要把密钥粘贴进日志。

```bash
# 从已审查、已合并的 main 快进，不覆盖本地修改。
git status --short
git checkout main
git pull --ff-only origin main
COMMIT=$(git rev-parse HEAD)

docker compose build backend runner
IMAGE=$(docker image inspect quant-foundry-backend:local --format '{{.Id}}')
printf 'commit=%s\nimage=%s\n' "$COMMIT" "$IMAGE"

# 数据库已就绪且业务正式化写入已停止；此命令不启动后端常驻服务。
docker compose run --rm --no-deps backend python -m alembic upgrade head
```

迁移头为 **`20261004_02`**：

- `20261004_01` 增加预处理、分段证明、结算、接管表及保护触发器，不复制现有大业务表。
- `20261004_02` 以 `CREATE INDEX CONCURRENTLY` 建立 4 个索引，独立于普通迁移事务；检查同名索引的完整预期定义，重试可复用有效索引、重建匹配但未完成的索引，不覆盖不相干定义。大表在线建索引仍消耗 CPU/I/O，应在回填限流时进行。

遇到锁超时先定位持锁会话，按受控窗口重试，不要去掉保护触发器或把索引错误当作可忽略。迁移中途失败可以再次执行 `upgrade head`；不要手动 stamp 未执行的 revision。

## 4. 登记新执行与归档

```bash
python3 scripts/archive_foundation_runtime.py \
  --image "$IMAGE" \
  --directory data/foundation-runtime-archives \
  --output "data/foundation-runtime-archives/evidence-$COMMIT.json" \
  --reader-gid "$(id -g)"
```

按既有部署配置将 `QF_FOUNDATION_RUNTIME_IMAGE_DIGEST` 更新为上述 `IMAGE`，确认容器可只读访问归档并具备正确的私有附加组。先启动新后端，保持自动基础 worker/全量驱动器暂停，再在**新镜像**内登记：

```bash
docker compose exec -T backend python -m app.data_foundation execution-register \
  --git-commit "$COMMIT" --image-digest "$IMAGE"

# EXECUTION_ID 填入上一条命令真实返回的新执行 ID。
docker compose exec -T backend python -m app.data_foundation archive-register \
  --execution-id "$EXECUTION_ID" \
  --archive-evidence "/app/data/foundation-runtime-archives/evidence-$COMMIT.json"
```

不得复用交接中的旧执行 ID 来运行新代码；不得手改旧执行 manifest、候选或 Work 的 domain_hash。保留全部仍被历史执行引用的归档。新执行继续使用原来的固定 campaign/capture/暂存代次和正式发布判定，不重新采集、不缩小固定分母，也不把过去的隔离输入从分母中删掉。

恢复驱动器时显式指定新执行和新镜像，保持已审查的 scope 分工及全局 6 槽限制。不要在多个新旧容器中同时无约束地启动相同全量驱动器；在实测之前不要仅靠加并发改成更多槽位。

## 5. 首次冷验证与恢复

首次部署不承认“旧校验散列等于新散列”，因此每个 scope 的旧块会经历一次分段重验。已提交页可跨进程重启接续；全部页、原始块 SHA-256 和完整根清单验证通过后才封存。后续无关 `reconciliation.py`、调度器或预处理实现变更不会再触发同一块的全历史正文重验。

`foundation_record_preparations` 记录预处理封存，`foundation_record_prepared_pages` 记录已提交页。候选工作 cursor 仍只表示已提交候选，不把预处理行数伪装成候选完成。`foundation_record_validation_roots` 标记引用已冻结的草稿；`foundation_record_block_page_verifications` 保存当前校验版本的分段结果；`foundation_record_block_verifications` 才是完整块验证凭据。不能手写这些表来绕过处理。

检查点重试仍走现有工作重试/驱动器入口。数据库中 `queued` 且已有 draft 代表可以继续校验，不代表要重新封存整条历史；只有 `sealed` 才能进入正式发布门禁。容器 healthy/Exited(0) 不代表业务来源已全部合格发布。

## 6. 接管原来临时暂停的任务

先通过原调度 API/UI，把确需切换的任务参数更新为新 execution_id/runtime_digest，保留原 fixed campaign/capture、领域、频率和其他参数。任务更新使用预期版本；不要直接写表改版本。然后在新后端生成候选计划：

```bash
docker compose exec -T backend python -m app.data_foundation.update_resumption plan \
  --plan /tmp/foundation-resumption-plan.json

docker compose exec -T backend cat /tmp/foundation-resumption-plan.json
```

**人工核对这份计划**：只保留已确认属于本次等待回填而暂停的任务；不能仅因为最新结果是 waiting_backfill 就推断允许自动恢复。计划仅包含任务 ID、版本、参数摘要及识别信息，不包含密钥。文件使用独占创建，不覆盖上次已审查计划。

将审查后的文件放回后端容器，再显式接管：

```bash
docker compose exec -T backend python -m app.data_foundation.update_resumption enroll \
  --plan /tmp/foundation-resumption-plan.json --apply
```

接管不会立即强行恢复。产品调度器每分钟以小批次核查；达到**全部固定来源合格发布**后才使用原状态变更服务恢复任务。空范围还要有独立的正式空范围发布；隔离来源未修好就继续等待。切换后任务版本或参数再次变化会取消旧接管，不覆盖操作者新决定。恢复写入成功而调度同步失败时保留 activated，下一轮重试并确认，不丢失恢复动作。

既有本地恢复脚本应在接管清单确认入库后停止对同一批任务写入，避免两套恢复器竞争。原本正常 active、人工 paused 或未明确列入计划的任务不由此功能改动。不要自动“恢复全部 paused”。

## 7. 上线后验收与资源决策

分别记录两大 Tushare scope 的连续 2,000 行分块：A 创建至完成、B 创建至 sealed、sealed 至 published、单事务最长耗时、租约过期次数、实际吞吐。冷启动重验样本和暖态样本分开，至少观察多个连续分块；不得用单个最快样本承诺全量完成时间。

确认旧发布固定查询结果未变化，新发布的业务键/字段值/质量与固定输入一致；异步页证据未削弱对账、issue/head/来源指针门禁。关注 `PREPARATION_INVALID`、`MANIFEST_INVALID`、`LEASE_LOST`、`DEPENDENCY_MISSING` 和接管 reason，不要对生产 Docker 日志做全量扫描。

结算缓存初次回建可能保守落后于已发布事实；返回的 provisional 计数说明的是已核验结算边，不是最终全量业务验收。正式完成度仍以原固定范围、published 记录、全 ready 候选及独立空范围凭据对账；隔离和 `OLDER_SOURCE_RETAINED` 中间状态不能算成功。同花顺版本、Tushare 分块、暂存主体三个单位不相加。

性能测试通过不解决不完整来源本身：例如交接中的 `fund_bond_history` 未完成请求继续保持隔离，不能用旧成员替代。最终使用指定验收工具包对真实数据库和 HTTP 服务执行只读验收；这属于部署后的生产验证，不包含在 PR 的合并结论中。

## 8. 回退

优先暂停新驱动器并保留检查点、页证明、结算凭据与全部运行归档。应用回退只能运行与旧执行完全匹配的镜像，并保留已经增加的兼容数据表。新增证据表存在记录时迁移拒绝破坏性 downgrade；不要 DROP/TRUNCATE 审计证据来强行回退。需要修改校验语义时用新版本追加验证或新正式发布，不能覆盖旧发布。

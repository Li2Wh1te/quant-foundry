# D17 退役与精确临时对象清理

旧 `backend/app/backtesting/`、`backend/app/strategy_protocol/` 已物理删除。
D13 的账户、策略关联、历史只读层继续使用原表；共享日历与身份查询已脱离旧执行目录。
历史 Alembic 链保留，命名日历 seed 在迁移目录中自包含。生产切换属于 P01，生产残留执行属于 C01；
本模块及下列命令均不授予生产执行权限。

## 只读引用清单

从仓库根执行：

```bash
python scripts/scan_legacy_dependencies.py --repo . --max-bytes 33554432 --output /tmp/d17-inventory.json
```

输出文件须不存在。工具扫描 AST、相对 import、模块字符串、动态导入调用、入口、容器命令及前端写请求。
`complete=false` 会以 2 退出；扫描不能证明任意计算字符串安全，启动、迁移和真实 worker 回归补足这一边界。
文档中的历史定位和鉴权后的旧只读 URL 是审查项，不是执行入口，也不是删除许可。

## 默认 dry-run 与受限 apply

使用 backend 的锁定环境。数据库连接必须由单独的环境变量提供，工具不读取应用 DSN 作为隐含清理目标。
下面命令形态已在本模块自建、可销毁的数据库及目录夹具中验证；路径与库名须替换为精确目标：

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/backtest_cleanup.py \
  --database-name qf_d17_test_EXACT_ID --root /tmp/qf-d17-private-EXACT_ID/retired-work \
  --path finished-attempt --table public.d17_disposable_EXACT_ID --output /tmp/d17-plan.json

PYTHONPATH=backend backend/.venv/bin/python scripts/backtest_cleanup.py --apply-plan /tmp/d17-plan.json
```

先在 `QF_D17_CLEANUP_DATABASE_URL` 中提供该隔离库连接；不要把口令写入命令或报告。
`--database-url-env` 可选择另一明确变量名。默认只读事务，除显式指定的新输出文件外不写数据。
apply 只接受同一个计划文件，重新检查当前身份、引用、活动状态和文件内容；不会把旧计划直接当删除许可。

工具仅接受此前已由可信所有者明确分类的临时对象，不会自动把现存目录、表或业务数据标记成可删除：

- 私有根 `.qf-retired-temp-root.json`：`kind=qf.retired-temp-root.v1`、UUID `root_id`、
  `database={name,oid,system_identifier}`，绑定 PostgreSQL 实际库与集群身份。
- 直接子目录 `.qf-retired-temp.json`：`kind=qf.stopped-legacy-worker-temp.v1`、相同 `root_id`、
  旧运行 UUID `run_id` 和精确临时 `files` 列表。运行必须已有确定终态、结束时间与退出码。
  标记不替代活动进程、文件引用及 Docker 挂载检查。
- 表只能是明确、空的专属普通表；表注释为 JSON `{"kind":"qf.empty-legacy-temp.v1","root_id":"同一UUID"}`。
  表所有者、OID、列定义复核；业务表即使空或被加注释也永久排除。

未知分类保留并拒绝，不以目录/表名前缀推断专属。历史数据没有自动标记或自动回填接口。
当前实现不删除容器、镜像、卷；这些候选交 C01 另行精确核验。

## 拒绝与保护范围

拒绝错库/集群/根目录、软/硬链接、路径替换、共享运行/数据/结果根、未知文件/表/视图/schema、
非空表、任何约束/FK/触发器/依赖、存储引用、源代码引用、活动或未知终态、存活 worker、
停止容器仍存在的 bind/volume 引用及无法检查的进程/daemon。错误只给稳定代码，不泄漏 DSN、SQL或密钥。
跨表实际引用读取限 10,000 行/32 MiB，文件限 4,096 项/1 GiB，目标最多 64 个；超限拒绝，不截断后声称安全。

apply 锁住根目录并以 NOWAIT 锁定引用表/目标表，重扫后逐个精确 unlink/rmdir 或 `DROP TABLE ... RESTRICT`。
它不执行 `CASCADE`、业务行删除、通配符批删或 prune。文件系统与数据库不是联合事务；执行期间异常可能留下
部分已删除的专属临时文件和回滚的表删除，需重新 dry-run；绝不继续清理未知对象。

策略文本/草稿/修订、账户和费用版本、完成与失败结果及错误、保存比较、R01 正式/原生数据与共享调度均保护。
新 worker 的正常回收继续由 D12 负责。D17 未执行内网、供应商、部署、生产切换或生产清理。

## 已落实回归

```bash
cd backend
POSTGRES_TEST_ENABLED=1 uv run --locked --no-sync python -m pytest -q tests/s3_retirement_suite.py
QF_S3_D13_REQUIRED=1 QF_S3_D14_REQUIRED=1 uv run --locked --no-sync python -m pytest -q tests/test_s3_run_service.py tests/test_s3_cli.py
```

需已配置 test 环境的隔离 PostgreSQL、Docker 及测试 wheel。必要进程权限不足时安全工具拒绝，不能隐藏检查。
`QF_D17_EVIDENCE` 输出历史升级的完整原值对照、dry-run/apply 精确对象与保护行、拒绝场景；CI 保留为
`s3-retirement-evidence` artifact。真实 runner/worker 与浏览器继续走原有独立工作流。

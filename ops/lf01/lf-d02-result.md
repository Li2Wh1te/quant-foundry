# LF-D02 v1.1 结果

状态：**ready_for_review（代码提交阶段，完整验收未执行）**。
实际提交：见本文件所在提交及 `codex/lf-d02-local-adapters` 的 Git 历史；本轮不合并。
前序输入：`1f155a8a975e9107f1246a1698a5a21cb7a21642`，LF-D01 PR115。
环境：开发隔离，2026-09-25。用户最新要求为“写完先提交，不用做太多测试”；
因此未申请/开启全量 CI、未跑容量大测，未将未执行项目标为通过。

## 已提交的实现范围

71 个原映射入口全部有代码处置：60业务、7来源辅助、3导入通道、1空状态。
注册、实际reader、纯领域转换/校验、schema及限制可定位于 `lf-d02-disposition.csv`。
原始基线CSV与source_mapping.json保持原样，完整性记录位于 `lf-d02-baseline/integrity.json`。

同花顺全原生观察及delta依赖；Tushare只读一致表快照和后续补扫；D03自包含救回reader；
真实日期点位/完整报告分解；逐目标可比较确认、当前问题和明确撤回；同一rebuild/update/
retry路径、有限外部归并、原子目录/问题/checkpoint提交、generation并发检查。
保留合法空/缺项/稀疏更新的区别、复杂成员的完整性、字段单位/时间限制和精确JSON往返。

CLI及D04内部能力/对象读取入口已提供；新 `20261006_01` 只添加当前入口状态表。
导入通道不产生第二份正式进度数据；真实业务继续来自已校验原生输出。
原生采集附加“实际返回键”小凭据及导入原始开始时间，用于避免把相同值的新确认丢掉，
或把完成时间错误赋给继承历史。此改动不触发供应商请求，不改变来源启停。

关键后续接口、命令、上限和未证实语义见 `lf-d02-interfaces.md`。

## 本轮实际检查

**通过：30项小型测试，9.33秒**，原始摘要 `evidence/lf-d02-smoke.txt`。
Python3.12.2、Arrow23.0.1、DuckDB1.4.3、PostgreSQL17.11（隔离随机schema）。
文件系统为overlay，沿用D01测试专用探测替换；真实PG、文件、flock、Arrow和DuckDB运行。
不是ext4/XFS/Btrfs持久性或跨容器认证。普通测试DuckDB限制64MiB，B05小冒烟128MiB，
生产默认512MiB未放宽；没有绕过运行预算检查。模块编译、所有业务schema生成与CLI
帮助/describe离线导入也检查通过。

| 原编号 | 本轮检查边界 |
|---|---|
| C02/C07 | 小型真实PG/Parquet流程：X1→X3→Y2→更早→Z4；旧输入补缺历史键。通过。 |
| C03/C04 | 旧坏不阻断新好；新坏限制目标、无关主体可读；目标修复后解除。通过。 |
| C05 | 关键转换失败/重试机制有代码及小型回归；未逐领域做规则修复全集验收。 |
| C06 | 完整成分集合A坏，修改B/排序仍受限；A修复后可读。小型流程通过。 |
| C08 | 稀疏返回凭据、明确撤回防旧值复活、合法空完整集合。小型检查通过；不宣称所有来源空值组合覆盖。 |
| C09 | 同纳秒不同序号及38位Decimal经过真实文件/DuckDB/内部JSON读取。通过；公共HTTP在D04，不宣称该端点已验收。 |
| C10 | 完整报告跨多个物理文件、尾部成员错误不发布有效兄弟。通过。 |
| C14 | 本地只读快照期间晚提交/更正，下次补扫可见；禁止HTTP请求、停用标记不变。小型检查通过。 |
| C16 | 静态不兼容检查、规则改变被拒绝、显式有界重建。小型检查通过。 |
| B05 | 执行器已实现；只运行32条+2个冷输入的代码冒烟，1000万条**未执行**。 |

测试命令（backend，隔离环境变量另行设置，不包含真实密钥）：

```sh
python -m compileall -q app/data_store app/data_ingestion/tonghuashun/confirmation.py \
  app/db/migrations/versions/20261006_01_local_entry_status.py scripts/benchmark_local_ticks.py
python -m pytest tests/test_data_store_local_adapters.py \
  tests/test_data_store_local_pipeline.py tests/test_data_store_schema.py -q
python -m app.data_store describe --entry E50
```

迁移本身在随机测试schema执行，旧6张内核表保留；新增表写入后自动downgrade被拒绝。
全迁移链、全部后端/前端回归、全领域原生fixture、跨容器、B05大负载及最终CI均**未执行**。
整体验收应在后续合并前按最新授权补足，本报告不将部分小样本替代所有C/B验收。

## 明确限制与待验证项

- 旧合并窗口缺少实际返回键时不能证明相同值的新确认，保留
  `SOURCE_CONFIRMATION_UNPROVEN`，不依据请求范围猜测；所有对应adapter已写。
- 未知单位、历史公开可用时间、ETF复权锚点等按现有纯转换和字段限制保留，不统一猜测。
- 超过来源/报告/异常文件/暂存/时间预算会返回incomplete并保留当前限制；不能计为空或合格。
  没有完整错误键清单的overflow不能仅凭一次小重试自动解除。
- 补扫按有限目标分区处理，可能重复读取原件；未测全量原生吞吐，不能给出完成时间。
- D02未接默认调度/公共API/完整UI，未实现D03清退；不得按本包标为整个LF-01完成。

## 权限

生产访问、部署、停写、生产迁移、reset apply、业务rebuild、原件/旧发布删除、
容器/镜像/卷清理：**均未执行**。只写开发分支，不合并main，不开启自动部署。

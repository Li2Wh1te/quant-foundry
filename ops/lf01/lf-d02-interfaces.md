# LF-D02 v1.1：本地适配、处理与后续接口

前序实际提交：`1f155a8a975e9107f1246a1698a5a21cb7a21642`（LF-D01）。
本包只增加本地处理能力，不切换公共 API、默认 runner 或共享调度，不删除旧体系。

## 1. 注册与入口处置

唯一注册在 `backend/app/data_store/adapters/registry.py` 的 `ENTRIES`。
`BY_ID`、`BY_NATIVE`、`Entry.describe_capability()` 供 D04 使用。
71 个旧映射为 60 个业务入口、7 个来源辅助入口、3 个导入通道、1 个旧空状态入口。
业务入口有纯转换函数和明确模型，不从供应商任意字段动态猜单位。
`Layout` 只编译代码内已经声明的业务模型，不是可编辑映射 DSL。

原始处置 CSV 和 `source_mapping.json` 原样保存在 `lf-d02-baseline/`。
实际逐项代码定位见 `lf-d02-disposition.csv`。E61/E64 改为实际公司行动/交易状态，
不再使用旧 operations 数据集名称；E01–E03 路由到 E50/E22 的真实行情/事件。
导入 reader 检查本地导入状态，但不会把未完成校验的暂存直接写成正式业务数据。

所有时序记录保留真实日期；报告以真实报告期/集合为完整性单位，成员跨物理文件
仍全有或全无。无可靠业务时间的行情快照/叙述仍是当前观察，不把供应商响应时间
猜成交易日期。经理任职区间保留为原文，不推断“至今”等词的经济生效时间。

## 2. 本地输入协议

`local_sources.NativeSources(engine, limits=SourceLimits(), cancelled=None)`：
每次 `iter_entry(entry)` 开一个新的 REPEATABLE READ、READ ONLY 快照；服务器游标
逐条取回原生行，单行 UTF-8、依赖链、SQL、整个 pass 均有有限预算。
Tushare 直接读取白名单中的 13 张原生表，与来源 enabled 开关无关，不构造 client。
主键/时间不是永久水位；后续 update 从新的快照补扫，因此早 ID 的晚提交、更正仍可见。

同花顺按 dataset/subject/variant 的原生观察历史读取，复原所有必需 delta 依赖，
逐层校验原生内容摘要、范围和链长，保留原始重复键供领域校验；不只读最新窗口。
原生 collection state 的失败和旧成功数据分别处理，不把失败当作合法空响应。
新失败无法定位业务对象时，限制对应主体/口径；能定位的报告/点位仅限制该对象。

`LocalInput` 明确 source、dataset、subject、variant、观察/快照时间、内容及摘要。
`row_basis` 是内存中的真实逐键确认依据，不保存第二套逐条治理台账。
`normalize(entry, LocalInput)` 产出完整 `Unit` 或带稳定原因码的目标问题。
普通空时序只表示没有返回点；缺项是错误，合法空完整集合可替换该集合；
`LocalInput.withdrawals` 必须由内部调用者给出明确业务键和原件证明。
原生 delta 的 removed、目录短窗口、缺项均不能自动转换成业务撤回。

## 3. 同值确认、稀疏更新与证据不足

比较只发生在可比较 source/dataset/subject/variant/order_kind 内，按实际键或完整报告。
Tushare 可变表以当前只读快照确认，固定版本公司行动使用 `fact_version`，不是自增 ID。
同花顺基于原生观察及其请求证据；新确认的相同值仍更新有效依据，不能被介于两次
确认之间的旧不同值覆盖。同时间不同有效值明确冲突，不用 UUID 或到达顺序选胜者。

`data_ingestion/tonghuashun/confirmation.py` 增加有限的实际返回键原生凭据：
`actual_returned_keys_v1`。仅含明确键，不复制价格/报告正文或密钥。
普通 acquisition 在已有请求完成后保存它；导入器只记录实际接受的日期组及原始
导入开始观察时间。继承的历史点不能获得整个主体的完成时间。
本地 rebuild/update 不调用 acquisition，不修改来源开关。

旧采集原件若已经混合历史行，且只有请求范围、没有实际返回键，则有时无法证明
某个相同值是否被本次重新确认。此时记录 `SOURCE_CONFIRMATION_UNPROVEN`，不猜测
相同值就是旧值或新确认。已证实未涉及的历史键保留原依据。后续带明确键的原生
观察，或能校验目标值与原始请求证明的救回材料，可通过相同 pipeline 重试。
这属于原件语义受限，不是缺少该领域 adapter；不会为通过进度而解除限制。

## 4. 当前存储、问题与断点

`pipeline.run_entry(store, entry, sources, options=PipelineOptions(...))` 与
`run_local(...)` 使用同一套路径，mode 为 rebuild/update/retry。
每个来源 factory 必须在每个 pass 返回完整且一致的已声明输入范围，不可把
未读完页声称为 complete。只读合成 B05 source 可明确声明序号范围。

先在 D01 计入 staging/spill 预算的临时 SQLite 中按目标分区归并原材料，再加载
相关当前文件并合并，最后调用 D01 原子 replace。临时库退出后回收，不是第二份
长期 DuckDB 全库、历史正式副本或逐次运行台账。分区过多会多次补扫；这是有界
空间换取额外读取，不是全量性能承诺。source_rows/normalized_units 统计实际扫描/
转换次数，重复 pass 会累计，不是全库唯一行数或覆盖率分母。

每个当前对象内包含紧凑 basis、有效/撤回/无效状态。可选字段限制只在对象根记录
保留有界 `quality_json`；不是逐成员成功日志。当前文件、问题、来源范围状态及
checkpoint 同事务切换。快照合并后使用 dataset generation fencing 防止并发丢失更新。
同输入/规则/上下文通过 token 无操作；不同依据但相同当前文件内容可只提交控制状态。
崩溃后重新扫描、归并和检查已提交 token，不盲目重放。

新错误默认限制对应能力，旧展示值即使保留也带 invalid，不进入合格读取。
旧坏输入不覆盖新好值；未解决旧错误可以保留为已解释、不阻断当前读取的问题。
较新的有效完整对象或同原件成功重校验，只能解除该目标问题；修改另一个报告/
成员而目标仍错误，不解除问题。当前 issue 更新次数/时间复用 D01，不追加无限行。

异常超过控制预算时生成 `.problem_samples/eNN.jsonl` 当前聚合错误文件（每入口最多
4 MiB、总共64 MiB），并持久化范围限制、返回 incomplete。只有完整键清单且每个
阻断目标已重新校验，才能自动清除 overflow 限制。清单本身超过上限/损坏/缺失时
保持受限，需要更小范围重处理或经维护者审查恢复原始错误证据，不能将截断算通过。

## 5. 内部读取与后续 D04

`domain_reader.read_object(store, entry_id, representation, subject, object_key)`
读取目录指定分片、锁定 generation 分页，重组并再次校验完整成员对象，返回
`data`、`field_quality`、当前确认和静态 capability。超总字节/成员上限明确失败。
`CurrentStore.read(entry.spec, Query(...))` 可读取有界时序点，金额/大整数保持精确。
D02 的 `object-key-v1` 问题按分区和业务键前缀限制，无关范围仍可读取；不理解的旧
问题不默认放行。basis_state=invalid/withdrawn 不进入合格业务读取。

字段限制如未知复权锚点、供应商口径/时间意义、原生小数文本不支持 SQL 算术等
继续声明，不把“可显示来源值”冒充“可用于所有研究/交易计算”。同花顺 ETF
前复权与 Tushare 原价/因子不自动拼接，不做跨源对账或备用补缺。

`pipeline.read_entry_status()` 读取新表 `data_store_entry_status` 的每入口当前摘要。
本包迁移 `20261006_01` 在 `20261005_01` 后只加该表，不重写旧迁移、不删除原件。
metadata 纳入相同结构；已填充的降级需要审查，不自动删除状态。
CLI 的 describe 不连接数据库；其他命令仅在显式调用后访问配置的本地数据库。
没有公共 GET、应用导入或默认任务触发写入。

## 6. D03 最小救回格式

`RescueSources(paths, ...)` 只读有界普通 JSONL 文件，最多128个文件，总字节有限。
每行 `format=qf-local-rescue-v1`，必须保留原生记录，不接受旧正式 payload 冒充来源。

- `kind=ths_observation`：entry_id、原生 record（dataset/subject/variant/id、observed_at、
  content_hash、data_json、request_json、base_observation_id），以及闭合 dependencies。
  文件序列按原生 scope/time/id 有序；每层摘要、依赖身份和时间验证。
  可选 row_basis 的每个 token 必须指向保留原件，而且原件明确确认同一个键与同值；
  仅凭人为填写时间/摘要不能解除原件的未证实确认限制。
- `kind=table_row`：entry_id、原始 record、snapshot_started_at、content_hash。
  Decimal 用精确 JSON 数字/字符串，禁止二进制 float 往返；完整本地表读取由 D03
  对一致快照负责。来源版本公司行动须保留 fact_version/logical_fact_key。

D03 的已知当前限制不能因搬文件丢失。该 reader 不授予任何生产删除权限。

## 7. 显式命令（由维护者在合适环境使用，本轮未执行生产）

从 backend 执行；沿用环境中的 QF_DATABASE_* 与 QF_CURSOR_SIGNING_KEY，勿把真实
密钥/连接串写入命令回传。首次使用前，维护者先按迁移流程建表，并提供支持的可信
本机共享挂载。CLI 不执行迁移，也没有 reset 参数。

```sh
python -m app.data_store describe --entry E50
python -m app.data_store status --root /trusted/current --entry E50
python -m app.data_store rebuild --root /trusted/current --entry E50 --initialize
python -m app.data_store update --root /trusted/current --entry E50
python -m app.data_store retry --root /trusted/current --entry E50 --partition 2026-01.b00
python -m app.data_store rebuild --root /trusted/current --entry E50 --allow-incompatible-rebuild
python -m app.data_store rebuild --root /trusted/current --entry E50 --rescue /trusted/rescued.jsonl
```

分区名以实际元数据为准，示例不是生产范围授权。不指定 entry 会按71入口处理。
每次 pass 默认只选择一个目标分区、最多256pass/每pass300秒；超预算不是成功完成。
Schema/规则不兼容须显式局部重建，不能把旧文件按新模式解释。

B05 执行器 `backend/scripts/benchmark_local_ticks.py` 默认1000万条，真实经过合成
adapter 和相同 pipeline，按交易日/会话/通道/十万序号范围分区，不每个事件建文件。
代码检查重复纳秒时间的独立序号、38位Decimal、冷热文件及实际计数；默认要求
本机隔离测试数据库和真实受支持挂载，没有文件系统探测替换，也不连接供应商。

```sh
QF_ENVIRONMENT=test python scripts/benchmark_local_ticks.py \
  --work-dir /trusted/isolated-bench --output /trusted/new-b05-result.json
```

本轮未执行该1000万条负载；32条开发冒烟不等于 B05 验收通过。分钟/小时结构通过
同一注册中的合成 IntradayBar 表达，不创建新增收费供应商接口或预先派生全周期缓存。

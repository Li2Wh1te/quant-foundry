# S3-D05 RESULT

实现已完成，真实隔离查询链通过；正式生产联调未执行且必要事实仍缺失。
基线 main：3448d12e9e8175c67a7e7e5b777d7e035a2a16c3。
分支：codex/s3-d05-research-api。精确最终 head/草稿 PR/CI 链接随交付回传。

## 实现与共享影响

- 七个公开数据 API、固定长表/空表、TimeLike、每标的 count、精确 Decimal/ns、
  财务公开时间、有效期、稳定身份、白名单筛选/排序/limit 均已实现。
  模拟时点默认截止；Rust 使用 D03 完整发布键，裁剪同 ns 未发布事件和未来预取。
- 研究复权仅接受经过验证的累积因子及原始锚，排除未来因子；不修改撮合输入。
  分钟/60m 使用原 D03 会话桶，不跨午休、不升频；短桶/缺失独立标记。
  指数点数经研究 schema 保持原单位，不塞入 CNY/份额成交格式。
- SMA/EMA/Wilder RSI/ATR/MACD/sample rolling_std/平均同值 rank 在 Rust 计算，
  NumPy/Pandas 薄胶水保持索引，float64 指标与 Decimal 原价分开，缺失不前填。
- CurrentStore Query 窄增集合 IN 和每组 count，游标在分组排名后分页；旧默认签名
  游标不变，仍走原质量、锁、generation 和资源门禁。D04 网关增加同路径集合读取，
  不逐标的 N+1；原 Arrow 平坦布局预检复用，未新增 IPC 协议或底座判断。
- 多批窗口预算独立于64MiB单批 IPC；默认视图仍10000行/64MiB，可信宿主可从
  run 总预算配置至100万行/1GiB。扩大视图必须显式分配包含转换、缓存和 IPC 的数据
  份额；行信用保守覆盖拥有的投影和转换峰值，不把共享 Arrow backing 按列重复累计。
  reset/expire 释放真实 Vec 容量。多标的成员筛选使用有界集合，D04 行情解码未替换。
- ResearchDataGateway 复用 D04 declare/check/read/finalize/serve；研究映射拒绝未知
  公开时间/历史成员/因子锚点。窗口、缓存、控制/Arrow、取消均有界，缓存命中仍验依赖。
  SDK 的旧七个占位拒绝已替换；其余未实现模块仍保持原拒绝，不退回旧 Python 引擎。
- Tick.sequence 在公开类型中明确可为 None：来源序号未知时保留未知，D03 稳定输入
  序仍保留内部事件身份，不能伪造来源序号。Python/Rust 声明同步。
  wheel 锁加入与既有 backend 相同的 NumPy 2.5.2/Pandas 3.0.5；backend 锁未改。

字段、参数、公式及可执行例程： [RESEARCH.md](RESEARCH.md)、
[examples/research.py](examples/research.py)。

## 实际验证

1. `bash scripts/test_s3_engine.sh`：rustfmt、clippy -D warnings、workspace Rust 测试
   91 项、公共合同/版本检查、根 Python 18 项、Python 3.12.2 release wheel 构建通过。
   D05 Rust 6 项用独立手算 SMA/EMA/RSI/ATR/MACD/std/rank 与 Decimal 比较验证。
   实现阶段 wheel 的 SDK 28 项通过；合并前修正后 workspace Rust 92 项、
   独立消费 venv 重装 wheel 的 SDK 29 项通过，clippy 无告警。
2. `cargo build -p qf-core --locked --example consume_gateway` 后，在独立 PostgreSQL
   16 容器及随机 schema 中运行 `python -m pytest -q tests/test_s3_data_gateway.py
   tests/test_s3_research_api.py`：62 passed，30.67 秒，0 skipped（D04 43、D05 19）。
   `QF_S3_INTEGRATION_REQUIRED=1` 强制真实 Rust IPC 消费者及已安装 wheel。
   最后容量修正后重建消费者/wheel，再运行 `python -m pytest -q
   tests/test_s3_research_api.py`：20 passed，16.60 秒，0 skipped。新增5000×20窗口
   加5000个当前事件的105000行 smoke，输出10万点；单批超10000行仍拒绝。
   这不是 D18 全量 B1。覆盖七 API/执行研究示例、200 标的集合读取、缓存复用、公开时间晚于期末、当前
   名称/成员不填历史、同 ns 发布键、预取/过期视图、双边数量/stale/未知停牌、
   复权未来因子/缺锚拒绝、跨午休/末短桶/缺 Bar、不升频、精度和资源/取消。
3. 空隔离库 `alembic upgrade head` 通过；`python -m pytest -q
   tests/test_data_store_{kernel,local_pipeline,api}.py tests/test_account_workspace.py
   tests/test_backtesting_account_{profiles,profile_storage}.py tests/test_strategy_storage.py
   tests/test_backtesting_{result_api,pagination}.py`：156 passed，80.25 秒，0 skipped。
   验证共享 CurrentStore 的原 JSON/分页、账户/策略与旧已保存历史结果消费。
4. 本地文件系统为 overlay，沿用现有 LF_D01_ALLOW_OVERLAY_TEST=1 测试 probe；
   实际 PostgreSQL/Parquet/DuckDB/Arrow/socket/原生扩展都运行，不能冒充受支持生产
   挂载验证。Current store isolated acceptance 的支持挂载检查由本 PR CI 另行执行。
   不将其描述为原 D04 merge 的补验。未运行 D18 的 B1–B4 全量产品基准。

## 合并前审查与修复证据

64d6d2b 的三项 PR CI 虽通过，审查仍发现下列语义/资源阻塞并修复，不以旧 CI 代替新 head 验证：

- 后复权错误地用第一条现存因子为锚。旧 wheel 上独立反例实际返回10而预期20；
  ResearchBinding 现在必须提供 factor_anchor 的原始值、真实公开/生效时间，Rust
  不猜基准。pre 要求终点有效因子；未来锚/因子、过期因子和歧义修订明确拒绝。
  先复权细 Bar 再聚合，来源 Bar 内的因子变化或覆盖缺口拒绝；按标的分组因子。
- 历史 count 原按存储键排名，可能选错同刻不同通道事件。Query 窄增白名单
  group_order 排名列，按完整事件顺序选 N，再按原键分页；原默认游标签名不变。
  同 ns 当前事件仍先在 Rust 裁剪后计数；price/current/research 缓存命中均实测
  拒绝不增 generation 的质量变化。同边界的缓存不被字段修改或下一个发布键污染。
- 无历史成员依据不再返回空名单；明确空集须有已公开有效的空集记录，完整成员
  超 limit 拒绝，不截断。财报同报告期/公开时点的歧义修订不按 record_id 猜选择。
  当前 ns 的细 Bar 未发布时不输出完成目标桶。MACD 空集/预热列也保持 float64。
- 视图原 reset 保留 Vec 容量、SDK 整表重复 JSON 图及1GiB未含转换/IPC份额的问题
  已修复。扩大窗口须显式 run_data_budget_bytes；原生工作集信用包括转换余量，
  reset/expire 真正释放容量。独立 native/SDK Arrow replay smoke 每窗口105000行、
  输出100000点，保留首个 DataFrame 再做第二次查询（共210000 replay 行）：
  峰值 RSS 748265472 B（713.6MiB），工作集信用823147392 B，数据份额1218445312 B
  （1162MiB），视图上限1GiB，reset 两项 Vec 容量均0。此 smoke 不包含 CurrentStore
  存储读取/父进程/cgroup，不冒充完整 run/D18 验收；D10/D12 必须落实总体预算。
- 最后重装 wheel/重建消费者后，`python -m pytest -q -s tests/test_s3_data_gateway.py
  tests/test_s3_research_api.py`：74 passed，34.32 秒，0 skipped（D04 43、D05 31，含
  真实隔离链与上述原生/SDK smoke）。RSI/ATR/MACD 缺值后重新预热用独立手算回归。
  最终精确 head、PR CI 与 merge 后状态通过同一交付回传，不写自引用 SHA 台账。
  共享 CurrentStore 窄改动另复测 kernel/local_pipeline/api：78 passed，53.83 秒；空库迁移通过。
  TimeLike 还修复了 Pandas 对第10位小数无声截断：非 ISO/超 ns 精度字符串拒绝。

## 接缝与未完成条件

- D10 必须用 `_callback(session,boundary)` 包住每个回调并 finally 退出；boundary
  使用 D03 now_ns/完整 market_through。ReadView lease 过期清缓冲并拒绝跨回调调用。
  已物化过去 DataFrame/Decimal/冻结 DTO 可保留。D10 可传入经过接受的当前行情及
  完整键，get_current_data 在内存裁剪且不逐 Tick 调父进程；D10/D12 仍负责引擎块
  依赖复核、受限 worker 和私有连接认证。本包没有创建另一个宿主/事件循环。
  扩大窗口要从 run 总额分配 run_data_budget_bytes；常驻引擎/账户/父进程及策略
  主动保留的结果由 D10/D12 的总体资源预算和进程组/cgroup 约束，不能只看视图上限。
- D11 不得覆盖 data/views.rs、analysis/indicators.rs。D13 的成功终态继续使用
  原 `finalize(context,commit(connection))` 单事务接缝，不拆为检查后单独提交。
- 正式 E14/E15 缺真实历史公开时间，E39 不能用当前观察快照填历史估值；E56/E57
  缺历史有效分类及完整成员集合；E40/E67 目录不能代替历史稳定身份/名字事实；
  E69 缺已接受因子锚点及公开时间。这些入口继续 CAPABILITY_UNAVAILABLE。
  仓库既有身份事实服务的 known_at/observed_at/effective_at 并非缺失；本包未验证其
  生产记录或把 E40 目录自动转成历史事实。确认事实后用同一 ResearchBinding 接入。
- D04 正式 E50 raw 已知但币种/整数份额依据未接受，E51 前复权未锚定，E52 缺
  研究点数/闭市可见依据，E70 元/手/千元已知但 raw/份额换算仍缺依据。映射未启用，
  合成接受事实仅为隔离 oracle。代码/隔离链 ready 不表示正式市场全频可用。
- 未部署、访问生产/内网、调用供应商、改来源开关/凭据/安全设置、执行 R01 reset。
  没有市场文件副本/快照/逐行成功台账；未编辑 S2 公共 UI/采集器。
  旧回测数据适配仍被旧运行/API 消费，本包未提前删除它或共享业务；D12/D13/D17
  完成调用退出后清退。策略、账户、历史结果及正式市场数据未作删除。

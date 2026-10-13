# S2/S3 review 修复状态

基线：`main` `50db997f8f50b955a6a5fe0ce237543d33b5f60b`，继续前再次 fetch 核对主干未变化。草稿 [PR #180](https://github.com/Li2Wh1te/quant-foundry/pull/180)，不合并，交父核验。本会话未开子会话，未生产部署、写入/清理生产数据、调用供应商或操作凭据/权限。R01、S2 批次及旧核心退役边界保留。

## 提交与结果

| 项目 | 精确代码提交 | 结果 |
|---|---|---|
| SR-03 逐标的完整 Bar count | `d47f308ab34cc6d4f8cefbf8796ee9d5f5ed387f` | A 取 H2/H3，B 取 H1/H2；有界向前补取，历史耗尽与预算不足分开；无前填/补零 |
| SR-02 普通 FormalProvider 研究装配 | `995c5a74dc636adf6bd9e1e7532dd04b711d0b8d` | 路由由 plan 的有界 bindings 派生；D05、行情、研究和最终事务共用 CurrentStore/context；参考指数保留 index_points |
| 受影响完整 B2/60m 选择 | `daf479c784519b75fe2c6dcc685bdf476c46e5c3` | 保留已证明的 B1–B4；本分支只重测完整 B2/60m，明确 full_B1_B4=false |
| 并发质量变化终态/测试镜像上下文 | `d765607a9542272363218bd7193274cab308069c` | 已接受 context 发生变化时保留 DATA_CHANGED 终态；修正研究 fixture 的 UI 镜像上下文 |
| SR-01 默认 SQL 桥接/动态诊断 | `b7963c0b56317c4cf7b05affd7f3cc6eea0c66c5` | API/runner 默认安装同一 SQL resolver；现有事实可解析；能力不再整体固定 False；完整默认成功仍未关闭 |
| SQL 预算/CLI 隔离 DB 与诊断契约 | `953076d7b35f85886a7e6ffa2c133051f0437e93`、`55bde7ed363e05f237618a36be1b4165bbb26ec9` | SQL timeout 报 RESOURCE_LIMIT；CLI 用私有 DB；有界事实诊断编码于 ErrorDTO 字符串 scope，不扩大共享协议 |

## 默认桥接的真实范围

`formal_resolver.py` 通过现有 repository/解析器读取身份和代码映射、严格命名日历、ETF 规则包、停牌事实/覆盖、公司行动/覆盖及已捕获的账户佣金。保留 validity、known_at、质量、版本及证据；不从代码后缀猜身份、从身份有效期猜上市日、从 ETF 大类猜 Product 或从缺停牌行猜可交易。

日历补齐已有输入可完成的代码：整周/月显式自然日覆盖产生交易序号、午休区间、下一/随后会话和稳定 reference；Rust 原 SessionCalendar 校验该投影。回调边界是源窗口相邻的逻辑时点，不添加未给出的竞价事实。Rust RuleBook 只读导出复用原官方目录，没有第二份 Python 规则/费用计算。佣金复用现有 Rust 校验；法定费用的具体产品/投资者 scope 仍缺。

## 续修已完成的终装配

`formal_assembly.py::assemble_run_plan` 已接到默认 resolver 的依赖齐备分支，按原 MarketBinding、日历、稳定身份和窄 ExecutionSessionInput 形成 AcceptedRunPlan。原 Rust RuleBook 校验 Product/ListingWindow/状态/实际限价，原 FeeCatalog 与捕获佣金组成两侧 FeeConfig；已知 false 权限保留 false，缺权限明确拒绝。调用方不能注入 fees/origin。会话事实最多 256 项分页，资源预算取捕获的 WorkerLimits，沿原 CurrentStore/context 和最终同一事务复核小事实 token。

修正真实 DTO 边界：原 following_session 错放了第二个会话字符串；现在是与 next_session 同键的完整 CalendarSession，第二个会话仅作为 subsequent_session reference。跨月读取下一会话的整周/月自然日证明，并再次走 Rust 日历校验，不算部分月份或制造假日。

完整零行动投影已直接接原 corporate_action coverage repository。逐开放会话、逐标的必须有全部适用行动类型的完整零证明；cash_dividend 一类的零证明不能推导 split/rights_issue 等也为零。非零事件的 public_ns、phase/sequence、raw mark 与投资者税务仍缺，保持单独拒绝。没有把 after_open_match 改成另一 phase，也没有给缺失税务填零。

缺研究发布时间/锚点/完整集只关闭对应 route；各 source 的缺项分别公开。`execution_plan_assembly=fact_missing` 的 `scope.adapter=implemented` 说明终装配代码存在；missing 列出实际未产出的 market/execution inputs 等依赖，并不代表默认真实成功。默认 `_resolve` 仍不产出源解释和交易执行 inputs。

末次本地验证：受影响 PG/CurrentStore/Arrow/IPC/Rust/研究/降采样/就绪回归 **171 通过**；其中 SQL 与终装配 **28 通过**，含最新同 rank 零行动证明拒绝及 DATA_CHANGED 检查。后者的窄 DTO 是明确隔离 oracle，真实 PG/原节点/IPC/Rust 按页消费，且相同数据下默认 resolver 前后仍拒绝。它不是默认 create_app/create_services 成功证书。独立安装 wheel 的 SDK **93 通过**，Rust workspace/core、fmt、clippy、共享契约/发布版本、根 unittest **29 通过**。

读取使用有界只读 REPEATABLE READ、行/字节/时间/控制预算及事实指纹。`check_facts(..., connection=...)` 在最终事务使用现有事实表 SHARE 锁及同一 connection 重新核验。没有新批准台账、治理层、行情快照或生产写入。

诊断区分 `available`、`fact_missing`、`fact_invalid`、`field_semantics_missing`、`adapter_missing`。无请求 capabilities 的可用项明确标明有界样本及 current-knowledge scope，不宣称全部标的/日期可运行；每次预检/绑定仍按模拟日知识边界重新读取。财务或成分的历史语义缺失不会关闭已验证的身份/日历能力。

## 验证及证据边界

- 原 SR-01/02 默认缺解析器、空路由，以及 SR-03 B 缺 H1 的问题已独立复现。附件脚本不是生产通过证书，本会话未执行它。
- 新增默认 SQL 用例 **15 通过**：实际隔离 PG/CurrentStore、默认 create_app 的真实 HTTP、默认工厂、各类缺行、晚知事实、fixture_only、版本/内容变化、最终同事务拒绝、预算、实际 PG statement timeout、Rust 日历投影和公司行动 cash/quantity/phase/currency。没有成功 provider 或 AcceptedRunPlan 注入。SQL 单测的 daemon 只作不启动 worker 的构造端口，不能算 Docker readiness 证明。
- 受影响 CurrentStore/研究/正式节点/降采样/Rust IPC/SDK/就绪回归 **156 通过**，新增日历版本及 SQL timeout 回归另 **2 通过**；默认 SQL 15 项及相关正式节点 9 项最终再跑全部通过。独立已安装 wheel 的 SDK **90 通过**，Rust core、fmt、clippy 通过。
- 必需 Docker 套件中的实际默认 create_services 使用真实固定镜像/daemon preflight，再经实际 HTTP 验证同一拒绝且不入队；随后保留原明确隔离 execution oracle，真实 runner/受限 worker/SDK 调用五种研究函数、复权和参考价格，质量变更终止运行。后者仍不是默认成功验收。
- 本地宿主/卷为 **overlay**，实际 CurrentStore 测试用了既有仅测试 probe。无 probe 本地套件已按门禁拒绝 UNSUPPORTED_FILESYSTEM。受支持文件系统的结论以精确提交 CI 为准，没有移除或放宽门禁。

## 已完成 CI 与完整规模证据

`d765607a9542272363218bd7193274cab308069c` 的 5 workflows / 7 jobs 全成功；这些属于该提交，不能移作后续 head 的 CI。

| workflow | run | job |
|---|---:|---:|
| Validate | 38340962687 | 115111599929 |
| S3 Rust contracts | 38340962690 | 115111600083、115111599817 |
| S3 worker isolation | 38340962691 | 115111599888 |
| S3 run UI | 38340962675 | 115111599862 |
| S3 full integration and release | 38340962781 | 115111674541、115111674766 |

完整 B2/60m：200 标的 × 252 日 × 240 分钟 = **12,096,000** 来源事件，完成 **201,600** 小时事件。4 CPU/8 GiB 整组预算，准入/验收墙钟 **214.468534608s**，采样 cgroup 峰值 **735,621,120 B**；equity/orders/trades/positions/records = 253/90/45/5/256，独立会计 cash_conserved=true。全 B1/B3/B4 未重复。

artifact **11787148539**（run 38340962781），SHA-256 `18a36e84be40a1319dc9af96d87560f148df1f62fb67660b070aae078e720465`。以上测量来自精确 job 的解码日志及 artifact 元数据；本地下载 ZIP 受代理 403，未声称已解析该 ZIP。后续桥接 head 的 CI 状态另在 PR/交接中逐项报告。

## 接续 HEAD af47 的已核终态

`af47b038fdfd0ea1952c23334af0279c2e28ddbc` 的 **5 workflows / 7 jobs 全成功**，见 [CI_AF47.json](CI_AF47.json)。Validate 38347275730/115131481521；Rust 38347275835/115131482202、115131482325；worker 38347275775/115131481571；UI 38347275764/115131481542；release 38347275805/115131552935、115131553278。真实 worker suite **26 通过**，保留原受支持文件系统门禁，未启用本地 overlay probe；artifact 11789672742，SHA-256 `c86ee7455a5d804d962767085e770bf5e9ca19bcbf13ded18eb24d4cad6d4b9a`。

该 HEAD 的受影响 B2/60m 再测：12,096,000 来源事件、201,600 小时事件，准入/验收墙钟 **286.7879789409999s**、采样 cgroup 峰值 **682,680,320 B**，4 CPU/8 GiB 整组内独立 cash_conserved=true。artifact 11790755796，SHA-256 `5def3ffa90cb799375660c7f9b029d593d30eda040c0fe1aaacfe70374e02f26`。来自解码日志与 artifact 元数据，未本地解析 ZIP。仍是 full_B1_B4=false，未重复 B1/B3/B4，未将原 d765 的 214.469s 换标签。

以上 CI 属于 af47；续修提交的精确 HEAD CI 另在同一草稿 PR/交接核验，不能用本节替代。

## 未通过条件及下一范围

**SR-01/SR-02 默认真实成功链仍未关闭。** `SqlFormalPlanResolver.__call__` 已有终装配分支，但当前源/执行事实生产者仍未定义，真实解析后按具体依赖拒绝。终装配代码和完整零行动投影已实现；正式研究 source mapping 的缺语义及非零行动适配仍分别保留。完整默认 API→runner→受限 worker→结果正例仍未通过，不能用隔离 DTO oracle、注入完整计划或成功 provider 代替。

已确认确实未定义的事实包括 E50 价格币种/量单位、E70 provider_reported 到 raw/精确份额解释、Product 细分、ListingWindow/上市会话序号、实际上下限/fund_price_band/风险与盘中状态、费用/权限/税务 scope；E69 没可靠 public_ns/锚点，财务/估值/成分缺历史知识/集合语义。保持拒绝，不用 created_at、账户 metadata 标志或合成 MODEL 填这些空缺。

达到默认真实成功所需最小新增生产者、源单位解释、涉及字段/文件及迁移/采集/费用边界见 [FACT_SCOPE.md](FACT_SCOPE.md)。已给父两个窄选项：先单 Product/1–3 标的/raw 日线/完整零行动区间，再可选一个研究 kind。未知真实权限、raw/份额解释、ListingWindow/实际限价及非零行动语义不伪造；本次未执行这些新增生产者或生产操作。

路径交集：变更在 S3 backtest_service、Rust 只读适配、SDK及其测试/构建/CI；仅读 R01 共有身份、日历、状态、公司行动契约，未修改其模型、迁移、CurrentStore 实现或生产状态。

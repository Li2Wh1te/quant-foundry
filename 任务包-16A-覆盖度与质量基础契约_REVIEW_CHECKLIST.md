# 任务包 16A：覆盖度与质量基础契约及内部链路验收——冻结验收检查表

> 基线来源：`docs/architecture/backtesting-engine-features/任务包-16A-覆盖度与质量基础契约.md`
>
> 冻结规则：本文件在子代理委派前冻结。后续审查只能按既有检查项核验，不得增删检查项。

| 编号 | 验收域 | 可验证标准 | 验证方式 | 红线/失败判定 |
| --- | --- | --- | --- | --- |
| B01 | 基线与复用 | 复用现有 `DataCoverageReport`、`DataPreflightReport`、`DataCapabilityManifest`、身份、规则、日历及 Provider 模型 | 代码检索、类型关系审查 | 创建第二套同义报告、Provider、身份或规则模型即失败 |
| B02 | 基线与依赖 | 任务包 17～20 尚未交付的正式能力均有明确 gate，`formal@1` 不得在依赖缺失时 `ready` | 单元测试、分支审查 | 缺失依赖被默认值、空表或 fixture 冒充即失败 |
| B03 | 变更安全 | 不覆盖、回退或重排任务开始前已有的未提交修改；交付物列出实际修改文件与依赖缺口 | 前后 diff、追溯表 | 用户既有修改丢失或被无关改写即失败 |
| F01 | 覆盖事实 | `DataCoverageFact` 不可变，包含稳定 UUID、会话日期、能力、机器字段名、精确规则版本、适用性、质量、证据、深度冻结 details、稳定排序 issue codes | 构造/变异测试、序列化测试 | 字段缺失、可变嵌套值或接受 `latest` 即失败 |
| F02 | 覆盖事实 | 空字段名、无效 UUID、无会话日期及非法枚举/组合被稳定拒绝 | 参数化负向测试 | 非法事实可创建即失败 |
| F03 | 覆盖事实 | `not_applicable` 必须带明确规则来源，不得由缺少事实推断 | 负向测试 | 无规则来源仍可标记不适用即失败 |
| F04 | 覆盖事实 | `complete` 必须带可审计 evidence；`unavailable` 明确表示无法证明覆盖 | 负向测试、字段断言 | 无证据 complete 或把 unavailable 当已覆盖即失败 |
| F05 | 覆盖事实 | `invalid` 保留必要原始值与失败规则，details 禁止凭证及 ORM 对象且 JSON 安全 | 负向测试、序列化测试 | 泄露凭证/ORM 或丢失必要失败上下文即失败 |
| F06 | 覆盖事实 | 唯一逻辑键仅由 instrument/date/capability/field/rule key-version 构成 | 相等性/去重测试 | DB ID、生成时间或对象身份参与比较即失败 |
| C01 | 覆盖聚合 | 聚合为纯函数且无 I/O，按规范化期望事实键统计 complete/partial/invalid/unavailable | 单元测试、依赖审查 | 聚合器访问网络/ORM 或按数据库行计数即失败 |
| C02 | 覆盖聚合 | 缺失期望键记 unavailable；字段部分存在记 partial；原值违规记 invalid；合法且证据完整才 complete | 参数化测试 | 缺失/非法值被填 0、前值填充或修复即失败 |
| C03 | 覆盖聚合 | 同键同内容确定性去重，同键冲突以 Provider contract violation 阻断，范围外事实被检测 | A2 与范围外测试 | 按输入顺序选冲突事实即失败 |
| C04 | 覆盖聚合 | `missing_ranges` 仅按已解析交易会话合并，不按连续自然日推测 | 非连续会话测试 | 周末/休市日参与猜测范围即失败 |
| C05 | 覆盖聚合 | 数组、issues、序列化和 hash 稳定排序；输入顺序变化不改变报告与 hash | A1 测试 | 顺序扰动改变报告/hash 即失败 |
| C06 | 覆盖聚合 | raw 请求不依赖复权 policy；仅明确 qfq/hfq 请求加入相应能力 | A17 测试 | raw 因复权未激活失败即失败 |
| Q01 | 资格端口 | 公共协议定义单标的资格请求/结果，输入绑定 effective date、窗口与三类 envelope、query boundary、profile、required capabilities、calendar IDs | 类型/构造测试 | 缺少任一边界字段即失败 |
| Q02 | 资格端口 | 输出含 instrument、eligible、coverage reports、稳定 reason codes、evidence summary、qualification hash | 类型/序列化测试 | 输出不足以审计资格即失败 |
| Q03 | 资格端口 | 只接受稳定 instrument_id；不访问策略、不生成候选、不改变 scope | 代码审查、mock 测试 | 实现候选生成或策略逻辑即失败 |
| Q04 | 资格端口 | 候选级不合格作为资格结果返回；Provider 能力缺失、越界、证据冲突为请求级稳定错误 | A13/A14 测试 | 两类传播语义混淆即失败 |
| Q05 | 资格端口 | qualification hash 排除 run ID、中文文案、生成时间，并对规范化内容稳定 | A18 测试 | 非业务元数据改变 hash 即失败 |
| Q06 | 资格端口 | 协议不依赖具体 `DataPreflightService`，任务包 15 与 16A 不互相导入具体服务 | 导入图/代码审查 | 循环依赖或具体服务耦合即失败 |
| P01 | 内部 profile | 只精确注册 `internal_link_acceptance@1`，run kind 服务端固定为 `internal_link_acceptance`，客户端不能任意指定 | 构造/API 负向测试 | 接受其他内部 profile/run kind 即失败 |
| P02 | 内部 profile | 内部状态只能 ready/blocked；任何 degraded 导致 profile contract violation/blocked | A12 测试 | 内部报告返回 degraded 即失败 |
| P03 | 内部 profile | 仅允许数量类公司行动覆盖、交易状态、来源修订审计、transitional repeatable-read 四类具名替代事实 | 注册表/参数化测试 | 接受第五类或未命名替代能力即失败 |
| P04 | 内部 profile | fixture 精确包含 key/version/capability/scope 或 instruments/date range/proof/source/fixture_only/content_hash | 构造与范围测试 | 字段缺失或版本模糊即失败 |
| P05 | 内部 profile | fixture 的标的和日期范围必须完整覆盖请求，且进入报告与 hash | A8/A19 测试 | 越界 fixture 继续执行或不影响 hash 即失败 |
| P06 | 内部 profile | 空公司行动表、未命名字典、测试常量及 adapter 默认值不得成为覆盖证明 | A9/A10 测试 | 任一默认/空值被解释为证明即失败 |
| P07 | 内部 profile | formal profile 拒绝全部 `fixture_only=true` 事实 | A11 测试 | formal 接受 fixture 即失败 |
| P08 | 硬门禁 | 身份/PIT 映射、strict-compatible 日历、原始 Bar/OHLC、规则、账户费用、结算类别、策略目标权限不可被 fixture 绕过 | 参数化门禁测试 | 任一硬门禁被 fixture 放行即失败 |
| P09 | 硬约束 | lookback 最大 512，超限在数据读取前失败且不裁剪 | 边界与 mock 调用测试 | 读取后失败或静默裁剪即失败 |
| P10 | 硬约束 | 日历只消费 `strict_compatible@1`；不默认 SSE、不做并集、不拆分回测 | A6 测试、代码审查 | 出现默认 SSE/并集/拆分逻辑即失败 |
| P11 | 硬约束 | 预检和运行读取不访问 Tushare 或其他外部网络 | A21 网络阻断测试 | 发生任何外网调用即失败 |
| S01 | 预检服务 | `DataPreflightService` 编排请求/profile、固定集合、任务包 15 范围、日历快照、覆盖资格、报告/hash，无 ORM、FastAPI、网络、策略依赖 | 依赖审查、mock 测试 | 重新实现领域算法或依赖禁用层即失败 |
| S02 | 页面准入 | 页面报告 blocked 时不创建内部运行；ready 才允许创建明确标记的内部运行 | A3/A7/A15 测试 | blocked 后创建运行即失败 |
| S03 | 会话权威预检 | `DataSession` 打开后且策略加载前，基于冻结请求重新预检并绑定页面/会话 hash | A16/A22 测试 | 策略先于权威预检调用即失败 |
| S04 | 会话失败 | 会话事实变化、请求绑定变化或 blocked 时以 `failure_phase=data_preflight` 失败并保存两份报告，不产生决策/订单/成交 | A16/A22 测试 | 未保存报告或产生交易副作用即失败 |
| S05 | 固定集合 | `static ∪ mandatory ∪ non_zero_initial_positions` 全部逐标的硬预检 | 集合并集测试 | 任一类别被漏检即失败 |
| S06 | 初始持仓 | 非零初始持仓不可被动态过滤跳过；正式起点估值缺失/非法、账户费用错误或不支持结算类别均在创建前阻断 | A7 测试 | 初始持仓门禁绕过即失败 |
| S07 | 初始持仓 | 复用现有初始持仓预检服务，不建立第二套账户规则 | 依赖审查 | 重复实现账户/费用语义即失败 |
| S08 | 动态/混合 | 动态单候选不合格由任务包 15 过滤且不阻断范围；Provider 无范围/资格能力请求级 blocked | A13/A14 测试 | 传播语义错误即失败 |
| S09 | 动态/混合 | hybrid 同时执行固定对象硬门禁与动态范围能力预检 | A15 测试 | 只检查一侧即失败 |
| S10 | 动态/混合 | 任务包 15 唯一负责动态候选生成、排名、过滤编排和选中后最终复检；16A 只消费解析与摘要 | 代码审查、联合测试 | 16A 实现上述动态语义即失败 |
| R01 | 报告契约 | `DataPreflightReport` 包含文档 9.1 所列 run/profile/manifest/status/scope/coverage/mapping/rules/lookback/bar/adjustment/universe/missing/invalid/PIT/source/issues/hash 字段 | 字段与序列化测试 | 关键审计字段缺失即失败 |
| R02 | hash 契约 | hash 覆盖 run/profile/manifest、窗口与 envelope、日历、固定集合、映射/规则/Bar、adapter、调整、动态 policy、fixture、PIT/source/issues | 定向变更测试 | 任一规定业务输入变化不改变 hash 即失败 |
| R03 | hash 契约 | hash 排除 generated_at、run_id、中文文案、DB 主键、耗时、原始凭证、运行时 token 上下文/获取时间 | A18 与定向测试 | 任一排除项改变 hash 或凭证入 hash 即失败 |
| R04 | 持久化 | 复用 `backtest_data_preflight` 和现有 admission/session/JSON/结果记录，保存两份报告及关联 hash、coverage、fixture、scope | 仓储测试、schema 审查 | 新建覆盖事实表或第二套运行真相源即失败 |
| R05 | 持久化 | 不保存原始 token 或凭证；现有结果 API 维持有界分页和 JSON 限制 | 仓储/API 测试 | 敏感原文落库或取消边界即失败 |
| I01 | 可见性隔离 | 正式列表默认且强制排除 `internal_link_acceptance` | A20 测试 | 内部运行出现在正式列表即失败 |
| I02 | 可见性隔离 | 比较 API、收益曲线和指标展示拒绝内部 run ID | A20 参数化测试 | 任一正式展示接受内部 ID 即失败 |
| I03 | 可见性隔离 | 内部详情明确显示“内部链路验收”、run kind/profile 与 fixture 来源，不描述为正式回测/收益证明 | schema/UI 文本测试 | 内部制品被误标为正式结果即失败 |
| E01 | 错误契约 | 稳定表达文档第 12 节全部 9 个错误码，机器流程按 code 分支 | 枚举/参数化测试 | 错误码缺失或依赖文案分支即失败 |
| E02 | 错误契约 | 错误 details JSON 安全，并按场景包含 instrument/date/capability/field/rule/expected/actual/fixture/scope | 序列化与字段测试 | 不可序列化或缺必要上下文即失败 |
| E03 | 操作日志 | 每个操作员可见事件含简洁中文 `message`，说明动作、范围和结果；技术字段保留结构化 | 日志捕获测试 | 仅英文/事件 key/JSON 占位作为主要文本即失败 |
| A01 | 验收矩阵 | 相同覆盖事实不同输入顺序，报告和 hash 相同 | 自动化测试 | 不一致即失败 |
| A02 | 验收矩阵 | 同一事实键内容冲突，Provider contract violation / blocked | 自动化测试 | 未阻断即失败 |
| A03 | 验收矩阵 | 期望 Bar 缺失，unavailable 且内部预检 blocked | 自动化测试 | 状态/计数错误或放行即失败 |
| A04 | 验收矩阵 | 任务包 12 判定非法 OHLC，invalid、保留原值/规则且 blocked | 自动化测试 | 修复原值、丢上下文或放行即失败 |
| A05 | 验收矩阵 | 规则事实缺失或冲突时 blocked | 自动化测试 | 放行即失败 |
| A06 | 验收矩阵 | 日历不兼容时 blocked，不降级、不默认 SSE | 自动化测试 | 任一条件不满足即失败 |
| A07 | 验收矩阵 | 非零初始持仓缺估值事实，在创建内部运行前 blocked | 自动化测试 | 创建后才失败即失败 |
| A08 | 验收矩阵 | internal profile 使用范围完整的具名数量类 fixture 可继续，报告记录 fixture | 自动化测试 | 合法 fixture 被拒或未记录即失败 |
| A09 | 验收矩阵 | 内部公司行动表为空且无覆盖 fixture 时 blocked | 自动化测试 | 放行即失败 |
| A10 | 验收矩阵 | internal profile 使用未命名状态字典时 blocked | 自动化测试 | 放行即失败 |
| A11 | 验收矩阵 | formal profile 读取 fixture_only 事实时 blocked | 自动化测试 | 放行即失败 |
| A12 | 验收矩阵 | internal report 产生 degraded 时 contract violation / blocked | 自动化测试 | degraded 对外返回即失败 |
| A13 | 验收矩阵 | dynamic 单候选行情不完整时由任务包 15 过滤，范围能力仍可用 | 自动化测试 | 请求级错误或候选保留即失败 |
| A14 | 验收矩阵 | dynamic Provider 无资格能力时请求级 blocked | 自动化测试 | 仅过滤候选或放行即失败 |
| A15 | 验收矩阵 | hybrid 固定对象失败时整体 blocked | 自动化测试 | 放行即失败 |
| A16 | 验收矩阵 | 页面 ready、会话事实变 blocked 时策略加载前失败并保存两份报告 | 自动化测试 | 时序/持久化任一不满足即失败 |
| A17 | 验收矩阵 | 请求 qfq/hfq 但未激活时 blocked；raw 不受影响 | 自动化测试 | 任一分支错误即失败 |
| A18 | 验收矩阵 | 生成时间变化不改变 qualification/report hash | 自动化测试 | hash 变化即失败 |
| A19 | 验收矩阵 | fixture key/version 变化时 hash 变化 | 自动化测试 | hash 不变即失败 |
| A20 | 验收矩阵 | 内部运行不出现在正式列表、比较 API、收益/指标展示 | 自动化测试 | 任一泄漏即失败 |
| A21 | 验收矩阵 | 预检读取不触发外部网络 | 自动化测试 | 触发即失败 |
| A22 | 验收矩阵 | blocked 后不调用策略、不产生订单或成交 | 自动化测试 | 任一副作用即失败 |
| X01 | 范围红线 | 不实现 token 签发、失效、覆盖向量或块前校验算法 | diff 关键词与代码审查 | 出现生产实现即失败 |
| X02 | 范围红线 | 不实现公司行动采集、cash effective date 派生、账户入账或数量类生产数据源 | diff 关键词与代码审查 | 出现生产实现即失败 |
| X03 | 范围红线 | 不实现交易状态生产、修订链/向量/历史回放 | diff 关键词与代码审查 | 出现生产实现即失败 |
| X04 | 范围红线 | 不实现新日历、Bar、规则包、复权算法、账户语义、分钟线/Tick/其他资产 | diff 与依赖审查 | 出现新业务算法即失败 |
| X05 | 范围红线 | 不实现正式回测创建入口、队列、Runner Supervisor、策略工作台或第二套运行模型 | diff 关键词与路由审查 | 出现正式运行扩展即失败 |
| X06 | 数据库红线 | 不新增 `data_coverage_facts` 或公司行动/状态/修订/token 表；仅在权威记录无法查询时允许最小 run kind/profile 字段 | migration/schema 审查 | 新增禁止表或无证据扩表即失败 |
| X07 | 测试与质量 | 覆盖范围内自动化测试全部通过，无语法/导入错误，并执行文档第 15 节范围检查 | 测试命令、compile/import、diff 检查 | 任一相关验证失败即失败 |

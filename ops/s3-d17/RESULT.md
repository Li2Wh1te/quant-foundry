# S3-D17 v1.1

基线：实际 `main cb45dfc159d4ff2d239c175d760e30250e839079`（D16 已合并），无仓库 AGENTS.md/skills。

## 实际变更

- 删除旧 backtesting 与 strategy_protocol 的 103 个源文件及 104 个仅服务旧执行协议/核心的测试或夹具。
  Git 保留历史，发行树不留 zip、副本或换名旧内核。
- 复用 D13 唯一账户/历史层；迁出实际被采集和身份模块消费的共享日历、错误、查询边界与最小显示记录。
  日历迁出仅保留事实类型/选择规则和原有直接 SQL 查询；旧轴注册、预检、快照规划/缓存及配套执行测试删除。
  命名日历 seed 自包含，历史 revision/DDL 不改；保留 Arrow/DuckDB/SQLAlchemy 共享依赖。
- 清除旧独占资源配置和模板项，保留鉴权 tombstone 所需内部 token。
  新 runner、策略校验/模板/API 目录、总览及迁移发现均无旧目录依赖。
- 交付只读 AST/文本/入口扫描和默认 dry-run 的精确 cleanup。工具拒绝未知依赖，apply 仅在自有隔离夹具验证。
  私有 CI 镜像源码在 scratch 阶段组装，避免 vfs 对大依赖层反复复制；生产镜像及资源/安全政策不改。

## 验证及证据

当前环境实测：Rust fmt/clippy/workspace/public contract、锁定 release wheel、76 项安装 SDK 测试通过；
D11 千万事件模块结果流与真实文件写失败检查通过（不是 D18/B3）。根测试 25 项及前端 152 项、构建通过。
最终全后端复核 1413 通过、2 跳过、96 subtests 通过；另 2 项 CurrentStore 原生 overlay 探测失败，未放宽检查。
实际 runner 套件本地 D12 测试通过，但 D13/D14 原生文件系统前提未满足；精确提交 CI 必须完整通过，不能用开发夹具代替。
最后 cleanup/startup 专项 25 项通过；共享身份、账户聚合及删除保护恢复后 35 项与 18 subtests 通过。
真实页面结果和精确提交 CI 见本模块 PR 与保留的自动输出。
真实 D15/D16 页面（含 16 组参数）及运输替身分别通过；本地 CurrentStore 检查使用仓库既有开发 overlay 夹具，
不宣称原生文件系统验收。私有权限切换等待在途旧凭据请求结束，原有 owner 隐藏/401/403 断言均保留。

旧样本在 `20261011_01` 建账户/费用版本、策略草稿/修订、成功/失败旧运行、精确金额/时间/错误及保存比较，
阻断两个旧 namespace 后升级到 head，逐列原值与关系对照；历史 API 继续读取原指标，不回算。
空库从零迁移及 app/models/runner/采集/策略启动也阻断整个 namespace。
新 run/SDK/CLI/HTTP 的原有回归与真实容器套件使用同一阻断钩子。

cleanup 自动输出包含完整保护行、对象身份、dry-run 文件不变和 apply 仅删除指定临时目录/空表；
错库/根、链接、未知对象、业务引用、活动/未知运行、路径/表替换、真实进程、并行写者和停止容器挂载均拒绝。
CI `s3-retirement-evidence` 保留完整历史原值和隔离对象输出；源码删除由本 PR diff 直接核对。

环境曾因本轮两次 Docker vfs 测试构建缓存耗尽磁盘；源码/证据未丢。只按创建时间与精确 ID 回收本次缓存，
现有配额内恢复；未清无关对象、未全局 prune、未扩大资源。中断和未完成检查不算通过。

## 状态与未决项

- `new_engine_code_ready`：D17 模块实现就绪，最终以精确提交 CI 为准。
- `legacy_execution_removed`：旧执行源目录物理缺席；共享业务及旧历史仍保留。
- `production_switched=false`、`runtime_cleanup_done=false`：仅隔离开发验证，未执行 P01/C01。

正式 D04/D05 数据/研究映射、D18 全量 B1–B4 与生产 runner 拓扑仍未关闭；不以模块、合成或原生测试代替生产通过。
未合入 S2 私有兼容候选或 R01 E40 修复，未开始 D18。cleanup 对超出已知引用/分类的实际残留保留并拒绝，
容器/镜像/卷不由本工具删除。可操作参数与安全边界见同目录 README。

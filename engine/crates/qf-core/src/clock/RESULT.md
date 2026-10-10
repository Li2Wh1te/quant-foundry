# S3-D03 RESULT

从最新 main `50aa333` 接续。实现 `clock/{calendar,merge,schedule}`、模拟时钟和
`engine.rs`；仅在 `lib.rs` 增加模块导出。D01 共享类型/错误/结果协议及 D02
规则/费用/SOURCES 保持原样，未修改 R01/S2 公共代码或上传原任务包。

完成分块有界合并、完整事件身份排序、日/周/月注册调度、会话/午休分桶、
同刻全部 Bar 的撮合/账户/行情屏障、单次聚合回调、逐 Tick 纳秒游标、
订单生效与盘后 DAY、非递归有界通知、取消/异常/进度和信用分块结果结束路径。
收盘同纳秒尚有后续 Tick 时保留当前会话资格，最后市场事件完成后归下一会话。

定向复核补齐了首事件前历史读取、同回调预留后账户可见、按标的有界订单访问、
撮合输出数量/身份检查、资源错误被策略捕获后的终止、编码字节计数及最终一致性
检查。接口和可运行隔离接入例见 [README.md](README.md)；替身只在
`tests/time/support.rs`，明确标为 `synthetic_clock_only`。

## 已执行

- Rust 1.90.0：`cargo test -p qf-core --locked --test time`，31 项通过。
  包含分块/来源排列的穷举小属性、50,000 条惰性输入/62 条缓冲上限、跨标的
  Bar、同纳秒 Tick（含收盘）、日周月/午休、DAY、通知、取消/异常/未来读取。
- `QF_S3_PYTHON=<CPython 3.12.2> bash scripts/test_s3_engine.sh` 完整通过：
  fmt、workspace clippy `-D warnings`、Rust 72 项、公共契约例、生成签名/版本
  检查、根目录 18 项、release wheel 构建、独立安装/导入及 SDK/D02 wheel 17 项。
  本地先修正 Python 安装路径后在独立环境重跑；仓库脚本没有修改。
- `PYO3_PYTHON=/nonexistent-python-core-must-not-use cargo test -p qf-core --locked`
  独立核心通过；`cargo tree -p qf-core --locked` 不含 Python/数据库客户端。
- 测试环境的 `uv run --locked python -m pytest -q` 对现有账户、费用配置、策略
  存储、历史结果/API 与分页的六个保护文件执行，78 项通过。
- `git diff --check` 通过。正式远端 CI 由草稿 PR 关联精确 head，最终回传附链接。

## 接缝、未跑项与保留

D04/D05 提供真实数据源与受限帧；D06/D09 实现 `ExecutionPort: AccountPort` 的
订单/唯一账户、结算/公司行动/估值；D07/D08 经 D01 Matcher 消费 D02 规则；
D10 实现 EngineStrategy/Python 生命周期及过期视图；D12 控制取消/硬超时和可打断
I/O；D11/D13 提供分析、结果 writer 与原子终态提交。当前没有完整真实引擎链路，
SDK 仍不开放完整运行能力。

未运行真实生产数据、真实 Python 策略事件链、真实 runner 容器隔离或 B1–B4
完整端到端基准。50,000 条惰性合并用例只证明局部缓冲界限，不作为 B3 或 RSS
验收。生产部署/清理、供应商及凭据/安全设置均未执行。

旧 Python 时间轴仍有旧生产调用方，D03 未接管控制面/共享业务，故本次无删除；
真实调用退出和物理退役继续由 D12/D13/D17/D18 集成完成。D02 未知历史规则/
费用和分红税资料缺口保持原 SOURCES，不在时钟模块补猜测。

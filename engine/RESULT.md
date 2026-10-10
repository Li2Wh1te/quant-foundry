# S3-D01 结果

从最新 main `4c9e8167d49ef1d6bbc49afc6578208d7a339f97` 接续，分支
`codex/s3-d01-rust-contracts`。未合入 S2 PR158；本次只新增 D01 代码、实现契约、
样例、锁文件与 CI，另扩展既有单版本脚本以同步 Rust/Cargo.lock（SDK 动态继承版本）。

已实现两个 crate、Python 3.12.2 可安装混合 wheel、受检 Decimal/Price/Quantity、
纳秒/事件身份、RunConfig 的 Rust/Python 共用验证与归一化、严格佣金覆盖和 FeeConfig（允许同类费用非重叠日期变更，拒绝重叠事实）。
Bar/TradeTick/QuoteTick（双边数量）、OrderIntent/Fill/AccountView、DataRequest/Batch、
TradePage/RunOutcome/有界结果批次及各模块端口可由公开 Rust 契约消费。
Python 公开签名、JSON wire 提示和安装包通过生成脚本同步，JSON Schema 与 Rust 字段对应。

数值使用 rust_decimal 的 96-bit/scale≤28 存储及有界 512-bit 中间整数，明确检查精确运算、
half-even 舍入、数量商余数和最终范围。1/3 均价允许 scale12 舍入，最后卖出释放全部余成本；
不是用库默认除法/round 冒充业务政策。金额、价格、纳秒、事件序号使用 JSON 字符串。
参数中的大整数也保留原值；NaN/Infinity、隐式精度丢失、非法数量及任意费用字段被拒绝。

锁定：Rust 1.90.0、PyO3 0.29.2、rust_decimal 1.43.0、bnum 0.14.4、
serde 1.0.229、serde_json 1.0.151、maturin 1.15.0；构建/契约测试工具全依赖含 SHA256 锁。
技术 API 依据 [PyO3 官方指南](https://pyo3.rs/v0.29.2/)、
[rust_decimal 官方 API](https://docs.rs/rust_decimal/1.43.0/rust_decimal/struct.Decimal.html)
及 [maturin 官方指南](https://www.maturin.rs/)；兼容性以本次真实构建为准。

实际验证（云端 Linux x86_64，Python 3.12.2）：

- `QF_S3_PYTHON=backend/.venv/bin/python bash scripts/test_s3_engine.sh`：通过。
  入口实际执行 cargo fmt/clippy/test、公开契约样例、生成契约检查、版本检查及 wheel 构建/安装。
  clippy 使用 `--workspace --all-targets --locked -- -D warnings`，无警告。
- Rust：15 项真实回归通过（9 契约、6 数值），另公开 example 编译并运行。
  qf-python 的 Rust 测试 target 无业务测试；绑定由安装 wheel 的 Python 测试验证。
- 独立消费 venv 的 `python -I -m unittest discover -s sdk/python/tests -v`：7 项通过，
  含72个有效/无效配置用例、纳秒/Decimal 往返及另一独立进程从 site-packages 导入。
  SDK 可选 None 按签名表示省略/默认；JSON 服务边界的显式 null 按 schema 拒绝。
- `python -m unittest discover -s tests -v`：18 项通过，包含已有共享根测试和 Rust 单版本同步。
- 既有 backend 保护回归：账户 workspace、账户 profiles/storage、策略 storage、历史结果 API、
  分页六个模块，`.venv/bin/python -m pytest -q ...`：78 passed（隔离 test 环境）。
- PATH 中无 python/python3/psql、`PYO3_PYTHON=/no-python-in-core`，独立 target 下
  `cargo test/run --offline --locked -p qf-core`：通过；依赖树无 PyO3 或数据库客户端。

本地实际 wheel：`quantfoundry_sdk-0.3.0-cp312-cp312-manylinux_2_34_x86_64.whl`，
504961 字节，SHA256 `6d6d5bde0abbaa285bfd3701e875b37c1d69d60a9007ffb24afc626f4c420cd0`。
自动构建输出在云端 `/workspace/artifacts/s3-d01/verification.log`，
core 独立验证输出在同目录 `core-without-python.log`；wheel 位于 `verify/wheels/`。

边界：D01 完成共享基础。事件循环、市场规则、撮合、账户引擎、Arrow 真实编码/网关、
策略回调、运行调度/仓储/API/客户端传输和分析尚未实现；capabilities 的模型/频率列表为空，
生产数据未检查，未实现入口明确 CAPABILITY_UNAVAILABLE。未运行全仓后端/前端、D18基准、
生产隔离/市场数据验收或远端 GitHub Actions；新增 CI 入口已经本地真实执行。

无旧实现删除：D01 没有替换旧算法或生产调用方，现有共享账户、策略、历史结果、CurrentStore
及后端锁文件均保留原样。未部署、切换生产、R01 reset、C01 清理、供应商调用或凭据修改。
未上传原包/全部参考资料，未执行 GitHub 推送/PR/合并；本地提交与必要差异交回父会话。

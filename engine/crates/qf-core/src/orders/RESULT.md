# S3-D06 RESULT

基于最新 main `60d1c9639b5d2f90547da8edd219d410641cb5ba`，独立分支
`codex/s3-d06-orders-targets`。本次终点为 D06 README，实际审查、CI 与授权合并
状态以 [PR #165](https://github.com/Li2Wh1te/quant-foundry/pull/165) 为准。

## 已交付

- `OrderManager`：市价/限价、DAY/GTC、受检状态过渡、数量/价值/比例目标、撤单
  和有界活动/终态查询。目标包括真实持仓和普通/目标余单；同目标不叠单，普通
  单不被取消，反向或增量目标准入失败保留旧单及资金/份额预留。
- 唯一 D09 `Account` 真实集成：账户修订绑定 preview/commit、累计费用、可卖
  检查与候选价预算。初始资金不足明确拒绝并报告可达数量；未成交卖款不作现金，
  跳空后只提交可支付份额并保留余单原因。不使用 float 算交易份额。
- D03 真实接缝：完整命令/市场键、稳定委托序、单次修订绑定成交 claim、整批
  订单/账户原子提交、旧 target 取消通知、公司行动 effects 输出。真实转发
  `check_session_end`，登记/收盘支付在末笔市场成交后、关闭回调前完成；盘后
  DAY 归次会话，公司行动取消 GTC。终态缓存淘汰后仍可幂等撤单，无永久台账。
- D07/D08 窄 `FillBudget`：候选价可支付量、有序成交 ID、累计前序候选成交的
  账户预览；不让多单重复使用同一可用现金。没有实现第二套撮合。订单变更键与
  公司行动 DTO 使用已有 wire 生成脚本同步，SDK 没有另外计算持仓/费用/目标。

接入及行为详见 [README.md](README.md)。正常未成交 Tick 不生成状态/成功记录；
活动数量 O(1)、按标的索引，账户无变化的事件不复制账户候选。

## 实际验证

Rust 1.90.0 / Python 3.12.2，隔离输入明确
`synthetic_business_oracles / production_executed=false`。预期来自手算，不用旧
Python 引擎输出。以下不是生产验收或全量 B1–B4 性能结论。

- `cargo test -p qf-core --locked --test orders`：29 passed，0 ignored。其中
  8 项使用真实 D03 driver + D06 + D09 + D02 规则/费用；host/data/显式成交计划/
  writer 为隔离适配，不能称 D07 matcher 或正式 CurrentStore 联调。
- `cargo clippy --workspace --all-targets --locked -- -D warnings`：通过。
- `QF_S3_PYTHON=backend/.venv/bin/python bash scripts/test_s3_engine.sh`：通过。
  fmt/clippy、workspace Rust 168 项、公共核心契约、wire/版本一致性、仓库 Python
  18 项、release wheel 构建及独立安装后的 SDK 29 项均通过。最终云端 Actions
  只以草稿 PR 的精确 head 为准，回传提供 commit 与运行链接。
- `git diff --check` 通过；D05 `data/views.rs` / `analysis/indicators.rs` 无改动。

独立反例：1001 元、100 份×10+1 费预留，改目标 200 明确拒绝且可达 100、旧单
不变；2001 元挂 200 份遇 12 元跳空，仅买 166、费用 1、现金 8，随后 34 份×0.2
只收累计增量费 0，现金 1.2；350 元两张 10 份挂单遇 20 元，仅成交 10+7，费用
2、现金 8。另验证零股 105 清仓、T+ 反向拒绝、普通单保留、全部/部分/缓存外
终态撤单、当日买后卖仍计买入限制、Bar 同刻屏障、同 ns 合法后续 Tick、过期/
跨运行/未知 claim 和后项失败无半批状态。末笔之前的收盘 Tick 保持实际竞价
阶段，末笔后的 DAY 才归下一会话。

合并前复核修正两处已复现缺口：金额/比例 target 的整手订单部分成交后，不将
合法余单重新量化取消；历史查询前先确认本运行已受理 ID，不让另一运行同序号
终态伪确认未知 ID。新增手算案例：10050 元，普通 100 份成交 30、target 200 份
成交 40 后，持仓 70＋普通余量 70＋target 余量 160＝300，现金 9348、权益 10048。
金额/比例目标各重复 100 次仍保留同单/预留；显式取消普通余量后才合法调整至
270。两项新回归修复前实际失败、修复后通过；另补旧 DTO 缺 updated_at 兼容、
完整事件键/公司行动 wire 往返，以及 driver 中零号/未来号/非规范 ID 拒绝。

## 保留边界

D07/D08 须消费真实预算接缝实现流动性/价格/滑点算法；D10 执行绑定和 D11/D13
结果消费、终态详情分页未由本包完成。D02 未确认历史官方规则/费用仍拒绝；
D04/D05 正式行情、历史公开时间/成员/因子、公司行动及资金可用事实映射缺项
继续开放。没有生产联调、部署、采集器/公共 UI/CurrentStore 或凭据/安全变更，
没有上传原 ZIP。

旧 `backend/app/backtesting/execution.py` 仍被 `runtime.py`、`session_matching.py`、
`bar_matching.py`、`registry.py`、`__init__.py` 等实际消费；旧订单相关结果模型也
用于历史读取。没有提前删除活依赖或迁出第二套旧计算。本模块不 import/fallback
旧算法；控制面/完整执行切换及物理退役继续交 D12/D13/D17/D18。
`new_engine_code_ready` 仅指 D06，`legacy_execution_removed`、`production_switched`、
`runtime_cleanup_done` 均未完成。授权合并仍以正式审查和精确 head CI 通过为前提。

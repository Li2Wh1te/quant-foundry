# S3-D08 结果

实现 `trade_tick_v1` / `quote_tick_v1`，从 main `54f7466` 接续。TickMatcher 复用
D02 已核定规则、D03 总序/DAY/非递归通知、D06 唯一 OrderManager 的完整累计
FillBudget 和 D09 唯一 Account/公司行动/结算；D10 worker 已替换 TickPending。
SDK 能力列出两种模型和 tick，但 production_data_available 仍为 false。

成交打印共享单事件增量量；报价 bid/ask 独立共享余额，同价同量新序号不补额、
增加只新增 floor(delta*参与率)、减少裁剪，对侧和研究打印不刷新本侧。缺量是
unknown，不能借旧/未来盘口。同 ns 后续完整键可成交，新单不能回填触发事件。
两类 Tick 可混合回放，但 run 只选一个撮合依据；D03 额外拒绝错误 matcher
在另一类事件上产生任何撮合结果。候选错误不改变余额/cursor/真实账户。

实际验证：

- `cargo test -p qf-core --locked --test tick_matching` 通过真实 D02/D06/D09 的
  手算例：连续相同 100 份 ask 的两个 60 份订单成交 60+40；双边独立耗用、
  新增/裁剪、未知/零量、重复打印、同 ns 生效、反序/重复键、价格/停牌限制、
  跳空累计现金、GTC 下一会话和最低佣金、非法/重复成交 ID 与后序预算失败。
- `QF_S3_PYTHON=...python3.12 bash scripts/test_s3_engine.sh` 完整通过：锁定 Rust
  1.90.0 / Python 3.12.2，fmt、Clippy、workspace、公共契约、生成提示、release/
  根测试、release wheel 构建，独立 pip consumer 的 `python -I` 全部 SDK 回归。
- `sdk/python/tests/test_tick_matching.py` 通过安装后的真实 Rust → Python →
  查询/下单/撤单/非递归订单/成交通知 → 唯一账户链路。Arrow chunk 1/5 得到
  相同结果；每个 ns 精确保留末尾 123，查询按 sequence 裁剪，Decimal 未转 float。
  报价独立手算为买 60@10.01、买 40@10.01、卖 30@9.90、买 30@10.01，每单
  fixture 最低佣金 1，最终 cash 8991.70、quantity 100。已发布研究打印只更新
  raw 估值，不增加报价流动性。
- 2048 Tick 小夹具实际执行每个 Python 回调，Arrow/merge 上限 17 事件，结果
  仅模型元信息和最终用户 record（无交易），结果字节低于 100000。这是有界
  API/小夹具回归，未测完整进程组/cgroup，也不是 D18 千万负载或性能成绩。
- D03 旧混合流拒绝例改为保留研究事件并拒绝错误撮合输出；D10 长 GIL 夹具
  的合成打印价改为在其声明价格带内循环，重复值仍保留各完整序号。规则没有
  放宽。旧的安装 wheel 能力与 TickPending 拒绝测试同步更新。

模型假设详见 [TICK.md](TICK.md)。quantity 只接受明确增量份额；累计源转换
仍需 D04 正式映射。新报价价格/会话建額、未知间隙保留耗用、新增量逐次向下
取整是保守研究模型，非真实盘口队列/补单/撤单延迟。Quote 不提供最后打印价；
市价预留使用已知 raw 价（夹具显式给 prior synthetic print），无该价需要显式
limit，账户估值仍保留 unknown，不造中间价或税后/真实成交保证。

没有旧 Tick 撮合实现可单独删除；旧 Bar/runtime/execution 仍有活依赖，留待
D12/D13/D17 退出。新路径未调用或搬运旧 Python 算法，无第二账户、撮合器、
行情副本、逐行情成功台账；未改 R01/S2 共享生产状态。

未验收：D04/D05 正式 Tick/研究映射与真实生产可用性、D12 容器/进程总资源和
权限隔离、D13 持久化结果、D11 分析及 D18 完整 B1–B4（含两种千万 Tick 模式）。
未部署、清理、供应商访问或生产启用。D08 完成后停工，不执行 D11。

# T18-00 fund_div 来源画像

- 来源：Tushare Pro `fund_div`，仅用于 ETF/场内基金现金分红。
- 字段映射：`ts_code`、`ann_date`、`record_date`、`ex_date`、`earpay_date`（优先）/`pay_date`（回退）、`div_cash`/`cash_div`、`status`。
- 原始响应完整保存并计算 SHA-256；规范化前不修复非法日期、金额或状态。
- 逻辑键由 source code 与公告、登记、除权、支付日期及金额组成；修订通过版本链保存。
- 非目标基金记录 `skipped_non_target`，目标代码缺失或映射歧义失败关闭。
- 状态与现金日期规则版本化为 `tushare_fund_div_status@1`、`tushare_fund_div_cash_date@1`；现金于支付日对应交易会话 `after_open_match` 入账。
- 数量类来源未审批，formal gate 继续 blocked。

## 官方语义与证据边界

- `fund_div` 是按基金代码（`ts_code`）返回的分红公告事实；全量覆盖必须逐一请求目标代码并记录请求范围、返回行数及 provider 是否截断。增量公告日查询仅表示“该公告日返回的行集合”，空响应不得推导目标基金 complete-zero。
- `earpay_date` 表示预计到账日，`pay_date` 表示实际支付日候选；两者同时存在时固定选择 `earpay_date`，并在证据中保留双字段原值。仅有 `account_date` 不属于批准语义，必须阻断。
- 现金生效会话由目标标的具名交易日历映射：日期为开市日则同日，否则取该日之后首个开市会话。`calendar_id`、IANA `timezone`、日历定义/修订引用均为必需证据，缺一不可，禁止默认 SSE 或服务器时区。
- 来源原始样本仅允许来自实际 Tushare 响应或明确标注的内部 fixture；当前仓库不伪造网络样本。空值分布、截断标记、逻辑键冲突及 source revision 必须按响应原样记录；未知分布标记为“未采样”，不得填写估计比例。

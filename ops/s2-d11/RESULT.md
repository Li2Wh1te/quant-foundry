# S2-D11 RESULT

D11默认HTTP政策解析、共同读取窗口、固定锚点续页、独立因子表及口径控件已实现。`code_ready=true`指这套工程链在**明示人工隔离契约**下通过默认应用验证；**生产E69的pre/post仍restricted，生产口径验收未完成**。`live_verified=false`，`deployed=false`。草稿[PR184](https://github.com/Li2Wh1te/quant-foundry/pull/184)不合并、不部署，父决定剩余来源契约下的阶段验收。

依赖D10已按父授权合并[PR183](https://github.com/Li2Wh1te/quant-foundry/pull/183)，merge `e6ab8ea745032328301cdbf6be96e3d0349bddd2`，精确head四条CI通过。原worker失败与根因未定证据保留`ops/s2-d10/CI_FAILURE.json`。D11仍为同一Standard会话，无新agent/session、无fast。

实现：

- 复用D08唯一factory及真实DB/CurrentStore准入，精确representation、完整对象和可信身份有效窗口。因子默认512/最多2000，签名向前游标、升序精确Decimal、固定start/end/as_of、10,000条浏览器驻留上限。
- 代码声明的`PricePolicy`与默认HTTP bars接通；价格、因子、身份、适用日历及政策同一read_scope，全部DTO构造后最终短锁复核。pre取固定截止内最后被日历证明的open日期精确因子；post要求独立原始基准。撤销post基准不关闭pre/raw。当前政策/数据/质量变更不混接旧页。
- 必要因子按有界输入批次预取，先逐日变换再日/周/月/年完整桶分页。512根默认窗口无需逐日重扫因子文件；原行/文件/扫描、共同100,000行/4MiB和30秒session限制保留。缺日不前填，未来因子不参与，量/成交额保持原值与元/手/千元单位。
- 原S3 `checked_mul`再`div_rounded(12,HalfEven)`抽出为唯一公共纯函数，S2通过已安装的有界native批量边界调用。S3原有身份、公开/生效时点、原始锚点、研究及执行限制保持，SDK发行打包仍归D13。
- 详情壳接入BasisControls/FactorTable，能力随回放截止刷新；资料、raw和因子模块独立。网络同条件旧值标旧禁续页，数据改变/失效游标/撤权撤下。口径切换先清旧图；拒绝不会暗中改成raw。tooltip/表格继续保留精确值，浏览器没有价格复权公式。

人工契约只允许test环境服务器声明，绑定独立业务键/variant；local/production拒绝配置，HTTP不接受公式/锚点或representation。它不替换生产E69/E70绑定，也不授予生产许可。官方来源核对及逐项恢复条件见[CONTRACT_REVIEW](CONTRACT_REVIEW.md)：pre需要累计关系/日期有效性和查询锚点；post另需原始基准。未知原始归一化日期不单独阻断pre，S3历史公开时点不成为当前浏览门槛。

| 验收项 | 工程/隔离验证 | 尚未取得的生产结论 |
| --- | --- | --- |
| 11-01 默认应用pre/post | 默认create_app→真实人工正式文件→HTTP成功；无成功provider或结果注入 | E69生产基金变换许可及真实浏览器联调 |
| 11-02 固定锚点/分页 | pre/post日周月年真实续页、重复游标、扩大输出窗口一致，跨年完整/partial桶 | 真实源契约下的窗口 |
| 11-03 共同依赖/改变 | 价格/因子/身份/适用日历/政策同一窗口；因子、价格及配置在读取或提交时变更拒绝 | 生产数据联调 |
| 11-04 禁止错误来源/二次复权 | E51、其他来源/variant/HTTPrepresentation拒绝；默认E69不能补测试variant | 无新增来源许可 |
| 11-05 回放/变换顺序 | 固定as_of、不用未来因子，先变换日线再聚合，量额不缩放 | 生产累计关系/日期有效性 |
| 11-06 模块独立/失败 | 缺/坏因子不挡raw；post基准不挡pre；网络旧值禁续页、撤权清除、显式口径恢复 | 真实权限/质量联调 |
| 11-07 共用数学/S3保护 | 同一Rust算术及独立Decimal对照，S3严格时间/原始基准拒绝仍通过 | 无需放松S3条件 |

最终本地回归：D08/D09/D10/D11默认HTTP、S3研究/重采样/网关、自选及配置 **166 passed、22 subtests passed**；其中新增默认调整HTTP专项23个案例。`pnpm --dir frontend test` **176 passed**，build通过；根 **29 passed、13 subtests passed**。此前Rust1.90 core测试/fmt/clippy及正确SDK wheel **95 tests passed**，原生数学代码未再更改。输入全为人工隔离事实，未访问真实数据；精确head远端CI状态另同步PR正文。

模拟Chromium10组交互/失效检查，0浏览器错误、0旧详情价格/因子请求：[UI_RESULT](UI_RESULT.json)、[1440×900](screenshots/1440x900.png)、[1024×768](screenshots/1024x768.png)、[390px](screenshots/390px.png)。模拟响应和可选常量F=1三口径只用于UI；不证明默认后端或生产语义。

可交互人工预览：

```sh
node ops/s2-d11/mock-api.mjs
# 可选三口径UI模拟：QF_D11_UI_CONTRACT=1 node ops/s2-d11/mock-api.mjs
QF_DEV_BACKEND_URL=http://127.0.0.1:8010 pnpm --dir frontend dev --host 127.0.0.1
# http://127.0.0.1:5173/admin/data/etf-basics/510300.SH?tab=factors
# 仅合成token/数据。
node ops/s2-d11/ui-check.mjs
```

D10已移除旧浏览器复权/全历史算法，本包没有再建一套。保留原采集后端、共享dataCollections、自选、S2/S3/R01；未删除未迁移共享路径。没有真实行情/用户数据读复制、供应商数据请求、生产访问、部署、采集、reset/rebuild或写入端点。仅普通CI新增已安装wheel下的D11默认HTTP验证；未改部署配置、全量workflow触发/并发或模型并发。

额外全量CI独立于上述工程收口；旧head ARM smoke已通过，全量基准仍待结果。先前取消请求遭环境403后只读观察，无再取消/凭据或路由绕行。必要源代码提交会依现有同步规则触发新head检查，不把旧head通过状态转移到新提交。

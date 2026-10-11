# S3-D18 集中结果

接续实际 main `5a14110668f240f183bf3e6a08f638fc4e3fc02f`（D17 PR175 已合并）。
当前 D18 尚未关单：完整 B1–B4 首次全量正在运行，正式默认 API 映射仍未实现。
草稿 [PR176](https://github.com/Li2Wh1te/quant-foundry/pull/176) 的已推送基线为
`24d1076fa3099a82120fe90582955be91553bf9d`；下述进度修复已完成本地验证，尚待新 head CI。
工作区无 AGENTS.md / .agents skill；原 v1.1 包已完整阅读，未混入未发布 S2/R01 候选。

实现固定完整夹具和真实 Python 策略、CurrentStore → Arrow → Rust、数据库队列、runner
容器 → 兄弟 worker、持久结果与逐页独立资金/费用核对。B2 两频率共享唯一分钟源，60m
使用既有 Rust 重采样；B3 成交/报价各有真实逐 tick 回调和运行中修正导致 DATA_CHANGED；
B4 16 项最多两并发，杀死一项后仅重跑该项，保留原 15 项结果。小夹具绝不替代全量。

修复有界日历/按会话事实传输、下一会话 DAY 归属、重复回放扫描、受限窗口复用和数据
通道失败终态；增加实际各阶段计时、进程组 RSS/cgroup、源/结果/临时字节及回调数。
测量全组限 4 CPU/8 GiB（runner/gateway 1 CPU/2 GiB、DB 1 CPU/1 GiB、worker 合计
2 CPU/5 GiB）。明确构建、暖缓存与测量；逻辑选中文件字节不冒充物理 SSD I/O。

发现并修复真实进度缺口：原 HTTP 状态始终返回 0。Rust 现在通过私有有界 IPC 至多
每秒发送当前事件/会话计数和字符串纳秒；supervisor 合并写入一个受 claim/租约保护的
当前值，不建立逐事件台账。最终计数在成功封存前获确认，通道断开不能留下成功结果。
网页显示实际计数，不虚构总量/百分比。新增迁移仅增加 nullable progress；非空旧 S3
运行和十类非空旧历史关系升级均逐列保持原值，旧 claim、取消和终态拒绝迟到写入。

实际本地验证：fmt/clippy、workspace/public contract、生成契约/版本、根 25 项通过；
独立安装最新 release wheel 的 SDK 为 **82 项通过**。进度相关队列/数据库回归为
14 passed / 1 skipped；新增非空旧 S3 升级回归为 6 passed / 1 skipped，跳过的宿主
worker 随后在实际 runner 容器回归强制执行。`scripts/test_s3_runtime.sh` 最新为
**25 passed / 0 skipped，333.874 秒**，含运行中真实 HTTP 计数、API/CLI/Python、五示例、
取消/崩溃/OOM/超时、claim、权限隔离及结果核对。前端 152 项与构建通过。
此前六个小夹具及两个真实热修正探针通过；`full_B1_B4=false`，不能代替最新全量。
本地 Docker VFS 曾耗尽本任务磁盘，失败输出保留；只回收已核对身份/引用的本任务
可恢复构建缓存后通过，不做全局 prune。受限宿主 cleanup 检查拒绝不能冒充退役通过。

本机 overlay/tmpfs 不满足 CurrentStore 正式挂载条件；相关本地读取回归明确使用既有
测试探针替身，不能作为原生文件系统验收。全量 CI 不设置该替身，失败不放宽门禁。
x86_64 `24d1076` 干净源码 release wheel、离线 backend/worker 镜像的独立安装和旧核心缺席检查通过；实际
新空库迁移、API readyz/能力、暂停接单、runner 正常启动/停止通过；Compose 配置已用
私有占位环境验证。命令与依赖见 [README](README.md)，未执行 P01/C01。

精确 `24d1076` 的四个既有 workflow / 五个 job 全通过：后端 1333 项、前端 152 项、
Rust/SDK/网关/共享服务、真实浏览器 16 项批次部分失败与重跑、实际隔离容器 25 项、
原生退役 29 项。其 [发行与全量 CI](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38174415508)
中的原生 arm64 构建、独立 wheel 安装、离线镜像、空库迁移和正常启停已实际通过；
[arm64 制品](https://github.com/Li2Wh1te/quant-foundry/actions/runs/38174415508/artifacts/11714848130)
只证明该精确旧 head，不冒充尚未推送的进度修复。该运行的完整 B1–B4 job 仍在执行，
尚无完整通过结论；不因连接通知重启未知状态作业。

| 族 | 必需的真实检查入口 / 输出 |
|---|---|
| T01 | Rust types/numerics/accounting、安装 SDK Decimal/ns、独立资金/费用核对 |
| T02 | Rust clock/engine/matching、SDK 未来过滤/订阅、不同批次屏障 |
| T03 | Rust orders/accounting、真实 SDK 下单/目标/撤单/冻结/到期 |
| T04 | Rust rules 有效日期/沪深北/ETF/停牌/价格限制/指数拒绝 |
| T05 | Rust bar/tick 流动性/同价报价双侧量；完整 B2/B3 待运行 |
| T06 | Rust accounting 公司行动/应收/支付/股份/政策/冻结、独立手算 |
| T07 | 后端真实 CurrentStore/gateway/streams/动态质量，完整 B 读取待运行 |
| T08 | research API、Rust views/indicators、SDK 研究时点/预热/缺失 |
| T09 | 安装 SDK 回调重入/Arrow/异常/Decimal/形状/分页/过期视图 |
| T10 | `isolation-evidence.json`；完整 B4 与全组资源输出待运行 |
| T11 | `d14-examples.json`、真实 CLI/SDK/API、浏览器 CI、结果核对 |
| T12 | D17 旧核心缺席、空库/非空历史升级、cleanup 拒绝、实际镜像启动 |

保留全部既有 CI。新增标准免费公共 x86_64/arm64 runner，输出测试 wheel、
`release.json`、`images.json` 和 `final-result.json`；后者保留各族固定输入/真实结果、
资源和阶段计时。全部业务/测量源码及 native SHA 相同且原全量通过时，纯文档可复用
该原结果，另记 `reuse.json` 的原测量提交与当前核对提交，不改写原测量证据。

| 状态 | 当前证据与结论 |
|---|---|
| `new_engine_code_ready` | **false / D18 未完成**：全量 B1–B4 尚未完成，新进度 head CI 待核，正式运行映射仍未实现接线 |
| `legacy_execution_removed` | **true（源码）**：D17 103 条旧源物理移除，当前 Rust 路径/实际镜像无旧执行 fallback；精确 head CI 仍需复核 |
| `data_ready` | **false**：正式身份/会话/单位/日期规则/研究知识时间映射缺口；合成数据不作生产依据 |
| `production_switched` | **false**：未访问或部署生产，P01 未执行 |
| `runtime_cleanup_done` | **false（旧生产残留）**：C01 未执行；正常新 worker 私有 tmp/IPC 自动回收已验证 |

明确阻塞：`FormalProvider.binding` 与 `_unavailable_formal_bounds` 当前仍明确拒绝，
并非仅生产数据暂缺。E50 货币/整数份额、E51 未锚定前复权、E52 指数研究、E70 raw
依据/手转份，以及稳定身份、正式日历/日期规则等须由数据底座提供已接受事实并完成
代码映射；不能让用户改开关启用占位函数。正式默认 API 目前不能创建可执行新 run。
尚无生产或供应商访问、R01 改动/复制、权限调整、配额扩大、部署或生产清理。

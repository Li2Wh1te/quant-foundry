# S2-D12 RESULT

D11经用户阶段接受后合并[PR184](https://github.com/Li2Wh1te/quant-foundry/pull/184)，merge `2c24bfce435c23cd4222a4a6547690fb8d4ab9a7`；精确head四条常规CI通过。E69生产pre/post仍restricted，未记live_verified或deployed。D12从此提交建立独立分支`s2/d12-market-retirement`，同一Standard会话，无新agent/session或fast。

实现：总览复用D09正式ETF目录摘要和D08读取会话，数量明确为当前来源目录业务对象；上市日期、业务截至、读取时点、行情覆盖分开。日历无全局正式计数契约，入口显示按交易所/有界日期查看，数量未知。运营指标仍从运营API读取，独立失败不关闭正式摘要；来源配置/运行结果不作为当前读取许可。网络同条件旧摘要标旧，质量/权限/结构/未知失败清除；共享自选403也进入既有失效总线并清除旧侧栏结果。通用数据资产说明明确存储说明与市场声明口径的不同范围。

反向引用、动态导入、App路由、请求路径、测试/文档和产物扫描后精确处置：

| 处置 | 实际内容 |
| --- | --- |
| 删除 | 无消费者的旧日历/ETF总览/详情/日线/因子前端请求、DTO及时间范围辅助函数；旧日历查询仓储、ETF概览方法与DTO、ETF日线/因子市场查询方法及其旧实现测试 |
| 受保护410 | 7个旧市场GET：ETF概览/详情/日线/因子，日历列表/概览/单日。鉴权后`LEGACY_MARKET_RETIRED`，无DB读取、无伪成功/采集fallback；OpenAPI也声明410 |
| 共享保留 | `GET /data-collections/etfs`、`EtfQueryRepository.list_codes`及资料`get_code`供现有自选搜索/保存；DataCollectionApiError及目录DTO；自选原生日收盘/CRUD、其仓储与原生模型 |
| 采集/诊断保留 | 全部原生写入服务、供应商适配、调度、来源配置、S2数据资产；同花顺独立诊断API；S3/账户/历史结果与R01内容 |
| 已由D10处理 | 旧浏览器复权/全历史业务聚合已删除，本包没有新建算法副本；绘图适配及有界技术指标保留 |

InstrumentPicker目前未发现静态/动态路由导入，但其共享代码目录契约保留；未把静态未引用当成删掉其他领域功能的充分证明。自选仍是明确保留的原生共享服务，不宣称迁为正式价格。未删除任何数据库、原件、R01文件或历史结果。

验证：

- 默认create_app、真实私有PG/Parquet/DuckDB、人工正式输入。额外未选择representation的物理记录不进入目录计数；来源disabled仍能读合格正式记录；本地扫描失败后旧合格日线仍可读，覆盖未核验。没有成功provider/HTTP结果注入。
- 旧市场SQL/HTTP被测试阻断，总览→列表→正式日历→旧symbol资料→raw K线的真实浏览器链成功，10根正式Bar可显示；生产pre/post仍禁用。共享自选真实隔离写入/读取正常；仅注入403失败，旧自选和主图撤下。[FORMAL_BROWSER_RESULT](FORMAL_BROWSER_RESULT.json)。
- 7个旧入口：未认证401、认证后410，测试期间禁止任何DB访问；自选、原生日历保存、来源/运营、鉴权及S3研究保护回归通过。默认HTTP/撤权/真实浏览器专项10 passed；影响范围回归164 passed、15 subtests passed。全后端首轮1571 passed、14 skipped、98 subtests passed，另有3项验证失败；已复用现有显式overlay测试探针并修正循环FK测试的DDL生命周期假设，相关38项通过，生产门禁/约束不变；最终全后端复跑进行中。
- `pnpm --dir frontend test`180 passed；build通过；根29测试通过。模拟UI7组交互/失败检查、三尺寸截图及可交互人工预览：[UI_RESULT](UI_RESULT.json)、[1440×900](screenshots/1440x900.png)、[1024×768](screenshots/1024x768.png)、[390px](screenshots/390px.png)。模拟UI不作真实服务证据；真实浏览器链另如上。
- 浏览器实际组合验证命令：先正常frontend build，再在backend锁定隔离环境以`QF_D12_BROWSER=1 LF_D01_ALLOW_OVERLAY_TEST=1 uv run --locked pytest -q tests/test_s2_market_retirement.py`运行。默认CI不要求本地Chromium的可选浏览器案例；普通默认HTTP案例不因此跳过。生产部署路径不注册fixture。

人工交互预览：`node ops/s2-d12/preview-api.mjs`，另以`QF_DEV_BACKEND_URL=http://127.0.0.1:8012 pnpm --dir frontend dev --host 127.0.0.1`启动；访问`/admin`，仅合成token。截图脚本`node ops/s2-d12/ui-check.mjs`默认5173，可用`QF_D12_PREVIEW_URL`指向本地预览。

`code_ready`以最终实际测试和草稿PR精确head检查为准；`live_verified=false`、`deployed=false`。不访问真实市场/用户数据、不调用供应商数据API、不部署、不reset/rebuild、不改生产任务。前后端成套发布及D13组合发行接线留给后续授权包；E69生产语义限制按D11保留。最终CI状态记录PR正文，不把仅静态扫描、模拟截图或旧head检查冒充正式验收。

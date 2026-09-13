/** This catalog documents the guarded Python facades in strategy_protocol/context.py
 * and data_view.py, including the stricter windows enforced by ChunkStrategyDataView.
 * Examples run inside a strategy decision with an existing context; they never
 * invent instrument identifiers or imply that an HTTP endpoint executes Python. */
export const API_GROUPS = [
  { id: "context", label: "上下文", index: "CONTEXT" },
  { id: "data", label: "市场数据", index: "MARKET DATA" },
  { id: "clock", label: "策略时钟", index: "CLOCK" },
  { id: "universe", label: "标的池", index: "UNIVERSE" }
] as const;
export type ApiGroup = typeof API_GROUPS[number]["id"];
export type Property = readonly [string, string];
export interface ApiEntry {
  id: string; group: ApiGroup; signature: string; summary: string; badge: string;
  meta: Property[]; params: Property[]; returns: Property[]; notes: string[];
  examples: { title: string; code: string }[]; boundary: Property[];
}
const WINDOW_PARAMS: Property[] = [
  ["instrument_id", "必填，单个稳定标的 UUID 或合法 UUID 字符串；从候选池获取，不使用交易代码替代。"],
  ["lookback_sessions", "回看交易日数，正整数，协议上限 512；运行时可设置更低上限。与起止日期均互斥。"],
  ["start_date / end_date", "显式日期区间，两个 date 必须同时提供，且起点不晚于终点。不能与 lookback_sessions 混用。"]
];
const WINDOW_BOUNDARY: Property[] = [
  ["查询上限", "由当前决策步 data_cutoff 和当日数据完整性共同确定；不完整的截止日不可读取。请求越界会失败，不静默截断。"],
  ["开盘前 / 收盘后", "D 日开盘前只能读取此前已完成交易日；收盘后仅在完整性条件满足时允许读取 D 日。D-1 指前一交易日，不是前一自然日。"],
  ["窗口规则", "回看以运行时可见边界为锚点，按交易日解析；显式范围必须同时提供起止日期，不支持无界查询。"],
  ["缺失历史", "不补齐或前向填充。PIT 回看历史不完整可能以 history_incomplete 失败，不能将缺失行情当作零值。"]
];
const CANDIDATES = `candidates = context.universe.query(asset_classes=["ETF"])`;
export const API_ENTRIES: ApiEntry[] = [
  {
    id: "session_date", group: "context", signature: "context.session_date",
    summary: "返回当前策略决策所在的交易日。", badge: "只读值",
    meta: [["类型", "date"], ["访问方式", "只读属性"], ["可见性", "当前决策步"]], params: [],
    returns: [["date", "当前决策步的市场交易日，与 context.clock.today() 一致。"]],
    notes: ["该值由运行时确定，策略不能修改。", "交易日不等于行情可见截止日；查询数据时仍由 data_cutoff 约束。"],
    examples: [{ title: "读取当前交易日", code: "session_date = context.session_date\nis_january = session_date.month == 1" }],
    boundary: [["决策时点", "在当前决策上下文中读取；同一步内值固定。"], ["市场日期", "来自策略会话，不读取操作系统本地日期。"], ["数据可见性", "当前交易日不代表当日完整日线已经可见。"]]
  },
  {
    id: "data_cutoff", group: "context", signature: "context.data_cutoff",
    summary: "查看当前决策步允许访问数据的时间上限。", badge: "只读值",
    meta: [["类型", "datetime"], ["时区", "带时区"], ["访问方式", "只读属性"]], params: [],
    returns: [["datetime", "带时区的可见截止时点，不晚于 context.decision_time。"]],
    notes: ["该值不能由策略覆盖或延后。", "时间上限不是数据完整性证明；实际查询还需满足截止日完整性、历史覆盖和数据源能力约束。"],
    examples: [{ title: "读取可见截止时点", code: "cutoff = context.data_cutoff\ncutoff_text = cutoff.isoformat()\ndecision_time = context.clock.now()" }],
    boundary: [["时间关系", "data_cutoff ≤ decision_time；策略无法通过参数绕过这一边界。"], ["日期与时点", "data_cutoff 是带时区的时点，session_date 是交易日期，二者不可互换。"], ["当日数据", "截止日的完整日线是否可读由运行时完整性条件确定。"]]
  },
  {
    id: "bars", group: "data", signature: "context.data.bars()",
    summary: "读取单个标的的原始日线行情，按交易日期升序返回。", badge: "时间边界",
    meta: [["返回", "tuple[BarDTO, ...]"], ["频率", "日频"], ["排序", "日期升序"]], params: WINDOW_PARAMS,
    returns: [["instrument_id", "UUID，稳定标的身份。"], ["trade_date", "date，行情所属交易日。"], ["values", "只读字段映射，值为 Decimal；包含实际可用的 open、high、low、close、volume、amount。缺失字段不伪造。"]],
    notes: ["一次查询一个标的；无 instrument_ids 批量参数，也无 fields 投影参数。通过 bar.values 读取字段，成交量字段是 volume。", "以下片段放在策略决策函数中，context 由运行时提供；不是可独立运行的脚本。候选池为空时示例不发起行情查询。", "数据不足、身份映射缺失或越界会由运行时报告错误；更换窗口或补齐数据后再运行，不以假行情继续计算。"],
    examples: [
      { title: "读取最近 20 个交易日", code: `${CANDIDATES}\ncloses = []\nif candidates:\n    bars = context.data.bars(\n        candidates[0].instrument_id,\n        lookback_sessions=20,\n    )\n    closes = [bar.values.get("close") for bar in bars]` },
      { title: "按已可见日期读取", code: `${CANDIDATES}\nselected_bars = ()\nif candidates:\n    instrument_id = candidates[0].instrument_id\n    recent = context.data.bars(instrument_id, lookback_sessions=20)\n    if recent:\n        selected_bars = context.data.bars(\n            instrument_id,\n            start_date=recent[0].trade_date,\n            end_date=recent[-1].trade_date,\n        )` }
    ], boundary: WINDOW_BOUNDARY
  },
  {
    id: "adjusted_series", group: "data", signature: "context.data.adjusted_series()",
    summary: "读取单个标的的复权因子点序列；不会返回完整复权 OHLC 行情。", badge: "时间边界",
    meta: [["返回", "tuple[AdjustedSeriesPointDTO, ...]"], ["条件", "复权策略已启用"]],
    params: [...WINDOW_PARAMS, ["basis", "qfq 或 hfq。方法签名默认 raw，但真实回测适配器不支持 raw 因子查询；原始行情应使用 bars()。"]],
    returns: [["instrument_id", "UUID，稳定标的身份。"], ["trade_date", "date，因子所属交易日。"], ["adj_factor", "Decimal，有限且大于零的因子值；不等于复权后的价格。"]],
    notes: ["qfq / hfq 要求 tushare_adj_factor_native@1 已验证且启用，并且本次运行的数据源支持因子读取。", "未启用时抛出 AdjustmentNotActiveError；数据源不支持时抛出 UnsupportedCapabilityError。选择 basis 不会自动启用服务。", "以下片段在已满足复权条件的策略决策中使用。复权点读取和价格计算是不同能力，不使用未来因子自行复权。"],
    examples: [{ title: "读取已启用的复权因子", code: `# Requires a verified, active adjustment policy and provider support.\n${CANDIDATES}\nfactors = ()\nif candidates:\n    factors = context.data.adjusted_series(\n        candidates[0].instrument_id,\n        basis="qfq",\n        lookback_sessions=20,\n    )\nfactor_values = [point.adj_factor for point in factors]` }],
    boundary: [...WINDOW_BOUNDARY, ["复权证据", "因子读取同样受时间边界和数据完整性约束；不能借复权读取未来数据。"]]
  },
  {
    id: "now", group: "clock", signature: "context.clock.now()",
    summary: "返回当前决策步固定的决策时间。", badge: "只读",
    meta: [["返回", "datetime"], ["时区", "带时区"], ["时钟", "确定性"]], params: [],
    returns: [["datetime", "当前步 decision_time；同一步内重复调用返回相同时点。"]],
    notes: ["这是策略时钟，不是服务器当前时间。回测执行速度不改变该值。", "不提供 clock.sessions() 交易日历查询。历史行情窗口由数据接口按运行时交易日解析。"],
    examples: [{ title: "读取决策时间", code: "decision_time = context.clock.now()\ndecision_time_text = decision_time.isoformat()" }],
    boundary: [["时钟来源", "与 context.decision_time 一致，不使用系统墙上时钟。"], ["数据上限", "决策时间可能晚于 data_cutoff；不能用 now() 替代查询的可见截止时点。"]]
  },
  {
    id: "today", group: "clock", signature: "context.clock.today()",
    summary: "通过策略时钟读取当前会话的交易日。", badge: "只读",
    meta: [["返回", "date"], ["时钟", "确定性"]], params: [],
    returns: [["date", "与 context.session_date 相同的交易日期。"]],
    notes: ["同一决策步重复调用值不变。", "该方法不返回交易日列表，也不查询未来交易日历。"],
    examples: [{ title: "读取策略日期", code: "trading_day = context.clock.today()\nis_same_session = trading_day == context.session_date" }],
    boundary: [["日期来源", "来自当前市场会话，而非 date.today()。"], ["可见性", "当前交易日不意味着同日全部市场数据可读，查询仍遵循 data_cutoff。"]]
  },
  {
    id: "query", group: "universe", signature: "context.universe.query()",
    summary: "读取当前决策步符合 PIT 资格规则的候选标的。", badge: "筛选器",
    meta: [["返回", "tuple[InstrumentCandidateDTO, ...]"], ["身份", "稳定 UUID"], ["排序", "instrument_id"]],
    params: [["exchanges", "可选，交易所标签的可迭代对象，例如 [\"SSE\", \"SZSE\"]；不能传单个字符串。"], ["asset_classes", "可选，资产类别标签的可迭代对象，例如 [\"ETF\"]。"]],
    returns: [["instrument_id", "UUID；用于行情查询和策略目标。"], ["trading_code / name / display_name", "交易代码、名称和展示名，属于展示信息，不替代稳定 UUID。"], ["asset_class / exchange", "资产类别和交易所。候选 DTO 共六个字段，不包含任意基础资料映射。"]],
    notes: ["候选池由当前步绑定的资格规则、时间边界和运行范围确定，策略只能进一步筛选，不能放宽边界。", "不支持 status、min_history_sessions、require_bar_on_cutoff 参数。不得将当前数据库的上市状态或名称作为历史资格依据。", "结果去重，并按 instrument_id 的字符串顺序稳定排序；同一查询范围内重复查询可复用缓存。空候选返回空元组。"],
    examples: [{ title: "筛选 ETF 候选", code: `candidates = context.universe.query(\n    exchanges=["SSE", "SZSE"],\n    asset_classes=["ETF"],\n)\ninstrument_ids = [item.instrument_id for item in candidates]` }],
    boundary: [["PIT 资格", "资格由运行时按当前步生效的证据和范围判断，不等同于今天的 ETF 基础资料列表。"], ["历史覆盖", "进入候选池不保证任意回看长度的数据都完整；历史查询仍可能因覆盖不足失败。"], ["身份边界", "使用返回的稳定 UUID 查询行情；不从交易代码拼造 UUID。"]]
  }
];

export const TABS = [{ id: "reference", label: "说明" }, { id: "example", label: "示例" }, { id: "boundary", label: "时间边界" }] as const;
export interface CatalogState { query: string; type: ApiGroup | "all"; api: string; tab: typeof TABS[number]["id"]; example: number }
export const DEFAULT_STATE: CatalogState = { query: "", type: "all", api: "session_date", tab: "reference", example: 0 };

/** Only known catalog values are restored; stale saved entries cannot blank the
 * page after a future documentation update or select an out-of-range example. */
export function restoreCatalogState(value: unknown): CatalogState {
  if (!value || typeof value !== "object") return { ...DEFAULT_STATE };
  const state = value as Partial<CatalogState>;
  const entry = API_ENTRIES.find(item => item.id === state.api) ?? API_ENTRIES[0];
  return {
    query: typeof state.query === "string" ? state.query : "",
    type: API_GROUPS.some(group => group.id === state.type) ? state.type! : "all",
    api: entry.id,
    tab: TABS.some(tab => tab.id === state.tab) ? state.tab! : "reference",
    example: Number.isInteger(state.example) && state.example! >= 0 && state.example! < entry.examples.length ? state.example! : 0
  };
}
export function filterCatalog(query: string, type: CatalogState["type"]) {
  const text = query.trim().toLowerCase();
  return API_ENTRIES.filter(entry => (type === "all" || entry.group === type)
    && `${entry.signature} ${entry.summary} ${entry.badge}`.toLowerCase().includes(text));
}

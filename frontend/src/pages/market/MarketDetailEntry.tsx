import { ArrowLeft, ChartCandlestick, RefreshCw } from "lucide-react";
import { useCallback, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { OverviewShell } from "../../components/OverviewShell";
import { useAuth } from "../../auth/AuthContext";
import { marketClient } from "../../features/market-data/data";
import { MarketInstrumentContext } from "../../features/market-data/catalog/MarketInstrumentContext";
import { useMarketResource } from "../../features/market-data/catalog/useMarketResource";
import { exchangeName, managementFee, MARKET_PATH, marketDate, marketReturnTo, marketTime, statusName } from "./marketPresentation";
import "../Market.css";

/** The one old-link entry: resolve trusted identity before mounting D10.
 * Explicit profile links can read source-scoped metadata even without a mapping.
 * D10 owns children (chart/factors); this wrapper never loads their legacy data.
 */
export function MarketDetailEntry({ children }: { children: ReactNode }) {
  const { tsCode = "" } = useParams();
  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { logout } = useAuth();
  const [revision, setRevision] = useState(0);
  const onUnauthorized = useCallback(() => { logout(); navigate("/login", { replace: true }); }, [logout, navigate]);
  const stableInput = tsCode.startsWith("instrument:");
  const sourceInput = tsCode.startsWith("etf-current:");
  const loadResolution = useCallback((signal: AbortSignal) => marketClient.resolve(tsCode, "etf", { signal }), [tsCode]);
  const identity = useMarketResource(`identity:etf:${tsCode}`, loadResolution, revision, onUnauthorized, !stableInput && !sourceInput);
  const instrumentKey = stableInput ? tsCode : identity.data?.instrument_key ?? null;
  const profileKey = instrumentKey ?? (sourceInput ? tsCode : `etf-current:${tsCode}`);
  const loadProfile = useCallback((signal: AbortSignal) => marketClient.instrument(profileKey, { signal }), [profileKey]);
  const denied = identity.error && [401, 403].includes(identity.error.status);
  const profile = useMarketResource(`profile:${profileKey}`, loadProfile, revision, onUnauthorized, !denied);
  const returnTo = marketReturnTo(location.state?.marketReturnTo);
  const item = profile.data?.instrument;
  const profileOnly = params.get("tab") === "profile" || sourceInput;
  const identityFresh = stableInput || (!identity.pending && !identity.error && !identity.stale && identity.data !== null);
  const profileDenied = profile.error && [401, 403].includes(profile.error.status);
  const canOpen = instrumentKey && identityFresh && !profileDenied;
  const resolvedSymbol = item?.symbol ?? identity.data?.symbol ?? tsCode;
  if (!profileOnly && canOpen) {
    return <MarketInstrumentContext.Provider value={{ instrumentKey, symbol: resolvedSymbol,
      profile: profile.data, resolution: identity.data }}>{children}</MarketInstrumentContext.Provider>;
  }

  return <OverviewShell title="ETF 基础资料" section="MARKET DATA" className="qfm-root"><section className="qfm-content" aria-labelledby="qfm-profile-title">
    <header className="qfo-page-head qfm-page-head"><div><Link className="qfm-code" to={returnTo}><ArrowLeft size={14} aria-hidden="true" /> 返回 ETF 列表</Link><h1 id="qfm-profile-title">{item?.name ?? "ETF 基础资料"}</h1><p className="qfo-page-desc">{item?.symbol ?? tsCode} · 当前正式资料{profile.stale ? " · 旧资料" : ""}</p></div><div className="qfo-page-actions"><button className="qfo-secondary-btn" type="button" disabled={profile.pending || identity.pending} onClick={() => setRevision(value => value + 1)}><RefreshCw size={16} aria-hidden="true" />重新读取</button>{canOpen && <Link className="qfo-secondary-btn" to={`${MARKET_PATH}/${encodeURIComponent(resolvedSymbol)}?tab=overview`} state={{ marketReturnTo: returnTo }}><ChartCandlestick size={16} aria-hidden="true" />查看 K 线</Link>}</div></header>
    {identity.error && <div className="qfm-error" role="alert">身份解析：{identity.error.message}基础资料与历史图表分别判断。</div>}
    {identity.stale && <div className="qfm-error" role="status">身份为上次读取的旧资料，请重新读取后进入历史图表。</div>}
    {profile.error && <div className="qfm-error" role="alert">基础资料：{profile.error.message}{profile.stale && " 当前显示同条件旧资料。"}</div>}
    <section className="qfm-workspace qfm-profile" aria-label="正式基础资料" aria-busy={profile.pending}>
      <header className="qfm-workspace-head"><h2>基础资料</h2><span className="qfm-workspace-meta">ETF · 当前目录范围</span></header>
      {item ? <><dl className="qfm-profile-grid">{[
        ["基金代码", item.symbol], ["名称", item.name ?? "未知"], ["交易所", exchangeName(item.exchange)],
        ["上市状态", statusName(item.list_status)], ["上市日期", marketDate(item.list_date)],
        ["跟踪指数", item.index_name ?? "未知"], ["管理人", item.manager_name ?? "未知"],
        ["管理费率", managementFee(item.management_fee, item.management_fee_unit)],
      ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      <div className="qfm-source-note">资料可读不代表历史价格或复权因子已通过请求检查。缺失业务字段保持未知。</div>
      <dl className="qfm-profile-grid">{item.capabilities.map(capability => <div key={capability.name}><dt>{({ metadata: "基础资料", identity_resolution: "可信身份", "etf-daily-local": "日线读取", "etf-factors-local": "复权因子" } as Record<string, string>)[capability.name] ?? capability.name}</dt><dd>{({ available: "可用", restricted: "受限", unavailable: "不可用", unknown: "尚未核验" } as Record<string, string>)[capability.availability]}{!capability.request_checked && " · 具体请求待检查"}</dd></div>)}</dl></> : <p className="qfm-source-note">{profile.pending ? "正在读取正式基础资料…" : "当前基础资料不可读取。"}</p>}
    </section>
    {profile.data && <details className="qfm-read-details" open><summary>正式资料读取说明</summary><p>{profile.data.meta.scope}。读取时点：{marketTime(profile.data.meta.read_at)}；业务截至日：{marketDate(profile.data.meta.business_as_of)}。</p><p>{profile.data.meta.source_labels.join(" · ")}。{profile.data.meta.coverage.note}</p>{profile.data.meta.limitations.map(notice => <p key={notice.code}>{notice.message}</p>)}</details>}
  </section></OverviewShell>;
}

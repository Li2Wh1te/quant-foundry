import { Link } from "react-router-dom";
import type { EtfPage } from "../../api/dataCollections";
import { exchangeName, managementFee, MARKET_PATH, marketDate, statusName } from "./marketPresentation";

export function MarketTable({ result, loading, error, empty, returnTo, onOpen }: {
  result: EtfPage | null; loading: boolean; error: boolean; empty: boolean; returnTo: string; onOpen: (code: string) => void;
}) {
  return <table className="qfm-table" aria-label="ETF 基础资料" aria-busy={loading}>
    <colgroup>{[12, 23, 8, 9, 11, 17, 12, 8].map((width, index) => <col key={index} style={{ width: `${width}%` }} />)}</colgroup>
    <thead><tr>{["基金代码", "名称", "交易所", "上市状态", "上市日期", "跟踪指数", "管理人", "管理费率"].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
    <tbody>{result?.items.map(etf => {
      const name = etf.csname || etf.extname || etf.cname || "未提供名称";
      const target = `${MARKET_PATH}/${encodeURIComponent(etf.ts_code)}`;
      return <tr key={etf.ts_code} data-code={etf.ts_code} onClick={event => {
        // Native links preserve open-in-new-tab and browser context menus.
        if (!(event.target as HTMLElement).closest("a") && !window.getSelection()?.toString()) event.currentTarget.querySelector<HTMLAnchorElement>("a")?.click();
      }}><td><Link className="qfm-code" to={target} state={{ marketReturnTo: returnTo }} onClick={() => onOpen(etf.ts_code)} aria-label={`查看 ${etf.ts_code} ${name} 详情`}>{etf.ts_code}</Link></td><td><Link className="qfm-name" to={target} state={{ marketReturnTo: returnTo }} onClick={() => onOpen(etf.ts_code)} title={etf.cname || name}>{name}</Link><div className="qfm-name-sub">{etf.etf_type || "—"}</div></td><td>{exchangeName(etf.exchange)}</td><td><span className={`qfm-state qfm-state-${etf.list_status}`}><i />{statusName(etf.list_status)}</span></td><td className="qfm-mono qfm-muted">{marketDate(etf.list_date)}</td><td><span className="qfm-name" title={[etf.index_name, etf.index_code].filter(Boolean).join(" · ")}>{etf.index_name || etf.index_code || "—"}</span></td><td title={etf.mgr_name || undefined}>{etf.mgr_name || "—"}</td><td className="qfm-mono">{managementFee(etf.mgt_fee)}</td></tr>;
    })}
    {!result && loading && Array.from({ length: 6 }, (_, index) => <tr key={index} aria-hidden="true" className="qfm-skeleton-row">{Array.from({ length: 8 }, (_, cell) => <td key={cell}><span className="qfm-skeleton" /></td>)}</tr>)}
    {(!loading || result) && !result?.items.length && <tr><td className="qfm-empty" colSpan={8}>{error ? "ETF 数据暂不可用，请重新加载。" : empty ? <>尚未采集 ETF 基础资料，请先配置并运行<Link to="/admin/tasks">采集任务</Link>。</> : "没有符合当前筛选条件的 ETF，请调整搜索或筛选条件。"}</td></tr>}
    </tbody>
  </table>;
}

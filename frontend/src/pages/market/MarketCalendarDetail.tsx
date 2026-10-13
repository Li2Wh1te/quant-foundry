import type { CalendarResult } from "../../features/market-data/data";
import { CALENDAR_LABELS, calendarState, exchangeName, marketDate, marketTime } from "./marketPresentation";

export interface CalendarDetailSnapshot { key: string; date: string; exchange: string; value: CalendarResult }

export function MarketCalendarDetail({ detail, date, exchange, loading, error, stale = false, onRetry }: {
  detail: CalendarDetailSnapshot | null; date: string; exchange: string; loading: boolean;
  error: string; stale?: boolean; onRetry: () => void;
}) {
  // A selected condition change withdraws the old facts synchronously. Only a
  // same-condition network/timeout can retain an explicitly stale date result.
  const shown = detail?.key === `${exchange}:${date}` ? detail.value : null;
  const day = shown?.items[0];
  const state = calendarState(day);
  return <aside className="qfm-calendar-aside" aria-label="日期详情" aria-busy={loading}>
    <div className="qfm-dialog-kicker">TRADING DAY</div><div className="qfm-aside-date">{marketDate(date)}</div>
    <div className={`qfm-aside-state qfm-${state}`} role="status"><i />{shown ? CALENDAR_LABELS[state] : loading ? "读取中" : error ? "暂不可用" : "未知"}{stale && " · 旧资料"}</div>
    {error && <div className="qfm-error" role="alert">{error}{stale && " 当前显示同日期旧资料。"}<button type="button" className="qfm-text-button" onClick={onRetry}>重新读取</button></div>}
    <dl className="qfm-detail-list"><div><dt>交易所</dt><dd>{exchangeName(exchange)}</dd></div><div><dt>前一交易日</dt><dd className="qfm-mono">{marketDate(day?.previous_trading_date)}</dd></div><div><dt>下一交易日</dt><dd className="qfm-mono">{marketDate(day?.next_trading_date)}</dd></div><div><dt>读取时点</dt><dd className="qfm-mono" title={shown?.meta.read_at}>{marketTime(shown?.meta.read_at)}</dd></div></dl>
    {shown && <p className="qfm-aside-note">{state === "unknown" ? "正式日期资料未说明此日交易状态，不能推定为休市。" : "日期状态来自正式记录。"}前后交易日缺少独立依据，保持未知。</p>}
    {shown && <details className="qfm-read-details"><summary>日期读取说明</summary><p>{shown.meta.scope}。{shown.meta.coverage.note}</p></details>}
  </aside>;
}

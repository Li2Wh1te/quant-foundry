import { useEffect, useState } from "react";
import type { TradingCalendarDetail } from "../../api/dataCollections";
import { CALENDAR_LABELS, calendarState, exchangeName, marketDate, marketTime } from "./marketPresentation";

export interface CalendarDetailSnapshot {
  key: string;
  date: string;
  exchange: string;
  value: TradingCalendarDetail | null;
}

/** Keep the date, exchange, and facts in one snapshot while another date loads.
 * Mixing a newly selected heading with the previous date's facts would be misleading. */
export function MarketCalendarDetail({ detail, date, exchange, loading, error, onRetry }: {
  detail: CalendarDetailSnapshot | null;
  date: string;
  exchange: string;
  loading: boolean;
  error: string;
  onRetry: () => void;
}) {
  const pendingKey = loading ? `${exchange}:${date}` : null;
  const [delayedKey, setDelayedKey] = useState<string | null>(null);
  useEffect(() => {
    setDelayedKey(null);
    if (!pendingKey) return;
    // Fast responses replace the facts directly. A slow request gets a separate
    // scoped message without clearing or recoloring the last successful detail.
    const timer = window.setTimeout(() => setDelayedKey(pendingKey), 350);
    return () => window.clearTimeout(timer);
  }, [pendingKey]);
  const shownDate = detail?.date ?? date;
  const shownExchange = detail?.exchange ?? exchange;
  const state = calendarState(detail?.value);
  const targetLabel = `${exchangeName(exchange)} ${marketDate(date)}`;
  const retainedLabel = detail ? `当前显示 ${exchangeName(shownExchange)} ${marketDate(shownDate)} 的详情。` : "";

  return <aside className="qfm-calendar-aside" aria-label="日期详情" aria-busy={loading}>
    <div className="qfm-dialog-kicker">TRADING DAY</div><div className="qfm-aside-date">{marketDate(shownDate)}</div>
    <div className={`qfm-aside-state qfm-${state}`} role="status"><i />{detail ? CALENDAR_LABELS[state] : loading ? "加载中" : error ? "暂不可用" : "未采集"}</div>
    {error && <div className="qfm-error" role="alert">{error}{retainedLabel}<button type="button" className="qfm-text-button" onClick={onRetry}>重试</button></div>}
    <dl className="qfm-detail-list"><div><dt>交易所</dt><dd>{exchangeName(shownExchange)}</dd></div><div><dt>前一交易日</dt><dd className="qfm-mono">{marketDate(detail?.value?.previous_trading_date)}</dd></div><div><dt>下一交易日</dt><dd className="qfm-mono">{marketDate(detail?.value?.next_trading_date)}</dd></div><div><dt>更新时间</dt><dd className="qfm-mono" title={detail?.value?.updated_at}>{marketTime(detail?.value?.updated_at)}</dd></div></dl>
    {detail && (!detail.value || !detail.value.next_trading_date) && <p className="qfm-aside-note">{detail.value ? "后续日期覆盖不足，下一交易日暂未知。" : "此日期尚未采集，交易状态和前后交易日暂未知。"}</p>}
    {pendingKey && delayedKey === pendingKey && <p className="qfm-aside-note" role="status">正在加载 {targetLabel} 的日期详情…{retainedLabel}</p>}
  </aside>;
}

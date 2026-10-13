import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { marketClient } from "../../features/market-data/data";
import { useMarketResource } from "../../features/market-data/catalog/useMarketResource";
import { ExchangeFilter } from "./ExchangeFilter";
import { MarketCalendarDetail } from "./MarketCalendarDetail";
import { CALENDAR_LABELS, calendarDates, calendarState, exchangeName, marketDate, shanghaiToday, shiftMonth } from "./marketPresentation";

export function MarketCalendar({ onClose, onUnauthorized }: { onClose: () => void; onUnauthorized: () => void }) {
  const today = shanghaiToday();
  const [month, setMonth] = useState(today.slice(0, 7));
  const [selected, setSelected] = useState(today);
  const [exchange, setExchange] = useState("SSE");
  const [revision, setRevision] = useState(0);
  const dialog = useRef<HTMLDialogElement>(null);
  const dates = useMemo(() => calendarDates(month), [month]);
  const gridKey = `${exchange}:${dates[0]}:${dates[41]}`;
  const detailKey = `${exchange}:${selected}`;
  const loadDays = useCallback((signal: AbortSignal) => marketClient.calendar({ exchange, start: dates[0], end: dates[41], signal }), [exchange, dates]);
  const loadDetail = useCallback((signal: AbortSignal) => marketClient.calendarDay(exchange, selected, { signal }), [exchange, selected]);
  const days = useMarketResource(gridKey, loadDays, revision, onUnauthorized);
  const detail = useMarketResource(detailKey, loadDetail, revision, onUnauthorized);
  const dayMap = new Map(days.data?.items.map(day => [day.date, day]));
  const loading = days.pending || (!days.data && !days.error);
  const detailLoading = detail.pending || (!detail.data && !detail.error);
  const detailSnapshot = detail.data ? { key: detailKey, date: selected, exchange, value: detail.data } : null;

  useEffect(() => {
    const node = dialog.current;
    const previous = document.activeElement as HTMLElement | null;
    node?.showModal();
    return () => { node?.close(); requestAnimationFrame(() => { if (previous?.isConnected) previous.focus({ preventScroll: true }); }); };
  }, []);

  function moveDate(event: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    const delta = ({ ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 } as Record<string, number>)[event.key];
    if (delta === undefined) return;
    event.preventDefault();
    const next = dates[Math.max(0, Math.min(41, index + delta))];
    setSelected(next);
    dialog.current?.querySelector<HTMLButtonElement>(`button[data-date="${next}"]`)?.focus();
  }

  return <dialog ref={dialog} className="qfm-calendar-dialog" aria-labelledby="qfm-calendar-title" onCancel={event => { event.preventDefault(); onClose(); }} onKeyDown={event => {
    if (event.key !== "Tab") return;
    const controls = [...event.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled),a[href],input,select,[tabindex]")]
      .filter(element => element.tabIndex >= 0 && element.getClientRects().length > 0);
    const next = controls.indexOf(document.activeElement as HTMLElement) + (event.shiftKey ? -1 : 1);
    if (next < 0 || next >= controls.length) {
      event.preventDefault(); controls[(next + controls.length) % controls.length]?.focus();
    }
  }} onClick={event => {
    if (event.target !== event.currentTarget) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
  }}>
    <header className="qfm-calendar-head"><div><div className="qfm-dialog-kicker">MARKET CALENDAR</div><h2 id="qfm-calendar-title">交易日历</h2></div><button className="qfo-icon-btn" type="button" aria-label="关闭交易日历" title="关闭交易日历" autoFocus onClick={onClose}><X aria-hidden="true" /></button></header>
    <div className="qfm-calendar-toolbar"><ExchangeFilter value={exchange} onChange={setExchange} /><div className="qfm-month-nav" aria-label="月份切换"><button type="button" aria-label="上个月" title="上个月" disabled={month <= "1900-02"} onClick={() => setMonth(value => shiftMonth(value, -1))}><ChevronLeft aria-hidden="true" /></button><span className="qfm-month-label" aria-live="polite">{month.replace("-", " / ")}</span><button type="button" aria-label="下个月" title="下个月" disabled={month >= "2199-11"} onClick={() => setMonth(value => shiftMonth(value, 1))}><ChevronRight aria-hidden="true" /></button></div><div className="qfm-legend"><span><i />交易日</span><span><i className="qfm-closed" />休市</span><span>虚线 · 未知</span></div></div>
    <div className="qfm-calendar-body">
      <section className="qfm-calendar-main" aria-label={`${exchangeName(exchange)} ${month} 月历`} aria-busy={loading}>
        {days.error && <div className="qfm-error" role="alert">{days.error.message}{days.stale && " 当前显示本月同条件的旧日期资料。"}<button type="button" className="qfm-text-button" onClick={() => setRevision(value => value + 1)}>重新读取</button></div>}
        <div className="qfm-weekdays">{["周一", "周二", "周三", "周四", "周五", "周六", "周日"].map(day => <span key={day}>{day}</span>)}</div>
        <div className="qfm-calendar-grid">{dates.map((date, index) => {
          const dayState = calendarState(dayMap.get(date));
          const label = !days.data ? loading ? "读取中" : days.error ? "不可用" : "未知" : CALENDAR_LABELS[dayState];
          const active = dates.includes(selected) ? selected === date : index === 0;
          return <button type="button" key={date} data-date={date} tabIndex={active ? 0 : -1} onKeyDown={event => moveDate(event, index)} className={`qfm-day qfm-${dayState}${date.slice(0, 7) !== month ? " qfm-out" : ""}${date === today ? " qfm-today" : ""}${date === selected ? " qfm-selected" : ""}`} aria-label={`${marketDate(date)} ${label}`} aria-pressed={selected === date} aria-current={date === today ? "date" : undefined} onClick={() => setSelected(date)}><span className="qfm-day-num">{date.slice(8)}</span><span className="qfm-day-state"><i />{label}</span></button>;
        })}</div>
      </section>
      <MarketCalendarDetail detail={detailSnapshot} date={selected} exchange={exchange} loading={detailLoading} error={detail.error?.message ?? ""} stale={detail.stale} onRetry={() => setRevision(value => value + 1)} />
    </div>
  </dialog>;
}

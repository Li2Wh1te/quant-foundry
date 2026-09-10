import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { DataCollectionApiError, getTradingCalendarDay, listTradingCalendarDays, type TradingCalendarPage } from "../../api/dataCollections";
import { ExchangeFilter } from "./ExchangeFilter";
import { MarketCalendarDetail, type CalendarDetailSnapshot } from "./MarketCalendarDetail";
import { CALENDAR_LABELS, calendarDates, calendarState, exchangeName, marketDate, shanghaiToday, shiftMonth } from "./marketPresentation";

export function MarketCalendar({ onClose, onUnauthorized }: { onClose: () => void; onUnauthorized: () => void }) {
  const today = shanghaiToday();
  const [month, setMonth] = useState(today.slice(0, 7));
  const [selected, setSelected] = useState(today);
  const [exchange, setExchange] = useState("SSE");
  const [revision, setRevision] = useState(0);
  const [days, setDays] = useState<{ key: string; value: TradingCalendarPage } | null>(null);
  const [detail, setDetail] = useState<CalendarDetailSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(true);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const dates = useMemo(() => calendarDates(month), [month]);
  const gridKey = `${exchange}:${month}`;
  const detailKey = `${exchange}:${selected}`;
  const visibleDays = days?.key === gridKey ? days.value : null;
  const dayMap = new Map(visibleDays?.items.map(day => [day.calendar_date, day]));
  const detailPending = detailLoading || (!detailError && detail?.key !== detailKey);

  useEffect(() => {
    const node = dialog.current;
    const previous = document.activeElement as HTMLElement | null;
    // A native modal makes the entire shell inert and contains focus, including
    // when an exchange menu is open. The approved calendar is a browsing exception.
    node?.showModal();
    return () => { node?.close(); requestAnimationFrame(() => { if (previous?.isConnected) previous.focus({ preventScroll: true }); }); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    void listTradingCalendarDays({ exchange, startDate: dates[0], endDate: dates[41], limit: 42 }, controller.signal)
      .then(value => { if (!controller.signal.aborted) setDays({ key: gridKey, value }); })
      .catch(caught => {
        if (controller.signal.aborted) return;
        if (caught instanceof DataCollectionApiError && caught.status === 401) onUnauthorized();
        else setError("交易日历加载失败，请重试。");
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [exchange, dates, gridKey, revision, onUnauthorized]);
  useEffect(() => {
    const controller = new AbortController();
    setDetailLoading(true); setDetailError("");
    void getTradingCalendarDay(exchange, selected, controller.signal)
      .then(value => { if (!controller.signal.aborted) setDetail({ key: detailKey, date: selected, exchange, value }); })
      .catch(caught => {
        if (controller.signal.aborted) return;
        if (caught instanceof DataCollectionApiError && caught.status === 401) onUnauthorized();
        else if (caught instanceof DataCollectionApiError && caught.status === 404) setDetail({ key: detailKey, date: selected, exchange, value: null });
        else setDetailError(`${exchangeName(exchange)} ${marketDate(selected)} 的日期详情加载失败，请重试。`);
      }).finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [exchange, selected, detailKey, revision, onUnauthorized]);

  return <dialog ref={dialog} className="qfm-calendar-dialog" aria-labelledby="qfm-calendar-title" onCancel={event => { event.preventDefault(); onClose(); }} onKeyDown={event => {
    if (event.key !== "Tab") return;
    // Native modal inertness blocks background controls, but browsers can
    // still move to their chrome at the boundary. Match the approved loop.
    const controls = [...event.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled),a[href],input,select,[tabindex]")]
      .filter(element => element.tabIndex >= 0 && element.getClientRects().length > 0);
    const next = controls.indexOf(document.activeElement as HTMLElement) + (event.shiftKey ? -1 : 1);
    if (next < 0 || next >= controls.length) {
      event.preventDefault(); controls[(next + controls.length) % controls.length]?.focus();
    }
  }} onClick={event => {
    // Ignore inside padding and only dismiss real backdrop clicks.
    if (event.target !== event.currentTarget) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
  }}>
    <header className="qfm-calendar-head"><div><div className="qfm-dialog-kicker">MARKET CALENDAR</div><h2 id="qfm-calendar-title">交易日历</h2></div><button className="qfo-icon-btn" type="button" aria-label="关闭交易日历" title="关闭交易日历" autoFocus onClick={onClose}><X aria-hidden="true" /></button></header>
    <div className="qfm-calendar-toolbar"><ExchangeFilter value={exchange} onChange={setExchange} /><div className="qfm-month-nav" aria-label="月份切换"><button type="button" aria-label="上个月" title="上个月" disabled={month <= "1900-01"} onClick={() => setMonth(value => shiftMonth(value, -1))}><ChevronLeft aria-hidden="true" /></button><span className="qfm-month-label" aria-live="polite">{month.replace("-", " / ")}</span><button type="button" aria-label="下个月" title="下个月" disabled={month >= "2199-12"} onClick={() => setMonth(value => shiftMonth(value, 1))}><ChevronRight aria-hidden="true" /></button></div><div className="qfm-legend"><span><i />交易日</span><span><i className="qfm-closed" />休市</span><span><i className="qfm-today" />当前日期</span></div></div>
    <div className="qfm-calendar-body">
      <section className="qfm-calendar-main" aria-label={`${exchangeName(exchange)} ${month} 月历`} aria-busy={loading}>
        {error && <div className="qfm-error" role="alert">{error}{visibleDays && " 保留本月上次数据。"}<button type="button" className="qfm-text-button" onClick={() => setRevision(value => value + 1)}>重试</button></div>}
        <div className="qfm-weekdays">{["周一", "周二", "周三", "周四", "周五", "周六", "周日"].map(day => <span key={day}>{day}</span>)}</div>
        <div className="qfm-calendar-grid">{dates.map(date => {
          const dayState = calendarState(dayMap.get(date));
          const label = !visibleDays ? loading ? "加载中" : error ? "不可用" : "未采集" : CALENDAR_LABELS[dayState];
          return <button type="button" key={date} className={`qfm-day qfm-${dayState}${date.slice(0, 7) !== month ? " qfm-out" : ""}${date === today ? " qfm-today" : ""}${date === selected ? " qfm-selected" : ""}`} aria-label={`${marketDate(date)} ${label}`} aria-pressed={selected === date} aria-current={date === today ? "date" : undefined} onClick={() => setSelected(date)}><span className="qfm-day-num">{date.slice(8)}</span><span className="qfm-day-state"><i />{label}</span></button>;
        })}</div>
      </section>
      <MarketCalendarDetail detail={detail} date={selected} exchange={exchange} loading={detailPending} error={detailError} onRetry={() => setRevision(value => value + 1)} />
    </div>
  </dialog>;
}

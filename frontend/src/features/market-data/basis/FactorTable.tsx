import { useEffect, useRef, useState } from "react";
import { exactMarketValue, isCancellation, type FactorPage, type MarketReadState } from "../data";
import type { FactorTableProps } from "../series/basisSlot";
import { createFactorWindow, defaultFactorStart } from "./window";
import "./basis.css";

export function FactorTable({ instrumentKey, seriesKey, start, end, asOf, adjustment, page, onError }: FactorTableProps) {
  const initialStart = start ?? page?.items[0]?.first_trade_date ?? defaultFactorStart(end);
  const [range, setRange] = useState({ start: initialStart, end }), [draft, setDraft] = useState({ start: initialStart, end });
  const [revision, setRevision] = useState(0), [rangeError, setRangeError] = useState("");
  const [read, setRead] = useState<MarketReadState<FactorPage>>({ data: null, error: null, pending: false, stale: false, canContinue: false });
  const active = useRef<ReturnType<typeof createFactorWindow> | null>(null);
  const [condition, setCondition] = useState("");
  const identityCondition = JSON.stringify({ instrumentKey, seriesKey, start, end, asOf, adjustment });
  useEffect(() => {
    const next = { start: initialStart, end };
    setRange(next); setDraft(next); setRangeError("");
  }, [instrumentKey, seriesKey, start, end, asOf, adjustment]);
  useEffect(() => {
    const window = createFactorWindow(); active.current = window;
    const stop = window.session.subscribe(setRead);
    return () => { active.current = null; stop(); window.dispose(); };
  }, []);
  useEffect(() => {
    const key = JSON.stringify({ identityCondition, range, revision }); setCondition(key);
    void active.current?.load({ instrument_key: instrumentKey, series_key: seriesKey,
      start: range.start, end: range.end, as_of: asOf, limit: 512 }, adjustment).catch(error => { if (!isCancellation(error)) onError(error); });
  }, [identityCondition, range.start, range.end, revision, onError]);
  const matches = condition === JSON.stringify({ identityCondition, range, revision });
  const data = matches ? read.data : null;
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!draft.start || !draft.end || draft.start > draft.end || draft.end > asOf) {
      setRangeError("请选择有序日期，结束日不得晚于固定截止。"); return;
    }
    setRangeError(""); setRange({ ...draft });
  };
  return <section className="qfb-factors" aria-label="复权因子原值" aria-busy={read.pending}>
    <h2>复权因子原值</h2>
    <p>原值表独立读取，不在浏览器计算复权。金额与成交量保留获准原单位。</p>
    <form onSubmit={submit}><label>开始<input type="date" value={draft.start} max={asOf} onChange={e => setDraft({ ...draft, start: e.target.value })} /></label>
      <label>结束<input type="date" value={draft.end} max={asOf} onChange={e => setDraft({ ...draft, end: e.target.value })} /></label>
      <button type="submit" disabled={read.pending}>查看日期范围</button></form>
    {rangeError && <p role="alert">{rangeError}</p>}
    {read.error && <p role="alert">{read.error.message}{read.stale && " 当前显示同条件旧因子，不能继续分页。"}</p>}
    {read.pending && <p role="status">正在读取因子…</p>}
    {data && <>
      <p>{data.basis.note}</p>
      <dl><dt>固定截止</dt><dd>{data.basis.as_of}</dd><dt>基准</dt><dd>{data.basis.anchor ? `${data.basis.anchor.date} · ${data.basis.anchor.factor}` : "未核验"}</dd>
        <dt>业务截至</dt><dd>{data.meta.business_as_of ?? "未知"}</dd><dt>来源</dt><dd>{data.meta.source_labels.join(" · ")}</dd></dl>
      <div className="qfb-table-scroll" tabIndex={0}><table><thead><tr><th scope="col">业务日期</th><th scope="col">精确因子原值</th></tr></thead>
        <tbody>{data.items.map(row => <tr key={row.trade_date}><td>{row.trade_date}</td><td>{exactMarketValue(row.factor)}</td></tr>)}</tbody></table></div>
      {!data.items.length && <p>该有界日期范围未读取到因子；不推断完整覆盖。</p>}
      <p>{data.items.length} / 10,000 条 · {data.meta.coverage.note}</p>
    </>}
    <div className="qfb-factor-actions"><button type="button" disabled={read.pending} onClick={() => setRevision(v => v + 1)}>重新读取因子</button>
      <button type="button" disabled={!data?.next_cursor || !read.canContinue || read.pending}
        onClick={() => { void active.current?.earlier().catch(error => { if (!isCancellation(error)) onError(error); }); }}>加载更早因子</button></div>
    {page?.basis.anchor && <p>图表固定基准：{page.basis.anchor.date} · {page.basis.anchor.factor}。此独立原值表不替换图表的基准。</p>}
  </section>;
}

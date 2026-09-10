import { Check, ChevronDown } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { EXCHANGES } from "./marketPresentation";

/** Reuse the approved menu's keyboard model: exploring options does not
 * commit a filter until Enter, Space, or a pointer click selects one. */
export function ExchangeFilter({ value, onChange, allowAll = false }: { value: string; onChange: (value: string) => void; allowAll?: boolean }) {
  const options = allowAll ? [{ key: "", name: "全部交易所" }, ...EXCHANGES] : EXCHANGES;
  const selected = Math.max(0, options.findIndex(option => option.key === value));
  const [open, setOpen] = useState(false);
  const [focused, setFocused] = useState(selected);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const id = useId();
  function close(restore = false) { setOpen(false); if (restore) trigger.current?.focus(); }
  function show() { setFocused(selected); setOpen(true); }
  useEffect(() => { if (open) items.current[focused]?.focus(); }, [open, focused]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return <div className="qfm-filter" ref={root} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false); }}>
    <button ref={trigger} className="qfm-filter-trigger" type="button" aria-label={`筛选交易所：${options[selected].name}`} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined}
      onClick={() => open ? close() : show()} onKeyDown={event => {
        if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); show(); }
      }}><span><span className="qfm-filter-prefix">交易所</span> {value ? options[selected].name : "全部"}</span><ChevronDown aria-hidden="true" /></button>
    {open && <div className="qfm-filter-menu" id={id} role="listbox" aria-label="交易所筛选" onKeyDown={event => {
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault(); setFocused(current => event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length);
      } else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); }
      else if (event.key === "Tab") close(true);
    }}>{options.map((option, index) => <button key={option.key} ref={node => { items.current[index] = node; }} type="button" className="qfm-filter-option" role="option" aria-selected={value === option.key} tabIndex={focused === index ? 0 : -1}
      onFocus={() => setFocused(index)} onClick={() => { onChange(option.key); close(true); }}><Check aria-hidden="true" /><span>{option.name}</span></button>)}</div>}
  </div>;
}

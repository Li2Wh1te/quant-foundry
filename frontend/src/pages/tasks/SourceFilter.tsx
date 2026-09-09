import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

/** Match the approved custom source menu while keeping selection and keyboard
 * focus separate: arrow keys explore, Enter/Space commit, and Escape cancels. */
export function SourceFilter({ sources, value, onChange }: {
  sources: { key: string; name: string }[]; value: string; onChange: (key: string) => void;
}) {
  const options = [{ key: "", name: "全部数据源" }, ...sources];
  const [open, setOpen] = useState(false);
  const [focused, setFocused] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();
  const selected = Math.max(0, options.findIndex(option => option.key === value));
  function show() { setFocused(selected); setOpen(true); }
  function close(restore = false) { setOpen(false); if (restore) trigger.current?.focus(); }
  useEffect(() => { if (open) items.current[focused]?.focus(); }, [open, focused]);
  useEffect(() => {
    if (!open) return;
    // Outside pointer actions retain their own focus; keyboard cancellation
    // explicitly returns focus to the trigger instead.
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return <div className="qft-source-filter" ref={root} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }}>
    <button ref={trigger} className="qft-source-trigger" type="button" aria-label={`筛选数据源：${value ? options[selected].name : "全部"}`} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? menuId : undefined}
      onClick={() => open ? close() : show()} onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); show(); }
        if (event.key === "Escape") { event.preventDefault(); close(true); }
      }}><span><span className="qft-filter-prefix">数据源</span>{value ? options[selected].name : "全部"}</span><ChevronDown aria-hidden="true" /></button>
    {open && <div id={menuId} className="qft-source-menu" role="listbox" aria-label="数据源筛选" onKeyDown={event => {
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        setFocused(current => event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
          : (current + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length);
      } else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); }
      else if (event.key === "Tab") close(true);
    }}>{options.map((option, index) => <button key={option.key} ref={node => { items.current[index] = node; }} className="qft-source-option" type="button" role="option" aria-selected={value === option.key} tabIndex={focused === index ? 0 : -1}
      onFocus={() => setFocused(index)} onClick={() => { onChange(option.key); close(true); }}><Check aria-hidden="true" /><span>{option.name}</span></button>)}</div>}
  </div>;
}

import { Children, isValidElement, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Check, ChevronDown, Search } from "lucide-react";
import { useControlPopover } from "./Popover";
import "./controls.css";
type Option = { value: string; label: string; disabled: boolean };
function text(node: ReactNode): string { return Children.toArray(node).map(child => isValidElement<{children?:ReactNode}>(child) ? text(child.props.children) : String(child)).join(""); }
/** A themed select with the same value-change shape as the former native field. */
export function Select({ value, onChange, children, disabled, "aria-label": ariaLabel }: { value: string; onChange: (event: { target: { value: string } }) => void; children: ReactNode; disabled?: boolean; "aria-label"?: string }) {
  const options: Option[] = Children.toArray(children).filter(isValidElement).map(child => { const p = child.props as { value?: unknown; children?: ReactNode; disabled?: boolean }; return { value: String(p.value ?? text(p.children)), label: text(p.children), disabled: !!p.disabled }; });
  const [open,setOpen]=useState(false),[search,setSearch]=useState(""),[active,setActive]=useState(0),[label,setLabel]=useState(ariaLabel || "选择选项");
  const id=useId(),button=useRef<HTMLButtonElement>(null),input=useRef<HTMLInputElement>(null);
  const {root,style}=useControlPopover(open,()=>setOpen(false));
  const filtered=options.filter(o=>o.label.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  useEffect(()=>{if(ariaLabel) setLabel(ariaLabel); else { const parent=root.current?.closest("label");const caption=parent && Array.from(parent.childNodes).filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join("").trim();if(caption)setLabel(caption); }},[ariaLabel]);
  useEffect(()=>{if(open){setSearch("");setActive(Math.max(0,options.findIndex(o=>o.value===value)));if(options.length>8)input.current?.focus();}},[open]);
  useEffect(()=>{if(open)document.getElementById(`${id}-${active}`)?.scrollIntoView({block:"nearest"});},[active]);
  useEffect(()=>{if(disabled)setOpen(false);},[disabled]);
  function choose(option:Option){if(option.disabled)return;onChange({target:{value:option.value}});setOpen(false);button.current?.focus();}
  function key(event:React.KeyboardEvent){
    if(event.key==='Escape'&&open){event.preventDefault();event.stopPropagation();setOpen(false);button.current?.focus();return;}
    if(event.key==='Tab'){setOpen(false);return;}
    if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();if(!open){setOpen(true);return;}const enabled=filtered.map((o,i)=>o.disabled?-1:i).filter(i=>i>=0);if(!enabled.length)return;const next=event.key==='Home'?enabled[0]:event.key==='End'?enabled.at(-1)!:enabled[(enabled.indexOf(active)+(event.key==='ArrowDown'?1:-1)+enabled.length)%enabled.length];setActive(next);}
    if(open&&event.key==='Enter'){event.preventDefault();if(filtered[active])choose(filtered[active]);}
  }
  return <span className="qfc-control" ref={root} onKeyDown={key} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget))setOpen(false);}}>
    <button ref={button} type="button" className="qfc-trigger" role="combobox" aria-label={label} aria-expanded={open} aria-controls={open?id:undefined} aria-haspopup="listbox" aria-activedescendant={open?`${id}-${active}`:undefined} disabled={disabled} onClick={()=>setOpen(o=>!o)}><span>{options.find(o=>o.value===value)?.label || "请选择"}</span><ChevronDown size={16} aria-hidden="true"/></button>
    {open&&<span className="qfc-popover" style={style}>
      {options.length>8&&<span className="qfc-search"><Search size={15} aria-hidden="true"/><input ref={input} aria-label={`搜索${label}`} value={search} onChange={e=>{setSearch(e.target.value);setActive(0);}} placeholder="搜索选项"/></span>}
      <span id={id} role="listbox" aria-label={label} className="qfc-options">{filtered.map((option,index)=><button id={`${id}-${index}`} key={`${option.value}-${index}`} type="button" role="option" aria-selected={option.value===value} disabled={option.disabled} className={index===active?'is-active':''} onMouseDown={e=>e.preventDefault()} onMouseMove={()=>setActive(index)} onClick={()=>choose(option)}><span>{option.label}</span>{option.value===value&&<Check size={15} aria-hidden="true"/>}</button>)}{!filtered.length&&<span className="qfc-no-options">没有匹配的选项</span>}</span>
    </span>}
  </span>;
}

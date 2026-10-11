import { useRef } from "react";
import type { DataAssetsView } from "../navigation";

const tabs: { value: DataAssetsView; label: string }[] = [
  { value: "overview", label: "概览" },
  { value: "preview", label: "数据预览" },
  { value: "updates", label: "更新与问题" }
];

interface DatasetTabsProps {
  value: DataAssetsView;
  onChange: (view: DataAssetsView) => void;
}

/** Same-object views with roving focus and the standard tab keyboard behavior. */
export function DatasetTabs({ value, onChange }: DatasetTabsProps) {
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  return <div className="qf-assets-tabs" role="tablist" aria-label="数据集视图">
    {tabs.map((tab, index) => <button key={tab.value} ref={node => { buttons.current[index] = node; }}
      type="button" role="tab" id={`qf-assets-tab-${tab.value}`}
      aria-controls={`qf-assets-panel-${tab.value}`} aria-selected={value === tab.value}
      tabIndex={value === tab.value ? 0 : -1} onClick={() => onChange(tab.value)}
      onKeyDown={event => {
        const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
          : direction ? (index + direction + tabs.length) % tabs.length : null;
        if (next === null) return;
        event.preventDefault();
        onChange(tabs[next].value);
        buttons.current[next]?.focus();
      }}>
      {tab.label}
    </button>)}
  </div>;
}

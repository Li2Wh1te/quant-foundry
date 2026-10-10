import type { CurrentDataset } from "../../api/dataStore";
import { DataSheet } from "./components";
import { FieldTable } from "./FieldTable";
import { count, frequencies, statusNames, time } from "./legacyPresentation";

export interface DatasetViewProps { dataset: CurrentDataset; }

/** D04 can replace this overview without editing the route or detail container. */
export function DatasetView({ dataset }: DatasetViewProps) {
  return <>
      <div className="qf-assets-metrics">
        <div><span>当前状态</span><strong className="qf-assets-metric-label">{statusNames[dataset.status] ?? "状态未知"}</strong></div>
        <div><span>当前记录</span><strong>{count(dataset.row_count)}</strong></div>
        <div><span>当前代次</span><strong>{count(dataset.generation)}</strong></div>
        <div><span>未解决问题</span><strong>{count(dataset.issues)}</strong></div>
      </div>
        <DataSheet title="当前数据">
          <dl className="qf-assets-properties">
            <div><dt>数据来源</dt><dd>{dataset.source}</dd></div>
            <div><dt>频率</dt><dd>{frequencies[dataset.frequency] ?? "未声明"}</dd></div>
            <div><dt>存储口径</dt><dd>类型化对象节点 · {dataset.representation}</dd></div>
            <div><dt>当前分区</dt><dd>{dataset.partition_range.from ?? "未检查"}{dataset.partition_range.to && ` ～ ${dataset.partition_range.to}`}</dd></div>
            <div><dt>最近提交</dt><dd>{time(dataset.updated_at)}</dd></div>
          </dl>
          <p className="qf-assets-asof">分区范围是存储范围，不证明每个业务日期均有数据。Schema {dataset.schema_id}。</p>
          {dataset.status === "empty" && <p className="qf-assets-empty">当前数据集已登记，尚无正式记录。</p>}
          {dataset.status === "not_checked" && <p className="qf-assets-empty">本地来源尚未检查，不能将未检查视为空。</p>}
        </DataSheet>
      <FieldTable fields={dataset.fields} />
  </>;
}

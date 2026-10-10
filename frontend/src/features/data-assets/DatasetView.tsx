import { useState } from "react";
import { datasetPresentation, type CurrentDataset } from "./data";
import { DataSheet, LocalNotice } from "./components";
import { FieldTable } from "./FieldTable";
import "./DatasetDetails.css";

export interface DatasetViewProps { dataset: CurrentDataset; }

/** The D02 container owns fetching and cancellation. Key local metadata controls
 * as well, so even a direct prop change cannot retain another object's inputs. */
export function DatasetView({ dataset }: DatasetViewProps) {
  return <DatasetOverview key={dataset.dataset} dataset={dataset} />;
}

function DatasetOverview({ dataset }: DatasetViewProps) {
  const display = datasetPresentation(dataset);
  const technicalLimitations = dataset.limitations.filter(value => /^[\w.[\]\/-]+$/.test(value.trim()));
  const describedLimitations = dataset.limitations.filter(value => !technicalLimitations.includes(value));
  const [copyFeedback, setCopyFeedback] = useState<{ text: string; failed: boolean } | null>(null);
  const declared = (value: string | null | undefined) => value?.trim() ? value : "未声明";

  async function copy(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyFeedback({ text: `已复制${label}。`, failed: false });
    } catch {
      setCopyFeedback({ text: "复制失败，可选中技术值后手动复制。", failed: true });
    }
  }

  function technicalValue(label: string, value: string | null | undefined) {
    return <div key={label}><dt>{label}</dt><dd>
      <code>{declared(value)}</code>
      {value?.trim() && <button className="qf-dataset-copy" type="button"
        aria-label={`复制${label}`} onClick={() => void copy(label, value)}>复制</button>}
    </dd></div>;
  }

  return <div className="qf-dataset-details">
    <DataSheet title="能提供什么" className="qf-dataset-purpose">
      <p className="qf-dataset-name">{display.name}</p>
      <dl className="qf-assets-properties">
        <div><dt>用途</dt><dd>未声明；当前接口未提供独立的用途说明。可在下方查看已登记字段与能力限制。</dd></div>
        <div><dt>数据来源</dt><dd>{declared(display.source)}</dd></div>
        <div><dt>频率</dt><dd>{display.frequency}</dd></div>
        <div><dt>目录/记录更新时间</dt><dd>{display.updatedAt}<span className="qf-dataset-note">该时间不是业务数据截至日。</span></dd></div>
      </dl>
    </DataSheet>

    <div className="qf-assets-metrics qf-dataset-metrics" aria-label="当前数据口径">
      <div><span>物理存储行</span><strong className="qf-dataset-count">{display.physicalRowCount}</strong></div>
      <div><span>业务数据截至日</span><strong className="qf-assets-metric-label">未声明</strong></div>
      <div><span>最近更新结果</span><strong className="qf-assets-metric-label">{display.update.label}</strong></div>
    </div>

    <DataSheet title="已知限制与读取边界" className="qf-dataset-boundaries">
      <p><strong>当前状态：{display.current.label}</strong></p>
      <p className="qf-dataset-state-explanation">{stateExplanation(dataset.status)}</p>
      <p>数据集说明可查看时，仍需由服务端判断实际对象与范围的读取权限。可通过页面顶部的“查看当前数据”进入预览。</p>
      <p>单位、币种、价格基准与业务日期完整覆盖未声明。字段的技术类型或 Schema 存在，不代表业务语义已全部确认。</p>
      {describedLimitations.length > 0 && <ul aria-label="服务端声明的限制">{describedLimitations.map((limitation, index) =>
          <li key={index}>{limitation.trim() ? limitation : "此项限制未提供说明。"}</li>)}</ul>
      }
      {technicalLimitations.length > 0 && <p className="qf-dataset-note">另有 {technicalLimitations.length} 项服务端限制仅提供技术标识，业务解释未提供；原始标识可在技术信息中查看。</p>}
      {dataset.limitations.length === 0 && <p className="qf-dataset-note">服务端未提供具体限制说明；这不代表没有限制。</p>}
    </DataSheet>

    <FieldTable fields={dataset.fields} />

    <DataSheet title="技术信息" className="qf-dataset-technical">
      <details>
        <summary>展开定位与存储信息</summary>
        <p className="qf-dataset-note">供定位当前对象与诊断使用。当前代次仅表示读取一致性，不提供历史版本切换。</p>
        <dl className="qf-assets-properties">
          {technicalValue("数据集标识", dataset.dataset)}
          {technicalValue("领域标识", dataset.domain)}
          {technicalValue("Schema 标识", dataset.schema_id)}
          {technicalValue("规则标识", dataset.rule)}
          {technicalValue("当前代次", dataset.generation == null ? null : String(dataset.generation))}
          {technicalValue("存储行结构", display.rowLayout)}
          {technicalValue("业务键字段", dataset.business_key.length ? dataset.business_key.join(", ") : null)}
          {technicalValue("源表示键", dataset.preview_key?.representation)}
          {technicalValue("示例对象标识", dataset.preview_key?.subject)}
          {technicalValue("示例业务键", dataset.preview_key?.object_key)}
          {technicalValue("示例分区", dataset.preview_key?.partition)}
          <div><dt>存储分区范围</dt><dd>
            <span>起点：<code>{declared(dataset.partition_range.from)}</code>；终点：<code>{declared(dataset.partition_range.to)}</code></span>
            <span className="qf-dataset-note">这是存储范围，不证明业务日期连续覆盖。</span>
          </dd></div>
          {technicalValue("分区范围精度", dataset.partition_range.precision)}
          <div><dt>返回的存储分区</dt><dd>
            {dataset.partitions.length ? <code>{dataset.partitions.join(", ")}</code> : "未声明"}
            {dataset.partitions_truncated && <span className="qf-dataset-note">分区列表已截断，不能作为完整范围。</span>}
          </dd></div>
          {technicalLimitations.length > 0 && <div><dt>原始限制标识</dt><dd>
            {technicalLimitations.map((value, index) => <code key={index}>{value}</code>)}
          </dd></div>}
        </dl>
        <p className="qf-dataset-note">源表示键来自服务端示例对象，不等于存储行结构，也不是所有主体的选择器。</p>
        {copyFeedback && <LocalNotice tone={copyFeedback.failed ? "error" : "info"}>{copyFeedback.text}</LocalNotice>}
      </details>
    </DataSheet>
  </div>;
}

/** Explain descriptor facts without turning them into an authorization rule. */
function stateExplanation(status: string): string {
  switch (status) {
    case "not_checked": return "本地来源尚未检查，不能将尚未检查视为空。";
    case "empty": return "当前数据集已登记，当前物理存储为空；不说明来源或市场上没有这种数据。";
    case "restricted": return "当前存在限制；说明可查看不等于所有范围可读取，也不表示所有范围均不可读取。";
    case "rebuild_required": return "服务端声明当前数据需要重建。页面不会执行重建，实际读取结果以服务端响应为准。";
    case "rebuilding": return "当前数据暂不可读取／处理中，说明仍可查看时继续展示；请稍后刷新页面。";
    case "available": return "当前可用是服务端对该数据集的声明，不证明任意请求均完整或允许读取。";
    default: return "当前状态尚未确认，不能据此判断为空、可用或具备实际读取权限。";
  }
}

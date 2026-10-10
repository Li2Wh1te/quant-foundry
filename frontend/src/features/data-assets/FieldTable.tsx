import { useId, useState } from "react";
import type { CurrentDataset, CurrentField } from "./data";
import { DataSheet } from "./components";
import "./DatasetDetails.css";

export interface FieldTableProps { fields: CurrentDataset["fields"]; }
const PAGE_SIZE = 20;

/** Identifier/path metadata is not a business definition. Keep the exact source
 * text available separately instead of translating a model path or f0_ name. */
export function fieldDescription(field: CurrentField) {
  const meaning = field.meaning.trim();
  const technicalMeaning = !meaning || /^[\w.[\]\/-]+$/.test(meaning);
  const exactDecimalText = field.logical_type === "exact_decimal_text" || field.type === "decimal_text";
  return {
    meaning: technicalMeaning ? "未提供业务说明" : field.meaning,
    calculation: field.arithmetic === "unsupported" || exactDecimalText ? "不支持直接计算"
      : field.arithmetic === "type_only_units_require_domain_contract" ? "计算口径需领域契约确认"
      : "计算能力未声明",
    exactDecimalText
  };
}

/** Search only the descriptor already supplied by D02. It never reads business
 * rows, changes permissions or creates another metadata request. */
export function filterFields(fields: CurrentField[], query: string): CurrentField[] {
  const needle = query.trim().toLocaleLowerCase();
  return needle ? fields.filter(field => [field.column, field.meaning, field.type, field.logical_type, field.arithmetic]
    .some(value => value.toLocaleLowerCase().includes(needle))) : fields;
}

export function FieldTable({ fields }: FieldTableProps) {
  const id = useId();
  const [query, setQuery] = useState("");
  const [requestedPage, setRequestedPage] = useState(0);
  const filtered = filterFields(fields, query);
  // A refresh can reduce the declared fields while preserving the same object.
  // Clamp the displayed page synchronously, without showing a false empty table.
  const page = Math.min(requestedPage, Math.max(0, Math.ceil(filtered.length / PAGE_SIZE) - 1));
  const first = page * PAGE_SIZE;
  const visible = filtered.slice(first, first + PAGE_SIZE);

  return <DataSheet title="字段与口径" className="qf-dataset-fields">
    <p id={`${id}-note`} className="qf-dataset-note">字段名称保留技术名。业务含义、技术类型与计算限制分开展示；单位未声明，不能从字段名前缀推断。</p>
    <div className="qf-dataset-field-toolbar">
      <label htmlFor={`${id}-search`}>搜索字段元数据
        <input id={`${id}-search`} type="search" placeholder="字段名、说明或技术类型"
          value={query} aria-describedby={`${id}-note`} onChange={event => {
            setQuery(event.target.value); setRequestedPage(0);
          }} />
      </label>
      {query && <button className="qfo-secondary-btn" type="button" onClick={() => { setQuery(""); setRequestedPage(0); }}>清除筛选</button>}
      <span role="status" className="qf-dataset-note">{filtered.length} / {fields.length} 个字段元数据</span>
    </div>
    {visible.length ? <>
      <div className="qf-assets-scroll" tabIndex={0} role="region" aria-label="数据集字段表格">
        <table><thead><tr><th scope="col">字段</th><th scope="col">已声明含义</th><th scope="col">技术类型</th><th scope="col">计算限制</th></tr></thead>
          <tbody>{visible.map((field, index) => {
            const description = fieldDescription(field);
            return <tr key={`${field.column}-${first + index}`}>
              <th scope="row"><code>{field.column}</code></th>
              <td>{description.meaning}
                <details><summary>技术元数据</summary>
                  <dl>
                    <div><dt>原始说明／路径</dt><dd><code>{field.meaning.trim() ? field.meaning : "未声明"}</code></dd></div>
                    <div><dt>逻辑类型</dt><dd><code>{field.logical_type.trim() ? field.logical_type : "未声明"}</code></dd></div>
                    <div><dt>计算声明</dt><dd><code>{field.arithmetic.trim() ? field.arithmetic : "未声明"}</code></dd></div>
                  </dl>
                </details>
              </td>
              <td><code>{field.type}</code>{description.exactDecimalText && <span className="qf-dataset-note">精确十进制文本；保留文本精度。</span>}</td>
              <td>{description.calculation}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {filtered.length > PAGE_SIZE && <div className="qf-assets-pager qf-dataset-field-pager" aria-label="字段元数据分页">
        <span>字段 {first + 1}–{Math.min(first + PAGE_SIZE, filtered.length)} / {filtered.length}</span>
        <button className="qfo-secondary-btn" type="button" disabled={page === 0}
          onClick={() => setRequestedPage(page - 1)}>上一页字段</button>
        <button className="qfo-secondary-btn" type="button" disabled={first + PAGE_SIZE >= filtered.length}
          onClick={() => setRequestedPage(page + 1)}>下一页字段</button>
      </div>}
    </> : <p className="qf-assets-empty">{fields.length ? "没有匹配的字段元数据。" : "接口未声明字段说明；不能据此判断数据集为空。"}</p>}
  </DataSheet>;
}

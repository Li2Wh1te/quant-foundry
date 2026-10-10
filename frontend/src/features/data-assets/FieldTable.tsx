import type { CurrentDataset } from "./data";
import { DataSheet } from "./components";

/** D04 owns field content; the shared shell only supplies the surrounding view. */
export function FieldTable({ fields }: { fields: CurrentDataset["fields"] }) {
  return <DataSheet title="字段与口径">
        <div className="qf-assets-scroll" tabIndex={0} role="region" aria-label="数据集字段表格"><table><thead><tr><th>字段</th><th>类型</th><th>字段含义</th><th>计算能力</th></tr></thead>
          <tbody>{fields.map(field => <tr key={field.column}>
            <td><code>{field.column}</code></td><td>{field.type}</td>
            <td>{field.meaning}</td><td>{field.arithmetic === "unsupported" ? "不支持直接计算" : "以领域契约为准"}</td>
          </tr>)}</tbody>
        </table></div>
      </DataSheet>;
}

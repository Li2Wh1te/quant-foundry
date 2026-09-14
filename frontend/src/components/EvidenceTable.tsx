const text = (value: unknown) => value === undefined || value === null ? "未记录" : typeof value === "object" ? JSON.stringify(value) : String(value);

export function EvidenceTable({ title, value }: { title: string; value: unknown }) {
  const rows: [string, unknown][] = [];
  const flatten = (item: unknown, path: string) => {
    if (item && typeof item === "object" && !Array.isArray(item) && Object.keys(item).length) {
      Object.entries(item).forEach(([key, child]) => flatten(child, path ? `${path} / ${key}` : key));
    } else rows.push([path, item]);
  };
  flatten(value, "");
  return <details><summary>{title}</summary><table><tbody>{rows.map(([key, item]) => <tr key={key}><th scope="row">{key || title}</th><td style={{ overflowWrap: "anywhere" }}>{text(item)}</td></tr>)}</tbody></table></details>;
}

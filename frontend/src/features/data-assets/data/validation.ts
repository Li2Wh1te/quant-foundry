import { invalidRequest, invalidResponse } from "./errors";
import type {
  CurrentDataset, CurrentEntryStatus, CurrentField, CurrentIssue, CurrentUpdate,
  CurrentValue, DatasetList, IssueList, PreviewRequest, PreviewResult, StatusList
} from "./types";

type ObjectValue = Record<string, unknown>;
function object(value: unknown, at: string): ObjectValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidResponse(at);
  return value as ObjectValue;
}
function array(value: unknown, at: string): unknown[] {
  if (!Array.isArray(value)) invalidResponse(at);
  return value;
}
function text(value: unknown, at: string, required = false): string {
  if (typeof value !== "string" || (required && !value.trim())) invalidResponse(at);
  return value;
}
function nullableText(value: unknown, at: string): string | null {
  return value == null ? null : text(value, at);
}
function boolean(value: unknown, at: string): boolean {
  if (typeof value !== "boolean") invalidResponse(at);
  return value;
}
function count(value: unknown, at: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) invalidResponse(at);
  return value;
}
function nullableCount(value: unknown, at: string): number | null {
  return value == null ? null : count(value, at);
}
function strings(value: unknown, at: string): string[] {
  return array(value, at).map((item, i) => text(item, `${at}[${i}]`));
}
function state(value: unknown, at: string): string {
  if (value == null || value === "") return "unknown";
  return text(value, at);
}
function cursor(value: unknown, at: string): string | null {
  if (value === null) return null;
  const result = text(value, at, true);
  if (result.length > 8192) invalidResponse(at);
  return result;
}
function offset(value: unknown, at: string): number | null {
  return value === null ? null : count(value, at);
}
function range(value: unknown, at: string): { from: string | null; to: string | null } {
  const raw = object(value, at);
  return { from: nullableText(raw.from, `${at}.from`), to: nullableText(raw.to, `${at}.to`) };
}

function update(value: unknown, at: string): CurrentUpdate {
  const raw = object(value, at);
  return {
    state: state(raw.state, `${at}.state`),
    complete: boolean(raw.complete, `${at}.complete`),
    qualified: boolean(raw.qualified, `${at}.qualified`),
    reason: nullableText(raw.reason, `${at}.reason`),
    source_rows: nullableCount(raw.source_rows, `${at}.source_rows`),
    committed_partitions: nullableCount(raw.committed_partitions, `${at}.committed_partitions`),
    updated_at: nullableText(raw.updated_at, `${at}.updated_at`)
  };
}
function field(value: unknown, at: string): CurrentField {
  const raw = object(value, at);
  return {
    column: text(raw.column, `${at}.column`, true), type: text(raw.type, `${at}.type`, true),
    meaning: text(raw.meaning, `${at}.meaning`), logical_type: text(raw.logical_type, `${at}.logical_type`),
    arithmetic: text(raw.arithmetic, `${at}.arithmetic`)
  };
}

/** Reconstruct known fields after checking them; additive server fields are ignored. */
export function parseDataset(value: unknown, at = "dataset"): CurrentDataset {
  const raw = object(value, at);
  const partitionRange = object(raw.partition_range, `${at}.partition_range`);
  const preview = raw.preview_key == null ? null : object(raw.preview_key, `${at}.preview_key`);
  return {
    dataset: text(raw.dataset, `${at}.dataset`, true), name: text(raw.name, `${at}.name`, true),
    domain: text(raw.domain, `${at}.domain`, true), source: text(raw.source, `${at}.source`, true),
    frequency: state(raw.frequency, `${at}.frequency`),
    representation: text(raw.representation, `${at}.representation`, true),
    schema_id: text(raw.schema_id, `${at}.schema_id`, true), rule: text(raw.rule, `${at}.rule`, true),
    fields: array(raw.fields, `${at}.fields`).map((item, i) => field(item, `${at}.fields[${i}]`)),
    limitations: strings(raw.limitations, `${at}.limitations`),
    business_key: strings(raw.business_key, `${at}.business_key`),
    status: state(raw.status, `${at}.status`),
    row_count: nullableCount(raw.row_count, `${at}.row_count`),
    generation: nullableCount(raw.generation, `${at}.generation`),
    updated_at: nullableText(raw.updated_at, `${at}.updated_at`),
    issues: nullableCount(raw.issues, `${at}.issues`),
    ...(raw.legacy_restrictions === undefined ? {} : {
      legacy_restrictions: count(raw.legacy_restrictions, `${at}.legacy_restrictions`)
    }),
    last_update: raw.last_update == null ? null : update(raw.last_update, `${at}.last_update`),
    partitions: strings(raw.partitions, `${at}.partitions`),
    partitions_truncated: boolean(raw.partitions_truncated, `${at}.partitions_truncated`),
    partition_range: { ...range(partitionRange, `${at}.partition_range`),
      precision: state(partitionRange.precision, `${at}.partition_range.precision`) },
    preview_key: preview && {
      representation: text(preview.representation, `${at}.preview_key.representation`, true),
      subject: text(preview.subject, `${at}.preview_key.subject`, true),
      object_key: text(preview.object_key, `${at}.preview_key.object_key`, true),
      partition: text(preview.partition, `${at}.preview_key.partition`, true)
    }
  };
}

function list(value: unknown, at: string) {
  const raw = object(value, at);
  const items = array(raw.items, `${at}.items`);
  const total = count(raw.total, `${at}.total`);
  if (items.length > total) invalidResponse(`${at}.total`);
  return { raw, items, total, next_offset: offset(raw.next_offset, `${at}.next_offset`) };
}
export function parseDatasetList(value: unknown): DatasetList {
  const { raw, items, total, next_offset } = list(value, "datasets");
  return { items: items.map((item, i) => parseDataset(item, `datasets.items[${i}]`)),
    total, next_offset, phase: state(raw.phase, "datasets.phase") };
}
export function parseStatusList(value: unknown): StatusList {
  const { raw, items, total, next_offset } = list(value, "status");
  return {
    total, next_offset, phase: state(raw.phase, "status.phase"), code: state(raw.code, "status.code"),
    fresh_install: boolean(raw.fresh_install, "status.fresh_install"),
    items: items.map((value, i): CurrentEntryStatus => {
      const at = `status.items[${i}]`, item = object(value, at);
      return { ...update(item, at), entry_id: text(item.entry_id, `${at}.entry_id`, true),
        dataset: nullableText(item.dataset, `${at}.dataset`), name: text(item.name, `${at}.name`, true),
        classification: text(item.classification, `${at}.classification`, true) };
    })
  };
}
export function parseIssueList(value: unknown): IssueList {
  const { raw, items, total, next_offset } = list(value, "issues");
  return { total, next_offset, affected_objects: nullableCount(raw.affected_objects, "issues.affected_objects"),
    items: items.map((value, i): CurrentIssue => {
      const at = `issues.items[${i}]`, item = object(value, at);
      return { dataset: nullableText(item.dataset, `${at}.dataset`), scope_key: nullableText(item.scope_key, `${at}.scope_key`),
        reason: state(item.reason, `${at}.reason`), kind: state(item.kind, `${at}.kind`),
        updated_at: nullableText(item.updated_at, `${at}.updated_at`),
        affected_objects: nullableCount(item.affected_objects, `${at}.affected_objects`) };
    }) };
}
export function parsePreview(value: unknown): PreviewResult {
  const raw = object(value, "preview");
  const rows = array(raw.rows, "preview.rows");
  if (rows.length > 100) invalidResponse("preview.rows");
  const semantics = raw.semantics === undefined ? undefined : object(raw.semantics, "preview.semantics");
  return {
    dataset: text(raw.dataset, "preview.dataset", true), status: state(raw.status, "preview.status"),
    request_satisfied: boolean(raw.request_satisfied, "preview.request_satisfied"),
    business_date_coverage_verified: boolean(raw.business_date_coverage_verified, "preview.business_date_coverage_verified"),
    ...(raw.partial_requested === undefined ? {} : { partial_requested: boolean(raw.partial_requested, "preview.partial_requested") }),
    rows: rows.map((row, i) => Object.fromEntries(Object.entries(object(row, `preview.rows[${i}]`)).map(([key, value]) => {
      if (value !== null && typeof value !== "string" && typeof value !== "boolean"
        && !(typeof value === "number" && Number.isFinite(value)
          && Math.abs(value) <= Number.MAX_SAFE_INTEGER && (!Number.isInteger(value) || Number.isSafeInteger(value)))) {
        invalidResponse(`preview.rows[${i}].${key}`);
      }
      return [key, value as CurrentValue];
    }))),
    next_cursor: cursor(raw.next_cursor, "preview.next_cursor"),
    generation: nullableCount(raw.generation, "preview.generation"), actual_range: range(raw.actual_range, "preview.actual_range"),
    selected_partitions: strings(raw.selected_partitions, "preview.selected_partitions"),
    limitations: strings(raw.limitations, "preview.limitations"),
    ...(raw.unresolved_issues === undefined ? {} : { unresolved_issues: count(raw.unresolved_issues, "preview.unresolved_issues") }),
    ...(raw.schema_id === undefined ? {} : { schema_id: text(raw.schema_id, "preview.schema_id", true) }),
    ...(semantics === undefined ? {} : { semantics: Object.fromEntries(Object.entries(semantics).map(([key, val]) => [key, text(val, `preview.semantics.${key}`)])) })
  };
}

/** Preserve exact numeric tokens before JSON.parse can round them. Strings stay untouched. */
export function parseExactJson(source: string): unknown {
  return JSON.parse(source.replace(/"(?:\\.|[^"\\])*"|(-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/g, (token, numeric: string | undefined) => {
    if (!numeric) return token;
    const unsigned = numeric.startsWith("-") ? numeric.slice(1) : numeric;
    const unsafeInteger = unsigned.length > 16 || (unsigned.length === 16 && unsigned > "9007199254740991");
    return /[.eE]/.test(numeric) || unsafeInteger ? JSON.stringify(numeric) : token;
  }));
}

export function validatePreviewRequest(value: PreviewRequest): PreviewRequest {
  if (!value || typeof value !== "object") invalidRequest("query");
  for (const key of ["dataset", "frequency", "representation", "subject", "from_key", "to_key"] as const) {
    if (typeof value[key] !== "string" || !value[key].trim() || value[key].length > 256) invalidRequest(`query.${key}`);
  }
  if (value.from_key > value.to_key) invalidRequest("query.from_key");
  if (!Array.isArray(value.columns) || value.columns.length > 32
    || value.columns.some(column => typeof column !== "string" || !column.trim() || column.length > 256)
    || new Set(value.columns).size !== value.columns.length) invalidRequest("query.columns");
  if (!Number.isInteger(value.page_size) || value.page_size < 1 || value.page_size > 100) invalidRequest("query.page_size");
  // Partial reads are not a promise of a safe subset. This batch always requests qualified reads.
  if (value.allow_partial !== false) invalidRequest("query.allow_partial");
  if (value.cursor != null && (typeof value.cursor !== "string" || !value.cursor.length || value.cursor.length > 8192)) invalidRequest("query.cursor");
  if (value.partitions != null && (!Array.isArray(value.partitions) || value.partitions.length > 64
    || value.partitions.some(partition => typeof partition !== "string" || !partition.trim())
    || new Set(value.partitions).size !== value.partitions.length)) invalidRequest("query.partitions");
  return { dataset: value.dataset, frequency: value.frequency, representation: value.representation,
    subject: value.subject, from_key: value.from_key, to_key: value.to_key, columns: [...value.columns],
    page_size: value.page_size, cursor: value.cursor ?? null, allow_partial: false,
    ...(value.partitions == null ? {} : { partitions: [...value.partitions] }) };
}

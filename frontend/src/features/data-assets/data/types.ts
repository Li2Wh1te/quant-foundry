/** Current server DTOs. Display labels and request-session state live separately. */
export interface CurrentField {
  column: string;
  type: string;
  meaning: string;
  logical_type: string;
  arithmetic: string;
}

export interface CurrentUpdate {
  state: string;
  complete: boolean;
  qualified: boolean;
  reason: string | null;
  source_rows: number | null;
  committed_partitions: number | null;
  updated_at: string | null;
}

export interface CurrentDataset {
  dataset: string;
  name: string;
  domain: string;
  source: string;
  frequency: string;
  /** Descriptor row layout; never use this as the query representation. */
  representation: string;
  schema_id: string;
  rule: string;
  fields: CurrentField[];
  limitations: string[];
  business_key: string[];
  /** Missing/null states normalize to "unknown", never to a usable state. */
  status: string;
  row_count: number | null;
  generation: number | null;
  updated_at: string | null;
  issues: number | null;
  legacy_restrictions?: number;
  last_update: CurrentUpdate | null;
  partitions: string[];
  partitions_truncated: boolean;
  partition_range: { from: string | null; to: string | null; precision: string };
  preview_key: { representation: string; subject: string; object_key: string; partition: string } | null;
}

export interface DatasetList {
  items: CurrentDataset[];
  total: number;
  phase: string;
  next_offset: number | null;
}

export interface CurrentEntryStatus extends CurrentUpdate {
  entry_id: string;
  /** Non-business entries have no dataset and must not inflate catalog counts. */
  dataset: string | null;
  name: string;
  classification: string;
}

export interface StatusList {
  items: CurrentEntryStatus[];
  total: number;
  phase: string;
  code: string;
  fresh_install: boolean;
  next_offset: number | null;
}

export interface CurrentIssue {
  dataset: string | null;
  scope_key: string | null;
  reason: string;
  kind: string;
  updated_at: string | null;
  /** A member count, not a count of distinct business objects or error rate. */
  affected_objects: number | null;
}

export interface IssueList {
  items: CurrentIssue[];
  total: number;
  affected_objects: number | null;
  next_offset: number | null;
}

export interface PreviewRequest {
  dataset: string;
  frequency: string;
  representation: string;
  subject: string;
  from_key: string;
  to_key: string;
  columns: string[];
  page_size: number;
  cursor?: string | null;
  allow_partial: boolean;
  partitions?: string[] | null;
}

export type CurrentValue = string | number | boolean | null;
export interface PreviewResult {
  dataset: string;
  status: string;
  request_satisfied: boolean;
  business_date_coverage_verified: boolean;
  partial_requested?: boolean;
  rows: Record<string, CurrentValue>[];
  next_cursor: string | null;
  generation: number | null;
  /** The range of this page only, not coverage of the requested interval. */
  actual_range: { from: string | null; to: string | null };
  selected_partitions: string[];
  limitations: string[];
  unresolved_issues?: number;
  schema_id?: string;
  semantics?: Record<string, string>;
}

export interface RequestOptions { signal?: AbortSignal; timeoutMs?: number }
export interface PageOptions extends RequestOptions { limit?: number; offset?: number }
export interface StatusOptions extends PageOptions { state?: string }
export interface CatalogOptions extends RequestOptions {
  pageSize?: number;
  maxPages?: number;
  maxItems?: number;
}
export interface CatalogCounts {
  total: number;
  byStatus: Record<string, number>;
}
export type CatalogIncompleteReason = "budget" | "pagination" | "changed" | "failed";
export interface CatalogSnapshot extends DatasetList {
  complete: boolean;
  incompleteReason: CatalogIncompleteReason | null;
  counts: CatalogCounts | null;
  /** Client observation time: label it "页面读取于". */
  readAt: string;
  error: Error | null;
}

export type PresentationTone = "neutral" | "success" | "warning" | "danger" | "info";
export interface StatusPresentation {
  label: string;
  tone: PresentationTone;
  known: boolean;
  diagnosticCode: string | null;
}

export interface PreviewSessionSnapshot {
  result: PreviewResult | null;
  page: number;
  cursors: (string | null)[];
  readAt: string | null;
}

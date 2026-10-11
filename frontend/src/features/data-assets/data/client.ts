import { dataStoreApi } from "../../../api/dataStore";
import { apiError, cancellation, DataStoreApiError, invalidRequest, invalidResponse, isCancellation } from "./errors";
import type { CatalogCounts, CatalogIncompleteReason, CatalogOptions, CatalogSnapshot, CurrentDataset,
  CurrentEntryStatus, DatasetList, IssueList, ConsistentPageOptions, PageOptions, PreviewRequest, PreviewResult, RequestOptions,
  StatusList, StatusOptions } from "./types";
import { parseDataset, parseDatasetList, parseIssueList, parsePreview, parseStatusList, validatePreviewRequest } from "./validation";

export type DataStoreTransport = <T>(path: string, signal?: AbortSignal, body?: unknown,
  options?: Omit<RequestOptions, "signal">) => Promise<T>;

function bounded(value: number | undefined, fallback: number, maximum: number, at: string): number {
  const result = value ?? fallback;
  if (!Number.isSafeInteger(result) || result < 1 || result > maximum) invalidRequest(at);
  return result;
}
function pagination(options: PageOptions): URLSearchParams {
  const offset = options.offset ?? 0;
  if (!Number.isSafeInteger(offset) || offset < 0) invalidRequest("offset");
  return new URLSearchParams({ limit: String(bounded(options.limit, 50, 100, "limit")), offset: String(offset) });
}
function datasetId(dataset: string): string {
  if (typeof dataset !== "string" || !dataset.trim() || dataset.length > 256) invalidRequest("dataset");
  return encodeURIComponent(dataset);
}
function consistentPagination(options: ConsistentPageOptions): URLSearchParams {
  const query = pagination(options);
  if (options.revision !== undefined) {
    if (!/^[0-9a-f]{64}$/.test(options.revision)) invalidRequest("revision");
    query.set("revision", options.revision);
  }
  return query;
}

export interface DataAssetsClient {
  listDatasets(options?: ConsistentPageOptions): Promise<DatasetList>;
  getDataset(dataset: string, options?: RequestOptions): Promise<CurrentDataset>;
  listStatus(options?: StatusOptions): Promise<StatusList>;
  listIssues(dataset?: string, options?: ConsistentPageOptions): Promise<IssueList>;
  queryPreview(request: PreviewRequest, options?: RequestOptions): Promise<PreviewResult>;
  loadCatalog(options?: CatalogOptions): Promise<CatalogSnapshot>;
}

/** The sole typed client sits above the existing authenticated current-store transport. */
export function createDataAssetsClient(transport: DataStoreTransport = dataStoreApi): DataAssetsClient {
  const client: DataAssetsClient = {
    async listDatasets(options = {}) {
      const query = consistentPagination(options);
      const result = parseDatasetList(await transport(`/datasets?${query}`, options.signal, undefined, options));
      if (options.revision !== undefined && result.revision !== options.revision) throw apiError(409, "DATA_CHANGED");
      if (result.items.length > Number(query.get("limit"))) invalidResponse("datasets.items");
      return result;
    },
    async getDataset(dataset, options = {}) {
      const result = parseDataset(await transport(`/datasets/${datasetId(dataset)}`, options.signal, undefined, options));
      if (result.dataset !== dataset) invalidResponse("dataset.dataset");
      return result;
    },
    async listStatus(options = {}) {
      const query = pagination(options);
      if (options.state !== undefined) {
        if (!/^[a-z][a-z0-9_-]{0,63}$/.test(options.state)) invalidRequest("state");
        query.set("state", options.state);
      }
      const result = parseStatusList(await transport(`/status?${query}`, options.signal, undefined, options));
      if (result.items.length > Number(query.get("limit"))) invalidResponse("status.items");
      return result;
    },
    async listIssues(dataset, options = {}) {
      const query = consistentPagination(options);
      if (dataset !== undefined) { datasetId(dataset); query.set("dataset", dataset); }
      const result = parseIssueList(await transport(`/issues?${query}`, options.signal, undefined, options));
      if (options.revision !== undefined && result.revision !== options.revision) throw apiError(409, "DATA_CHANGED");
      if (result.items.length > Number(query.get("limit"))) invalidResponse("issues.items");
      if (dataset !== undefined && result.items.some(item => item.dataset !== null && item.dataset !== dataset)) invalidResponse("issues.items.dataset");
      return result;
    },
    async queryPreview(request, options = {}) {
      const body = validatePreviewRequest(request);
      const result = parsePreview(await transport("/query", options.signal, body, options));
      if (result.dataset !== body.dataset || result.rows.length > body.page_size || result.partial_requested === true) invalidResponse("preview");
      return result;
    },
    async loadCatalog(options = {}) {
      const pageSize = bounded(options.pageSize, 100, 100, "pageSize");
      const maxPages = bounded(options.maxPages, 10, 10, "maxPages");
      const maxItems = bounded(options.maxItems, 1000, 1000, "maxItems");
      const previous = options.previous;
      if (previous && (previous.complete || previous.next_offset === null || !previous.revision
        || previous.next_offset !== previous.items.length || previous.incompleteReason !== "budget")) invalidRequest("catalog.continuation");
      const items: CurrentDataset[] = previous ? [...previous.items] : [];
      const baseCount = items.length, identities = new Set(items.map(item => item.dataset)), offsets = new Set<number>();
      let total: number | undefined = previous?.total, phase = previous?.phase ?? "unknown";
      let nextOffset: number | null = previous?.next_offset ?? 0, revision: string | null = previous?.revision ?? null;
      let incompleteReason: CatalogIncompleteReason | null = null, error: Error | null = null;
      let readAt = new Date().toISOString();
      for (let page = 0; page < maxPages && nextOffset !== null; page++) {
        if (options.signal?.aborted) throw cancellation();
        const offset: number = nextOffset;
        if (offsets.has(offset) || items.length - baseCount >= maxItems) {
          incompleteReason = offsets.has(offset) ? "pagination" : "budget"; break;
        }
        offsets.add(offset);
        const limit = Math.min(pageSize, maxItems - (items.length - baseCount));
        let result: DatasetList;
        try {
          result = await client.listDatasets({ signal: options.signal, timeoutMs: options.timeoutMs,
            limit, offset, ...(revision ? { revision } : {}) });
          if (result.next_offset !== null && result.revision === null) invalidResponse("datasets.revision");
          if (options.signal?.aborted) throw cancellation();
        } catch (problem) {
          // Auth failures/cancellation discard the accumulated response. An ordinary
          // mid-load failure may retain metadata, but a first-page failure is no catalog.
          if (isCancellation(problem) || (problem instanceof DataStoreApiError
            && ([401, 403].includes(problem.status) || problem.code === "DATA_CHANGED")) || total === undefined) throw problem;
          error = problem instanceof DataStoreApiError ? problem : apiError(0, "NETWORK_ERROR");
          incompleteReason = "failed"; break;
        }
        readAt = new Date().toISOString();
        if (total !== undefined && (result.total !== total || result.phase !== phase || result.revision !== revision)) {
          throw apiError(409, "DATA_CHANGED");
        }
        total = result.total; phase = result.phase; revision = result.revision;
        if (result.items.some(item => identities.has(item.dataset))
          || new Set(result.items.map(item => item.dataset)).size !== result.items.length) {
          incompleteReason = "pagination"; break;
        }
        for (const item of result.items) { identities.add(item.dataset); items.push(item); }
        nextOffset = result.next_offset;
        if (nextOffset !== null && (nextOffset !== offset + result.items.length || nextOffset <= offset || offsets.has(nextOffset))) {
          incompleteReason = "pagination"; break;
        }
        if (nextOffset !== null && items.length >= total) { incompleteReason = "pagination"; break; }
      }
      if (total === undefined) throw apiError(502, "INVALID_RESPONSE");
      if (!incompleteReason && nextOffset !== null) incompleteReason = "budget";
      if (!incompleteReason && items.length !== total) incompleteReason = "pagination";
      const complete = incompleteReason === null;
      const counts: CatalogCounts | null = complete ? { total: items.length, byStatus: Object.create(null) } : null;
      if (counts) for (const item of items) counts.byStatus[item.status] = (counts.byStatus[item.status] ?? 0) + 1;
      return { items, total, phase, revision, next_offset: nextOffset, complete, incompleteReason, counts, readAt, error };
    }
  };
  return client;
}

export const dataAssetsClient = createDataAssetsClient();

/** Link status by dataset identity, retaining multiple entries and excluding non-business entries. */
export function statusByDataset(items: readonly CurrentEntryStatus[]): Map<string, CurrentEntryStatus[]> {
  const result = new Map<string, CurrentEntryStatus[]>();
  for (const item of items) {
    if (!item.dataset) continue;
    result.set(item.dataset, [...(result.get(item.dataset) ?? []), item]);
  }
  return result;
}

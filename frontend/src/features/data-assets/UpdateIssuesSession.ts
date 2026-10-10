import { clearsPreview, createRequestScope, dataAssetsClient, onDataAssetsInvalidation,
  type DataAssetsClient, type IssueList } from "./data";
import { apiError, cancellation, invalidRequest, invalidResponse } from "./data/errors";
import { readApiToken } from "../../auth/tokenStorage";

export const ISSUE_PAGE_SIZE = 20;
export interface IssuesSnapshot {
  result: IssueList | null;
  page: number;
  offset: number;
  readAt: string | null;
  stale: boolean;
}

/** D06-local page state. D01 owns transport/DTO validation. No auto paging or deduplication. */
export function createUpdateIssuesSession(dataset: string, client: Pick<DataAssetsClient, "listIssues"> = dataAssetsClient) {
  const scope = createRequestScope();
  let result: IssueList | null = null, page = 0, displayedOffset = 0, offsets = [0], readAt: string | null = null;
  let stale = false, revision = 0, token = readApiToken();
  const clear = () => { result = null; page = 0; displayedOffset = 0; offsets = [0]; readAt = null; stale = false; revision++; };
  const reset = () => { scope.cancel(); clear(); token = readApiToken(); };
  const stop = onDataAssetsInvalidation(reason => {
    clear();
    if (reason === "session") scope.cancel();
  });
  return {
    reset,
    cancel() { scope.cancel(); revision++; },
    dispose() { scope.dispose(); clear(); stop(); },
    getSnapshot(): IssuesSnapshot {
      if (token !== readApiToken()) reset();
      return { result: result && { ...result, items: result.items.map(item => ({ ...item })) },
        page, offset: displayedOffset, readAt, stale };
    },
    async read(target = 0, restart = false): Promise<void> {
      if (token !== readApiToken()) reset();
      if (restart) { scope.cancel(); revision++; offsets = [0]; stale = result !== null; }
      if (!Number.isSafeInteger(target) || target < 0 || target >= offsets.length || (stale && target !== 0)) invalidRequest("issues.page");
      const version = revision, offset = offsets[target];
      try {
        const next = await scope.run(signal => client.listIssues(dataset, { limit: ISSUE_PAGE_SIZE, offset, signal }));
        if (version !== revision) throw cancellation();
        const end = offset + next.items.length;
        if (next.items.length > ISSUE_PAGE_SIZE || end > next.total || (next.next_offset === null ? end !== next.total
          : next.next_offset !== end || end <= offset || end >= next.total)) invalidResponse("issues.pagination");
        // Offset pagination has no snapshot token. Changed inventories cannot join an older page sequence.
        if (target > 0 && result && (next.total !== result.total || next.affected_objects !== result.affected_objects)) {
          throw apiError(409, "DATA_CHANGED");
        }
        result = next; page = target; displayedOffset = offset; readAt = new Date().toISOString(); stale = false;
        offsets = offsets.slice(0, target + 1);
        if (next.next_offset !== null) offsets.push(next.next_offset);
      } catch (error) {
        if (version === revision && clearsPreview(error)) reset();
        throw error;
      }
    }
  };
}

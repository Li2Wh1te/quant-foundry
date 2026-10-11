import { readApiToken } from "../auth/tokenStorage";
import { apiError, cancellation, DataStoreApiError, invalidRequest } from "../features/data-assets/data/errors";
import { invalidateDataAssetsSession, registerRequest } from "../features/data-assets/data/lifecycle";
import type { PreviewRequest, RequestOptions } from "../features/data-assets/data/types";
import { parseDataset, parseDatasetList, parseExactJson, parseIssueList, parsePreview,
  parseStatusList, validatePreviewRequest } from "../features/data-assets/data/validation";

// Keep the existing import surface while the page modules are split independently.
export { DataStoreApiError } from "../features/data-assets/data/errors";
export type { CurrentDataset, DatasetList, IssueList, PreviewRequest, PreviewResult,
  StatusList } from "../features/data-assets/data/types";

function endpoint(path: string, body: unknown) {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("#")) invalidRequest("path");
  const pathname = path.split("?", 1)[0];
  if (body !== undefined) {
    if (path !== "/query") invalidRequest("path");
    return { parse: parsePreview, body: validatePreviewRequest(body as PreviewRequest) };
  }
  if (pathname === "/datasets") return { parse: parseDatasetList, body: undefined };
  if (pathname.startsWith("/datasets/") && pathname.length > "/datasets/".length) return { parse: parseDataset, body: undefined };
  if (pathname === "/status") return { parse: parseStatusList, body: undefined };
  if (pathname === "/issues") return { parse: parseIssueList, body: undefined };
  return invalidRequest("path");
}

/**
 * Current-only transport: validate DTOs, disable HTTP caching, and race cancellation
 * independently of fetch. A mock/proxy ignoring AbortSignal cannot deliver late data.
 */
export async function dataStoreApi<T>(path: string, signal?: AbortSignal, body?: unknown,
  options: Omit<RequestOptions, "signal"> = {}): Promise<T> {
  if (signal?.aborted) throw cancellation();
  const route = endpoint(path, body);
  const timeoutMs = options.timeoutMs ?? (body === undefined ? 15_000 : 30_000);
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) invalidRequest("timeoutMs");
  const token = readApiToken();
  const controller = new AbortController();
  const unregister = registerRequest(controller);
  const cancel = () => controller.abort(cancellation());
  signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  const fresh = () => {
    if (controller.signal.aborted || signal?.aborted || readApiToken() !== token) throw cancellation();
  };
  let rejectAbort: (() => void) | undefined;
  const cancelled = new Promise<never>((_resolve, reject) => {
    rejectAbort = () => reject(cancellation());
    controller.signal.addEventListener("abort", rejectAbort, { once: true });
  });
  try {
    const work = async () => {
      fresh();
      const response = await fetch(`/api/admin/data-store${path}`, {
        method: route.body === undefined ? "GET" : "POST", signal: controller.signal, cache: "no-store",
        headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), "Content-Type": "application/json" },
        ...(route.body === undefined ? {} : { body: JSON.stringify(route.body) })
      });
      fresh();
      // Authentication and permission decisions are authoritative at the HTTP
      // status. Do not delay invalidation on an optional or stalled error body.
      if (response.status === 401 || response.status === 403) throw apiError(response.status, undefined);
      if (!response.ok) {
        let code: unknown;
        try {
          const raw: unknown = parseExactJson(await response.text());
          if (raw && typeof raw === "object" && "detail" in raw) {
            const detail = raw.detail;
            if (detail && typeof detail === "object" && "code" in detail) code = detail.code;
          }
        } catch { /* Ignore malformed error bodies and keep the safe local summary. */ }
        fresh();
        throw apiError(response.status, code);
      }
      const raw = parseExactJson(await response.text());
      fresh();
      // The legacy generic is retained only for import compatibility. The value is
      // reconstructed by endpoint-specific runtime checks before any cast occurs.
      return route.parse(raw) as T;
    };
    const result = await Promise.race([work(), cancelled]);
    fresh();
    return result;
  } catch (error) {
    if (signal?.aborted || readApiToken() !== token) throw cancellation();
    if (error instanceof DataStoreApiError) {
      if (error.status === 401 || error.status === 403) {
        invalidateDataAssetsSession(error.status === 401 ? "authentication" : "permission", controller);
      }
      throw error;
    }
    if (timedOut) throw apiError(504, "QUERY_TIMEOUT");
    if (controller.signal.aborted) throw cancellation();
    if (error instanceof SyntaxError) throw apiError(502, "INVALID_RESPONSE");
    throw apiError(0, "NETWORK_ERROR");
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener("abort", cancel);
    if (rejectAbort) controller.signal.removeEventListener("abort", rejectAbort);
    unregister();
  }
}

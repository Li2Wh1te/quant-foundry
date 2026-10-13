import { readApiToken } from "../../../auth/tokenStorage";
import { invalidateDataAssetsSession, registerRequest } from "../../data-assets/data/lifecycle";
import { cancellation, isCancellation, MarketApiError, marketError, invalidRequest, invalidResponse } from "./errors";
import type { RequestOptions } from "./types";

export type MarketTransport = (path: string, options?: RequestOptions) => Promise<unknown>;
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

/** Read-only transport; reuse the existing API credential and logout lifecycle. */
export const marketTransport: MarketTransport = async (path, options = {}) => {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("#")) invalidRequest();
  if (options.signal?.aborted) throw cancellation();
  const timeout = options.timeoutMs ?? 15_000;
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 60_000) invalidRequest();
  const token = readApiToken(), controller = new AbortController(), unregister = registerRequest(controller);
  const cancel = () => controller.abort();
  options.signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  const fresh = () => {
    if (controller.signal.aborted || options.signal?.aborted || readApiToken() !== token) throw cancellation();
  };
  let rejectAbort: (() => void) | undefined;
  const cancelled = new Promise<never>((_resolve, reject) => {
    rejectAbort = () => reject(cancellation());
    controller.signal.addEventListener("abort", rejectAbort, { once: true });
  });
  try {
    const work = async () => {
      fresh();
      const response = await fetch(`/api/admin/market${path}`, { method: "GET", signal: controller.signal,
        cache: "no-store", headers: { Accept: "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) } });
      fresh();
      if ([401, 403].includes(response.status)) throw marketError(response.status);
      if (!response.ok) {
        let code: unknown;
        try {
          const body = await boundedJson(response);
          if (body && typeof body === "object" && "detail" in body && body.detail
              && typeof body.detail === "object" && "code" in body.detail) code = body.detail.code;
        } catch { /* Use only a safe local error summary. */ }
        fresh(); throw marketError(response.status, code);
      }
      const body = await boundedJson(response); fresh(); return body;
    };
    const result = await Promise.race([work(), cancelled]); fresh(); return result;
  } catch (error) {
    if (options.signal?.aborted || readApiToken() !== token) throw cancellation();
    if (error instanceof MarketApiError) {
      if ([401, 403].includes(error.status)) invalidateDataAssetsSession(error.status === 401 ? "authentication" : "permission", controller);
      throw error;
    }
    if (timedOut) throw marketError(504, "QUERY_TIMEOUT");
    if (controller.signal.aborted || isCancellation(error)) throw cancellation();
    throw marketError(0, "NETWORK_ERROR");
  } finally {
    clearTimeout(timer); options.signal?.removeEventListener("abort", cancel);
    if (rejectAbort) controller.signal.removeEventListener("abort", rejectAbort);
    unregister();
  }
};

async function boundedJson(response: Response): Promise<unknown> {
  // Stream and charge actual bytes, not just an untrusted Content-Length.
  const declared = response.headers.get("Content-Length");
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > MAX_RESPONSE_BYTES)) invalidResponse();
  const reader = response.body?.getReader();
  let body = "";
  if (reader) {
    let bytes = 0;
    const decoder = new TextDecoder("utf-8", { fatal: true });
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        bytes += chunk.value.byteLength;
        if (bytes > MAX_RESPONSE_BYTES) invalidResponse();
        try { body += decoder.decode(chunk.value, { stream: true }); } catch { invalidResponse(); }
      }
      try { body += decoder.decode(); } catch { invalidResponse(); }
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  } else {
    body = await response.text();
    if (new TextEncoder().encode(body).byteLength > MAX_RESPONSE_BYTES) invalidResponse();
  }
  try { return JSON.parse(body) as unknown; } catch { invalidResponse(); }
}

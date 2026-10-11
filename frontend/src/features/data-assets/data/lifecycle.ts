import { cancellation } from "./errors";

export type DataAssetsInvalidation = "authentication" | "permission" | "session";
const requests = new Set<AbortController>();
const listeners = new Set<(reason: DataAssetsInvalidation) => void>();

/** Only in-flight requests and explicitly owned page sessions are registered. */
export function registerRequest(controller: AbortController): () => void {
  requests.add(controller);
  return () => { requests.delete(controller); };
}
export function onDataAssetsInvalidation(listener: (reason: DataAssetsInvalidation) => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function invalidateDataAssetsSession(reason: DataAssetsInvalidation = "session", except?: AbortController): void {
  for (const controller of requests) if (controller !== except) controller.abort(cancellation());
  for (const listener of listeners) listener(reason);
}

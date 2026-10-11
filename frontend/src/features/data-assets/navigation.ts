export const DATA_ASSETS_ROOT = "/admin/data-assets";
export type DataAssetsView = "overview" | "preview" | "updates";

const catalogKeys = ["search", "status", "source", "frequency", "page"] as const;

/** Only list context belongs in links; query cursors and results stay in memory. */
export function catalogParams(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams();
  for (const key of catalogKeys) {
    const value = params.get(key);
    if (value !== null) next.set(key, value);
  }
  return next;
}

export function dataAssetsView(value: string | null): DataAssetsView {
  return value === "preview" || value === "updates" ? value : "overview";
}

export function viewParams(params: URLSearchParams, view: DataAssetsView): URLSearchParams {
  const next = catalogParams(params);
  next.set("view", view);
  return next;
}

export function catalogHref(params: URLSearchParams): string {
  const query = catalogParams(params).toString();
  return `${DATA_ASSETS_ROOT}${query ? `?${query}` : ""}`;
}

export function datasetHref(datasetId: string, params: URLSearchParams): string {
  const query = catalogParams(params).toString();
  return `${DATA_ASSETS_ROOT}/${encodeURIComponent(datasetId)}${query ? `?${query}` : ""}`;
}

function scrollKey(params: URLSearchParams): string {
  return `qf-assets-scroll:${catalogParams(params)}`;
}

export function saveCatalogScroll(params: URLSearchParams): void {
  try {
    sessionStorage.setItem(scrollKey(params), String(document.querySelector(".qfo-main")?.scrollTop ?? 0));
  } catch { /* List navigation also works when optional storage is unavailable. */ }
}

export function restoreCatalogScroll(params: URLSearchParams): void {
  try {
    const main = document.querySelector(".qfo-main");
    const value = Number(sessionStorage.getItem(scrollKey(params)) ?? 0);
    if (main) main.scrollTop = Number.isFinite(value) && value >= 0 ? value : 0;
  } catch { /* Keep the browser's default scroll position. */ }
}

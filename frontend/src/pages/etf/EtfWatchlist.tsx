import { useCallback, useEffect, useRef, useState } from "react";
import { MoreHorizontal, Plus, X } from "lucide-react";
import { listEtfs, type EtfCode } from "../../api/dataCollections";
import {
  listWatchlist,
  setWatched,
  type WatchItem,
} from "../../api/etfWatchlist";
import { direction, numberText, numeric, signed } from "./etfData";
import { EtfPopover, type PopoverAnchor } from "./EtfPopover";

export function EtfWatchlist({
  code,
  replay,
  refresh,
  onSelect,
  onClose,
  onError,
}: {
  code: string;
  replay: boolean;
  refresh: number;
  onSelect: (code: string) => void;
  onClose: () => void;
  onError: (error: unknown) => void;
}) {
  const [rows, setRows] = useState<WatchItem[]>([]),
    [loading, setLoading] = useState(true),
    [error, setError] = useState("");
  const [anchor, setAnchor] = useState<PopoverAnchor | null>(null),
    [keyword, setKeyword] = useState(""),
    [matches, setMatches] = useState<EtfCode[]>([]);
  const [searching, setSearching] = useState(false),
    [searchError, setSearchError] = useState(""),
    [searchOffset, setSearchOffset] = useState(0),
    [total, setTotal] = useState(0);
  const [pending, setPending] = useState(""),
    [revision, setRevision] = useState(0);
  const generation = useRef(0),
    mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    const version = ++generation.current;
    setLoading(true);
    setError("");
    void (async () => {
      const next: WatchItem[] = [];
      let offset = 0,
        more = true;
      while (more) {
        const page = await listWatchlist(offset, controller.signal);
        next.push(...page.items);
        more = page.has_more;
        offset += page.items.length;
        if (more && !page.items.length)
          throw new Error("自选分页返回异常，请重试。");
      }
      if (version === generation.current) setRows(next);
    })()
      .catch((e) => {
        if (!controller.signal.aborted) {
          setError(e instanceof Error ? e.message : "自选加载失败。");
          onError(e);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [refresh, revision, onError]);
  useEffect(() => {
    if (anchor?.key !== "add" || !keyword.trim()) {
      setMatches([]);
      setTotal(0);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    setSearchError("");
    setMatches([]);
    const timer = setTimeout(() => {
      void listEtfs(
        { keyword: keyword.trim(), limit: 20, offset: searchOffset },
        controller.signal,
      )
        .then((result) => {
          setMatches(result.items);
          setTotal(result.total);
        })
        .catch((e) => {
          if (!controller.signal.aborted) {
            setSearchError(e instanceof Error ? e.message : "搜索失败。");
            onError(e);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [keyword, searchOffset, anchor?.key, onError]);
  const mutate = useCallback(
    async (target: string, watched: boolean) => {
      if (pending) return;
      setPending(target);
      setError("");
      try {
        await setWatched(target, watched);
        if (!mounted.current) return;
        if (!watched)
          setRows((prior) => prior.filter((item) => item.ts_code !== target));
        // Reload the server snapshot after success; don't invent quote facts.
        setRevision((v) => v + 1);
      } catch (e) {
        if (mounted.current) {
          setError(e instanceof Error ? e.message : "自选操作失败，请重试。");
          onError(e);
        }
      } finally {
        if (mounted.current) setPending("");
      }
    },
    [pending, onError],
  );
  const open = (key: string, element: HTMLElement) =>
    setAnchor((prior) => (prior?.key === key ? null : { key, element }));
  return (
    <section className="qfe-watch" aria-label="自选 ETF">
      <div className="qfe-right-head">
        <strong>自选 ETF</strong>
        <span className="qfe-spacer" />
        <button
          className="qfe-icon"
          aria-label="添加自选"
          title="添加自选"
          onClick={(e) => open("add", e.currentTarget)}
        >
          <Plus />
        </button>
        <button
          className="qfe-icon"
          aria-label="管理自选"
          title="管理自选"
          onClick={(e) => open("manage", e.currentTarget)}
        >
          <MoreHorizontal />
        </button>
        <button
          className="qfe-icon qfe-narrow-close"
          aria-label="关闭资料侧栏"
          onClick={onClose}
        >
          <X />
        </button>
      </div>
      <div className="qfe-watch-scroll" aria-busy={loading}>
        <div className="qfe-watch-columns">
          <span>标的</span>
          <span>收盘</span>
          <span>涨跌</span>
          <span>涨跌幅</span>
        </div>
        {error && (
          <div className="qfe-note qfe-error" role="alert">
            {error}
            <button
              onClick={() => setRevision((v) => v + 1)}
              disabled={loading}
            >
              重试
            </button>
          </div>
        )}
        {loading && !rows.length ? (
          <p className="qfe-note">正在加载自选…</p>
        ) : !rows.length ? (
          <p className="qfe-note">还没有自选 ETF。点击 ＋ 添加。</p>
        ) : (
          rows.map((item) => (
            <button
              key={item.ts_code}
              className={`qfe-watch-row ${code === item.ts_code ? "active" : ""}`}
              onClick={() => onSelect(item.ts_code)}
              title={`${item.name ?? item.ts_code} · ${replay ? "回放中隐藏最新行情" : `${item.trade_date ?? "无日线"} · 不复权收盘 · ${item.message}`}`}
            >
              <span className="qfe-watch-code">{item.ts_code}</span>
              <span>{replay ? "—" : numberText(item.close)}</span>
              <span className={replay ? "neutral" : direction(item.change)}>
                {replay ? "—" : signed(item.change)}
              </span>
              <span className={replay ? "neutral" : direction(item.pct_chg)}>
                {replay || numeric(item.pct_chg) === null
                  ? "—"
                  : `${signed(item.pct_chg, 2)}%`}
              </span>
            </button>
          ))
        )}
        {replay && <p className="qfe-note">回放中隐藏自选最新行情。</p>}
      </div>
      {anchor && (
        <EtfPopover
          anchor={anchor}
          boundary={anchor.element.closest<HTMLElement>(".qfe-right")}
          title={anchor.key === "add" ? "添加自选" : "管理自选"}
          close={() => setAnchor(null)}
        >
          {anchor.key === "add" ? (
            <>
              <label htmlFor="qfe-watch-search">代码、名称或跟踪指数</label>
              <input
                id="qfe-watch-search"
                type="search"
                value={keyword}
                placeholder="例如 510300 或沪深300"
                onChange={(e) => {
                  setKeyword(e.target.value);
                  setSearchOffset(0);
                }}
              />
              <div className="qfe-results" aria-live="polite">
                {searching ? (
                  <p>正在搜索…</p>
                ) : searchError ? (
                  <p role="alert">{searchError}</p>
                ) : !keyword.trim() ? (
                  <p>输入关键词搜索 ETF。</p>
                ) : !matches.length ? (
                  <p>没有匹配的 ETF，请调整关键词。</p>
                ) : (
                  matches.map((item) => (
                    <div className="qfe-search-row" key={item.ts_code}>
                      <div>
                        <strong>
                          {item.csname ?? item.extname ?? item.ts_code}
                        </strong>
                        <small>{item.ts_code}</small>
                      </div>
                      <button
                        disabled={
                          Boolean(pending) ||
                          loading ||
                          rows.some((row) => row.ts_code === item.ts_code)
                        }
                        onClick={() => void mutate(item.ts_code, true)}
                      >
                        {pending === item.ts_code
                          ? "添加中…"
                          : rows.some((row) => row.ts_code === item.ts_code)
                            ? "已添加"
                            : "添加"}
                      </button>
                    </div>
                  ))
                )}
              </div>
              {total > 20 && (
                <div className="qfe-actions">
                  <button
                    disabled={searchOffset === 0 || searching}
                    onClick={() => setSearchOffset((v) => v - 20)}
                  >
                    上一页
                  </button>
                  <span>
                    {searchOffset + 1}–{Math.min(searchOffset + 20, total)} /{" "}
                    {total}
                  </span>
                  <button
                    disabled={searchOffset + 20 >= total || searching}
                    onClick={() => setSearchOffset((v) => v + 20)}
                  >
                    下一页
                  </button>
                </div>
              )}
              <p>选择后加入自选，当前研究标的不变。</p>
            </>
          ) : (
            <>
              <p>移除只影响自选列表，保留当前图表与行情数据。</p>
              <div className="qfe-results">
                {rows.length ? (
                  rows.map((item) => (
                    <div className="qfe-search-row" key={item.ts_code}>
                      <div>
                        <strong>{item.ts_code}</strong>
                        <small>{item.name}</small>
                      </div>
                      <button
                        disabled={Boolean(pending) || loading}
                        onClick={() => void mutate(item.ts_code, false)}
                      >
                        {pending === item.ts_code ? "移除中…" : "移除"}
                      </button>
                    </div>
                  ))
                ) : (
                  <p>还没有自选 ETF。点击 ＋ 添加。</p>
                )}
              </div>
            </>
          )}
          {error && (
            <p className="qfe-error" role="alert">
              {error}
            </p>
          )}
        </EtfPopover>
      )}
    </section>
  );
}

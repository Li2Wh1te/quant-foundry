import { useEffect, useRef, useState } from "react";
import { resultPage, type ResultKind, type ResultPage } from "../../api/backtestResults";

/** One page retained per section; cleanup and sequence guard late transports. */
export function useResultPage(id: string, kind: ResultKind, cursor: string, enabled: boolean, version: number) {
  const key = `${id}:${kind}:${cursor}`;
  const [stored, setStored] = useState<{ key: string; value: ResultPage } | null>(null);
  const [error, setError] = useState(""), [loading, setLoading] = useState(false), [retry, setRetry] = useState(0), [cancelled, setCancelled] = useState(false);
  const sequence = useRef(0), controller = useRef<AbortController | null>(null);
  useEffect(() => {
    const c = new AbortController(), seq = ++sequence.current; controller.current = c;
    setError(""); setCancelled(false); setLoading(enabled);
    if (!enabled) { setStored(null); return () => c.abort(); }
    resultPage(id, kind, cursor || undefined, c.signal).then(value => {
      if (!c.signal.aborted && seq === sequence.current) setStored({ key, value });
    }).catch(e => { if (!c.signal.aborted && seq === sequence.current) { if (e.status === 404) setStored(null); setError(e instanceof Error ? e.message : "读取失败，请重试。"); } })
      .finally(() => { if (!c.signal.aborted && seq === sequence.current) setLoading(false); });
    return () => { c.abort(); sequence.current++; };
  }, [key, enabled, version, retry]);
  return { page: enabled && stored?.key === key ? stored.value : null, error, loading, cancelled,
    cancel: () => { controller.current?.abort(); sequence.current++; setLoading(false); setCancelled(true); },
    reload: () => setRetry(v => v + 1) };
}

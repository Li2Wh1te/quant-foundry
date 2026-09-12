import {
  Archive,
  Code2,
  ExternalLink,
  Plus,
  RefreshCw,
  Rocket,
  Save,
  ShieldCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useBlocker, useNavigate, useParams } from "react-router-dom";

import {
  archiveStrategy,
  createStrategy,
  getStrategy,
  getStrategyRevision,
  listStrategies,
  listStrategyRevisions,
  publishStrategy,
  saveStrategyDraft,
  StrategyApiError,
  StrategyDetail,
  StrategyRevision,
  StrategyRevisionSummary,
  StrategySummary,
  StrategyValidationIssue,
  StrategyValidationResult,
  updateStrategyMetadata,
  validateStrategy,
} from "../api/strategies";
import { OverviewShell } from "../components/OverviewShell";
import { TEMPLATES, type TemplateKey } from "./strategies/templates";
import { Drawer, ResizeHandle, RecentRuns } from "./strategies/WorkspaceParts";
import "./strategies/StrategyWorkspace.css";
import { useAuth } from "../auth/AuthContext";

type EditorDraft = {
  name: string;
  description: string;
  sourceCode: string;
  parameterSchema: string;
  defaultParameters: string;
};

type CreateDraft = {
  name: string;
  description: string;
  template: TemplateKey;
};

const EMPTY_CREATE_DRAFT: CreateDraft = {
  name: "",
  description: "",
  template: "hold",
};

function prettyJson(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2);
}

function editorDraftFromDetail(detail: StrategyDetail): EditorDraft {
  return {
    name: detail.name,
    description: detail.description ?? "",
    sourceCode: detail.draft.source_code,
    parameterSchema: prettyJson(detail.draft.parameter_schema),
    defaultParameters: prettyJson(detail.draft.default_parameters),
  };
}

function parseJsonObject(
  value: string,
  label: string,
): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`${label}必须是有效的 JSON。`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象。`);
  }
  return parsed as Record<string, unknown>;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function shortHash(value: string | null | undefined): string {
  if (!value) return "—";
  return `${value.slice(0, 10)}…${value.slice(-8)}`;
}

function issueLocation(issue: StrategyValidationIssue): string {
  if (issue.line === null || issue.line === undefined) return "全局检查";
  return `第 ${issue.line} 行${issue.column ? `，第 ${issue.column} 列` : ""}`;
}

function stateLabel(
  detail: StrategyDetail,
  validation: StrategyValidationResult | null,
  isDirty: boolean,
): string {
  if (detail.state === "archived") return "已归档";
  if (
    !isDirty &&
    validation?.draft_version === detail.draft.version &&
    !validation.valid
  )
    return "发布检查失败";
  if (!detail.current_revision_id) return "从未发布";
  if (isDirty || detail.draft_changed_since_revision) return "草稿有未发布修改";
  return "已发布";
}

function isSameEditorDraft(
  detail: StrategyDetail,
  draft: EditorDraft,
): boolean {
  return (
    detail.name === draft.name &&
    (detail.description ?? "") === draft.description &&
    detail.draft.source_code === draft.sourceCode &&
    canonicalJson(detail.draft.parameter_schema) ===
      canonicalJson(parseJsonObject(draft.parameterSchema, "参数 Schema")) &&
    canonicalJson(detail.draft.default_parameters) ===
      canonicalJson(parseJsonObject(draft.defaultParameters, "默认参数"))
  );
}

function sourceLineCount(source: string): number {
  return source.length === 0 ? 1 : source.split("\n").length;
}

function validationFromError(
  error: StrategyApiError,
  detail: StrategyDetail | null,
): StrategyValidationResult | null {
  if (error.issues.length === 0 || !detail) return null;
  return {
    valid: false,
    draft_version: detail.draft.version,
    source_hash: detail.draft.source_hash,
    issues: error.issues,
  };
}

function statusText(validation: StrategyValidationResult | null): string {
  if (!validation) return "尚未校验当前草稿";
  return validation.valid
    ? `校验通过 · 草稿 v${validation.draft_version}`
    : `发现 ${validation.issues.length} 个问题 · 草稿 v${validation.draft_version}`;
}

export function StrategiesPage() {
  const { strategyId } = useParams<{ strategyId?: string }>();
  const navigate = useNavigate();
  const { logout } = useAuth();

  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const [detail, setDetail] = useState<StrategyDetail | null>(null);
  const [revisions, setRevisions] = useState<StrategyRevisionSummary[]>([]);
  const [draft, setDraft] = useState<EditorDraft | null>(null);
  const [validation, setValidation] = useState<StrategyValidationResult | null>(
    null,
  );
  const [revisionPreview, setRevisionPreview] =
    useState<StrategyRevision | null>(null);
  const [createDraft, setCreateDraft] =
    useState<CreateDraft>(EMPTY_CREATE_DRAFT);
  const [createOpen, setCreateOpen] = useState(false);
  const [revisionLoading, setRevisionLoading] = useState(false);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [search, setSearch] = useState(() => sessionValue("search", ""));
  const detailRequestSequence = useRef(0);
  const revisionRequestSequence = useRef(0);

  const isArchived = detail?.state === "archived";
  const isDirty = Boolean(
    detail && draft && !isSameEditorDraftSafe(detail, draft),
  );
  const busy = saving || validating || publishing || archiving || refreshing;
  const [file, setFile] = useState<
    "sourceCode" | "defaultParameters" | "description"
  >("sourceCode");
  const [panel, setPanel] = useState("parameters");
  const [filter, setFilter] = useState(() => sessionValue("filter", "all"));
  const [overlay, setOverlay] = useState("");
  const [explorer, setExplorer] = useState(
      () => sessionValue("explorer", "true") === "true",
    ),
    [contextOpen, setContextOpen] = useState(
      () => sessionValue("context", "true") === "true",
    );
  const [diagnostics, setDiagnostics] = useState(false);
  const [sizes, setSizes] = useState(() => {
    try {
      return {
        ...{ left: 228, right: 320, bottom: 180 },
        ...JSON.parse(sessionStorage.getItem("qfs-sizes") || "{}"),
      };
    } catch {
      return { left: 228, right: 320, bottom: 180 };
    }
  });
  const [narrow, setNarrow] = useState(window.innerWidth < 1200);
  const [cursor, setCursor] = useState({ line: 1, column: 1 });
  const editorRef = useRef<HTMLTextAreaElement>(null),
    linesRef = useRef<HTMLPreElement>(null);
  const lock = useRef(false);
  const guardRef = useRef({ dirty: false, busy: false });
  guardRef.current = {
    dirty:
      isDirty ||
      Boolean(
        createOpen &&
          (createDraft.name ||
            createDraft.description ||
            createDraft.template !== "hold"),
      ),
    busy,
  };
  function canLeave() {
    if (guardRef.current.busy) {
      setNotice("操作进行中，请稍后再离开。");
      return false;
    }
    return (
      !guardRef.current.dirty ||
      window.confirm("当前草稿尚未保存，确定放弃修改并离开吗？")
    );
  }
  useEffect(() => {
    try {
      sessionStorage.setItem("qfs-sizes", JSON.stringify(sizes));
    } catch {
      /* Storage is optional. */
    }
  }, [sizes]);
  useEffect(() => {
    const resize = () => setNarrow(window.innerWidth < 1200);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const workbenchRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = workbenchRef.current;
    if (!el) return;
    const resize = new ResizeObserver(([entry]) =>
      setNarrow(
        entry.contentRect.width < 360 + sizes.left + sizes.right + 16 ||
          window.innerWidth < 900,
      ),
    );
    resize.observe(el);
    return () => resize.disconnect();
  }, [sizes.left, sizes.right]);
  useEffect(() => {
    try {
      for (const [k, v] of Object.entries({
        search,
        filter,
        explorer,
        context: contextOpen,
      }))
        sessionStorage.setItem("qfs-" + k, String(v));
    } catch {
      /* Storage is optional. */
    }
  }, [search, filter, explorer, contextOpen]);
  const explorerVisible = narrow ? overlay === "explorer" : explorer,
    contextVisible = narrow ? overlay === "context" : contextOpen;
  const blocker = useBlocker(() => !canLeave());
  useEffect(() => {
    if (blocker.state === "blocked") blocker.reset();
  }, [blocker]);
  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (guardRef.current.dirty || guardRef.current.busy) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", unload);
    return () => window.removeEventListener("beforeunload", unload);
  }, []);
  useEffect(() => {
    const key = (e: globalThis.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (!createOpen) void handleSave();
      }
      if (e.key === "Escape" && !document.querySelector("dialog[open]")) {
        if (narrow) {
          setOverlay("");
        } else setDiagnostics(false);
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  const visibleStrategies = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return strategies.filter(
      (item) =>
        (filter === "all" || item.state === filter) &&
        `${item.name} ${item.description ?? ""} ${item.id}`
          .toLocaleLowerCase()
          .includes(query),
    );
  }, [search, strategies, filter]);

  const handleApiError = useCallback(
    (caught: unknown, fallback: string) => {
      if (caught instanceof StrategyApiError && caught.status === 401) {
        logout();
        navigate("/login", { replace: true });
        return;
      }
      setError(
        caught instanceof StrategyApiError && caught.status === 409
          ? `版本冲突：${caught.message}。本地输入已保留，请复制需要保留的内容后刷新核对服务端版本。`
          : caught instanceof Error
            ? caught.message
            : fallback,
      );
    },
    [logout, navigate],
  );

  const loadStrategies = useCallback(
    async (background = false) => {
      if (background) setRefreshing(true);
      else setLoadingList(true);
      setError(null);
      try {
        const next: StrategySummary[] = [];
        for (let offset = 0; ; offset += 100) {
          const batch = await listStrategies(true, offset, 100);
          next.push(...batch);
          if (batch.length < 100) break;
        }
        setStrategies(next);
        if (!strategyId && next.length > 0) {
          navigate(`/admin/strategies/${next[0].id}`, { replace: true });
        } else if (strategyId && next.length === 0) {
          navigate("/admin/strategies", { replace: true });
        }
      } catch (caught) {
        handleApiError(caught, "策略列表加载失败。");
      } finally {
        setLoadingList(false);
        setRefreshing(false);
      }
    },
    [handleApiError, navigate, strategyId],
  );

  const loadDetail = useCallback(
    async (id: string, background = false) => {
      const requestSequence = ++detailRequestSequence.current;
      revisionRequestSequence.current += 1;
      setRevisionPreview(null);
      setFile("sourceCode");
      if (!background) {
        setLoadingDetail(true);
        // Do not render one strategy's private source while another route is
        // loading, even briefly. The selected record owns the editor surface.
        setDetail(null);
        setDraft(null);
        setRevisions([]);
        setValidation(null);
      }
      setError(null);
      try {
        const [nextDetail, nextRevisions] = await Promise.all([
          getStrategy(id),
          listStrategyRevisions(id),
        ]);
        if (requestSequence !== detailRequestSequence.current) return;
        setDetail(nextDetail);
        setDraft(editorDraftFromDetail(nextDetail));
        setRevisions(nextRevisions);
        setValidation(null);
      } catch (caught) {
        if (requestSequence === detailRequestSequence.current) {
          handleApiError(caught, "策略详情加载失败。");
        }
      } finally {
        if (requestSequence === detailRequestSequence.current) {
          setLoadingDetail(false);
        }
      }
    },
    [handleApiError],
  );

  useEffect(() => {
    void loadStrategies();
  }, [loadStrategies]);

  useEffect(() => {
    if (strategyId) {
      void loadDetail(strategyId);
    } else {
      // Invalidate an in-flight detail request before clearing the editor.
      // This prevents an older response from rendering private source under a
      // newer route when the user switches strategies quickly.
      detailRequestSequence.current += 1;
      revisionRequestSequence.current += 1;
      setDetail(null);
      setDraft(null);
      setRevisions([]);
      setValidation(null);
    }
  }, [loadDetail, strategyId]);

  function selectStrategy(id: string) {
    if (id === strategyId) return;
    navigate(`/admin/strategies/${id}`);
  }

  function updateDraftField<K extends keyof EditorDraft>(
    field: K,
    value: EditorDraft[K],
  ) {
    setDraft((current) => (current ? { ...current, [field]: value } : current));
    setNotice(null);
  }

  async function persistEditor(): Promise<StrategyDetail> {
    if (!detail || !draft) throw new Error("请先选择一个策略。");
    const parameterSchema = parseJsonObject(
      draft.parameterSchema,
      "参数 Schema",
    );
    const defaultParameters = parseJsonObject(
      draft.defaultParameters,
      "默认参数",
    );
    let nextDetail = detail;

    // Metadata and source use independent optimistic-lock versions in the API.
    // Persist them sequentially so a successful metadata save is never silently
    // discarded while the editor still reports the precise draft-save conflict.
    const metadataChanged =
      detail.name !== draft.name ||
      (detail.description ?? "") !== draft.description;
    if (metadataChanged) {
      const updated = await updateStrategyMetadata(detail.id, {
        version: nextDetail.version,
        name: draft.name.trim(),
        description: draft.description.trim() || null,
      });
      nextDetail = { ...nextDetail, ...updated };
      setDetail(nextDetail);
      setStrategies((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
    }

    const draftChanged =
      detail.draft.source_code !== draft.sourceCode ||
      canonicalJson(detail.draft.parameter_schema) !==
        canonicalJson(parameterSchema) ||
      canonicalJson(detail.draft.default_parameters) !==
        canonicalJson(defaultParameters);
    if (draftChanged) {
      const saved = await saveStrategyDraft(detail.id, {
        version: nextDetail.draft.version,
        source_code: draft.sourceCode,
        parameter_schema: parameterSchema,
        default_parameters: defaultParameters,
      });
      // The server compares source and parameter contracts with the published
      // revision. Refresh that projection after a save, including a revert.
      nextDetail = { ...nextDetail, draft: saved };
      setDetail(nextDetail);
      nextDetail = await getStrategy(detail.id);
    }

    setDetail(nextDetail);
    setDraft(editorDraftFromDetail(nextDetail));
    setStrategies((current) =>
      current.map((item) =>
        item.id === nextDetail.id ? { ...item, ...nextDetail } : item,
      ),
    );
    return nextDetail;
  }

  async function handleSave(event?: FormEvent) {
    event?.preventDefault();
    if (!detail || !draft || busy || lock.current || isArchived) return;
    lock.current = true;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await persistEditor();
      setNotice("草稿已保存。");
    } catch (caught) {
      handleApiError(caught, "策略草稿保存失败。");
    } finally {
      lock.current = false;
      setSaving(false);
    }
  }

  async function validateCurrentDraft(): Promise<StrategyValidationResult | null> {
    if (!detail || !draft) return null;
    // Validation operates on the stored draft only. Saving first makes the
    // result unambiguously describe the source currently visible in the editor.
    const current = isDirty ? await persistEditor() : detail;
    const result = await validateStrategy(current.id);
    setValidation(result);
    return result;
  }

  async function handleValidate() {
    if (!detail || busy || lock.current || isArchived) return;
    lock.current = true;
    setDiagnostics(true);
    setValidating(true);
    setError(null);
    setNotice(null);
    try {
      const result = await validateCurrentDraft();
      if (result?.valid) setNotice("静态校验通过，可以发布当前草稿版本。");
    } catch (caught) {
      handleApiError(caught, "策略校验失败。");
    } finally {
      lock.current = false;
      setValidating(false);
    }
  }

  async function handlePublish() {
    if (!detail || busy || lock.current || isArchived) return;
    lock.current = true;
    setPublishing(true);
    setError(null);
    setNotice(null);
    try {
      const current = isDirty ? await persistEditor() : detail;
      const result = await validateStrategy(current.id);
      setValidation(result);
      if (!result.valid) {
        setDiagnostics(true);
        setError("当前草稿未通过校验，请先处理下方问题。");
        return;
      }
      const revision = await publishStrategy(current.id, current.draft.version);
      setRevisions((currentRevisions) => [
        {
          id: revision.id,
          revision_number: revision.revision_number,
          source_hash: revision.source_hash,
          runtime_manifest: revision.runtime_manifest,
          published_at: revision.published_at,
        },
        ...currentRevisions,
      ]);
      await loadDetail(current.id, true);
      setValidation(result);
      setNotice(
        `已发布策略版本 v${revision.revision_number}。发布版本不可修改。`,
      );
    } catch (caught) {
      if (caught instanceof StrategyApiError) {
        const failedValidation = validationFromError(caught, detail);
        if (failedValidation) setValidation(failedValidation);
      }
      handleApiError(caught, "策略发布失败。");
    } finally {
      lock.current = false;
      setPublishing(false);
    }
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (lock.current || busy) return;
    if (
      isDirty &&
      !window.confirm("创建后将离开当前策略，确定放弃当前未保存草稿吗？")
    )
      return;
    if (!createDraft.name.trim()) {
      setError("请填写策略名称。");
      return;
    }
    lock.current = true;
    setSaving(true);
    setError(null);
    try {
      const created = await createStrategy({
        name: createDraft.name.trim(),
        description: createDraft.description.trim() || undefined,
        source_code: TEMPLATES[createDraft.template].source,
        parameter_schema: TEMPLATES[createDraft.template].schema,
        default_parameters: TEMPLATES[createDraft.template].parameters,
      });
      setCreateOpen(false);
      setCreateDraft(EMPTY_CREATE_DRAFT);
      setStrategies((current) => [
        created,
        ...current.filter((item) => item.id !== created.id),
      ]);
      guardRef.current = { dirty: false, busy: false };
      navigate(`/admin/strategies/${created.id}`);
      setNotice("策略已创建，当前是未发布草稿。");
    } catch (caught) {
      handleApiError(caught, "策略创建失败。");
    } finally {
      lock.current = false;
      setSaving(false);
    }
  }

  async function handleArchive() {
    if (!detail || busy || lock.current || isArchived) return;
    if (isDirty) {
      setError("请先保存草稿或刷新放弃修改，再归档策略。");
      return;
    }
    if (
      !window.confirm(
        "归档后将不能继续编辑或发布，但版本历史会保留。确定归档吗？",
      )
    )
      return;
    lock.current = true;
    setArchiving(true);
    setError(null);
    try {
      await archiveStrategy(detail);
      setStrategies((current) =>
        current.map((item) =>
          item.id === detail.id ? { ...item, state: "archived" } : item,
        ),
      );
      await loadDetail(detail.id, true);
      setNotice("策略已归档，源码和版本历史仍保留在数据库中。");
    } catch (caught) {
      handleApiError(caught, "策略归档失败。");
    } finally {
      lock.current = false;
      setArchiving(false);
    }
  }

  async function openRevision(revision: StrategyRevisionSummary) {
    if (!detail) return;
    const requestSequence = ++revisionRequestSequence.current;
    setRevisionLoading(true);
    setError(null);
    try {
      const nextRevision = await getStrategyRevision(
        detail.id,
        revision.revision_number,
      );
      if (requestSequence === revisionRequestSequence.current) {
        setRevisionPreview(nextRevision);
      }
    } catch (caught) {
      if (requestSequence === revisionRequestSequence.current) {
        handleApiError(caught, "策略版本加载失败。");
      }
    } finally {
      if (requestSequence === revisionRequestSequence.current) {
        setRevisionLoading(false);
      }
    }
  }

  async function refreshPage() {
    if (!canLeave()) return;
    setRefreshing(true);
    try {
      await loadStrategies(true);
      if (strategyId) await loadDetail(strategyId, true);
    } finally {
      setRefreshing(false);
    }
  }

  const currentRevisionNumber =
    detail?.current_revision?.revision_number ?? null;

  const stale = Boolean(
    validation &&
      (isDirty || validation.draft_version !== detail?.draft.version),
  );
  const checkLabel = stale ? "检查已过期" : statusText(validation);
  const editorValue = draft?.[file] || "";
  function closeCreate() {
    if (busy) return;
    if (
      (createDraft.name ||
        createDraft.description ||
        createDraft.template !== "hold") &&
      !window.confirm("放弃尚未创建的策略信息吗？")
    )
      return;
    setCreateOpen(false);
    setCreateDraft(EMPTY_CREATE_DRAFT);
  }
  function showCreate() {
    if (busy) return;
    setCreateOpen(true);
  }
  function updateCursor() {
    const e = editorRef.current;
    if (!e) return;
    const before = e.value.slice(0, e.selectionStart);
    setCursor({
      line: before.split("\n").length,
      column: before.length - before.lastIndexOf("\n"),
    });
  }
  function formatSchema() {
    try {
      updateDraftField(
        "parameterSchema",
        prettyJson(
          parseJsonObject(draft?.parameterSchema || "", "参数 Schema"),
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }
  let parameters: Record<string, unknown> | null = null;
  try {
    parameters = parseJsonObject(draft?.defaultParameters || "", "默认参数");
  } catch {
    /* Keep malformed JSON in the editor. */
  }
  const controlsDisabled =
    busy || Boolean(isArchived) || !draft || detail?.id !== strategyId;
  return (
    <OverviewShell
      title="策略工作台"
      section="RESEARCH / 01"
      className="qfs-root"
      workspace
      beforeNavigate={() => {
        const ok = canLeave();
        if (ok) guardRef.current = { dirty: false, busy: false };
        return ok;
      }}
    >
      <section className="qfs-page" aria-labelledby="strategies-title">
        <header className="qfs-heading">
          <div>
            <small>PRIVATE STRATEGY</small>
            <h1 id="strategies-title">策略工作台</h1>
            <p>编辑策略代码、参数与版本。</p>
          </div>
          <div className="qfs-actions">
            <button
              aria-expanded={explorerVisible}
              onClick={() => {
                if (narrow)
                  setOverlay(overlay === "explorer" ? "" : "explorer");
                else setExplorer(!explorer);
              }}
            >
              策略目录
            </button>
            <button
              aria-expanded={contextVisible}
              onClick={() => {
                if (narrow) setOverlay(overlay === "context" ? "" : "context");
                else setContextOpen(!contextOpen);
              }}
            >
              参数面板
            </button>
            <button disabled={busy} onClick={showCreate}>
              <Plus />
              新建策略
            </button>
            <button
              className="qfs-primary"
              disabled={controlsDisabled}
              onClick={() => void handlePublish()}
            >
              <Rocket />
              {publishing ? "发布中…" : "发布版本"}
            </button>
          </div>
        </header>
        {error && (
          <div className="qfs-message qfs-error" role="alert">
            {error}
            <button onClick={() => setError(null)} aria-label="关闭错误">
              <X />
            </button>
          </div>
        )}
        {notice && (
          <div className="qfs-message" role="status">
            {notice}
            <button onClick={() => setNotice(null)} aria-label="关闭提示">
              <X />
            </button>
          </div>
        )}
        <div
          ref={workbenchRef}
          className={`qfs-workbench ${narrow ? "qfs-narrow" : ""}`}
        >
          {narrow && (explorerVisible || contextVisible) && (
            <button
              className="qfs-scrim"
              aria-label="关闭辅助面板"
              onClick={() => setOverlay("")}
            />
          )}
          {explorerVisible && (
            <>
              <aside
                className="qfs-explorer"
                style={{ width: sizes.left }}
                aria-label="策略目录"
              >
                <header>
                  <strong>策略目录</strong>
                  <span>{visibleStrategies.length} 项</span>
                  <button
                    disabled={busy}
                    onClick={() => void refreshPage()}
                    aria-label="刷新策略"
                  >
                    <RefreshCw />
                  </button>
                </header>
                <div className="qfs-search">
                  <input
                    aria-label="搜索策略"
                    placeholder="搜索名称、说明或 ID"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                  <div className="qfs-filters">
                    {[
                      ["all", "全部"],
                      ["active", "使用中"],
                      ["archived", "已归档"],
                    ].map(([key, label]) => (
                      <button
                        key={key}
                        aria-pressed={filter === key}
                        onClick={() => setFilter(key)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="qfs-list">
                  {loadingList ? (
                    <p role="status">加载策略…</p>
                  ) : !visibleStrategies.length ? (
                    <p>暂无匹配策略</p>
                  ) : (
                    visibleStrategies.map((item) => (
                      <button
                        disabled={busy}
                        key={item.id}
                        aria-current={
                          item.id === strategyId ? "true" : undefined
                        }
                        className={item.id === strategyId ? "qfs-selected" : ""}
                        onClick={() => {
                          selectStrategy(item.id);
                          if (narrow) setOverlay("");
                        }}
                      >
                        <strong>{item.name}</strong>
                        <span className="qfs-badge">
                          {item.state === "archived"
                            ? "已归档"
                            : item.current_revision_id
                              ? "已发布"
                              : "未发布"}
                        </span>
                        <small>{item.description || "暂无说明"}</small>
                        <small>{formatTimestamp(item.updated_at)}</small>
                      </button>
                    ))
                  )}
                </div>
              </aside>
              {!narrow && (
                <ResizeHandle
                  label="策略目录宽度"
                  value={sizes.left}
                  min={196}
                  max={320}
                  onChange={(left) => setSizes({ ...sizes, left })}
                />
              )}
            </>
          )}
          <section className="qfs-editor-panel" aria-label="策略编辑区">
            {loadingDetail || (detail && detail.id !== strategyId) ? (
              <div className="qfs-empty" role="status">
                加载策略内容…
              </div>
            ) : !detail || !draft ? (
              <div className="qfs-empty">
                <Code2 />
                <h2>选择或创建策略</h2>
                <p>源码、参数与版本将在这里显示。</p>
                <button onClick={showCreate}>新建策略</button>
              </div>
            ) : (
              <>
                <header className="qfs-object">
                  <div>
                    <strong>{detail.name}</strong>
                    <span className="qfs-badge">
                      {stateLabel(detail, stale ? null : validation, isDirty)}
                      {currentRevisionNumber
                        ? ` · v${currentRevisionNumber}`
                        : ""}
                    </span>
                    <small title={detail.id}>{detail.id}</small>
                  </div>
                  <div className="qfs-actions">
                    <button
                      disabled={controlsDisabled}
                      onClick={() => void handleValidate()}
                    >
                      <ShieldCheck />
                      {validating ? "检查中…" : "静态检查"}
                    </button>
                    <button
                      disabled={controlsDisabled}
                      onClick={() => void handleSave()}
                    >
                      <Save />
                      {saving ? "保存中…" : "保存草稿"}
                    </button>
                    <button
                      disabled={busy || !currentRevisionNumber}
                      title={
                        !currentRevisionNumber
                          ? "请先发布策略"
                          : `使用已发布 v${currentRevisionNumber}`
                      }
                      onClick={() =>
                        navigate(`/admin/strategies/${detail.id}/backtests`)
                      }
                    >
                      <ExternalLink />
                      创建回测
                    </button>
                  </div>
                </header>
                {(isDirty || detail.draft_changed_since_revision) &&
                  currentRevisionNumber && (
                    <div className="qfs-version-note">
                      创建回测使用已发布 v{currentRevisionNumber}
                      ，不包含未发布修改。
                    </div>
                  )}
                {!currentRevisionNumber && (
                  <div className="qfs-version-note">
                    当前策略尚未发布，发布后可以创建回测。
                  </div>
                )}
                <div
                  className="qfs-file-tabs"
                  role="tablist"
                  aria-label="草稿内容"
                >
                  {(
                    [
                      ["sourceCode", "strategy.py"],
                      ["defaultParameters", "params.json"],
                      ["description", "策略说明"],
                    ] as const
                  ).map(([key, label]) => (
                    <button
                      key={key}
                      role="tab"
                      aria-selected={file === key}
                      onClick={() => {
                        setFile(key);
                        setCursor({ line: 1, column: 1 });
                      }}
                    >
                      {label}
                    </button>
                  ))}
                  <span>
                    {isDirty ? "未保存" : isArchived ? "只读" : "已保存"} ·
                    UTF-8
                  </span>
                </div>
                {file === "description" && (
                  <div className="qfs-description-head">
                    <label>
                      策略名称
                      <input
                        maxLength={100}
                        disabled={controlsDisabled}
                        value={draft.name}
                        onChange={(e) =>
                          updateDraftField("name", e.target.value)
                        }
                      />
                    </label>
                    <p>策略说明不随发布版本冻结。</p>
                  </div>
                )}
                <div className="qfs-code" role="tabpanel">
                  <pre ref={linesRef} aria-hidden="true">
                    {Array.from(
                      { length: sourceLineCount(editorValue) },
                      (_, i) => i + 1,
                    ).join("\n")}
                  </pre>
                  <textarea
                    ref={editorRef}
                    aria-label={
                      file === "sourceCode"
                        ? "策略源码"
                        : file === "defaultParameters"
                          ? "默认参数 JSON"
                          : "策略说明"
                    }
                    value={editorValue}
                    readOnly={controlsDisabled}
                    maxLength={file === "description" ? 10000 : undefined}
                    wrap="off"
                    spellCheck={false}
                    onChange={(e) => {
                      updateDraftField(file, e.target.value);
                      updateCursor();
                    }}
                    onClick={updateCursor}
                    onKeyUp={updateCursor}
                    onScroll={(e) => {
                      if (linesRef.current)
                        linesRef.current.scrollTop = e.currentTarget.scrollTop;
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Tab" && !controlsDisabled) {
                        e.preventDefault();
                        const t = e.currentTarget,
                          start = t.selectionStart,
                          end = t.selectionEnd;
                        updateDraftField(
                          file,
                          editorValue.slice(0, start) +
                            "    " +
                            editorValue.slice(end),
                        );
                        requestAnimationFrame(() => {
                          t.selectionStart = t.selectionEnd = start + 4;
                          updateCursor();
                        });
                      }
                    }}
                  />
                </div>
                <footer className="qfs-status">
                  <button
                    onClick={() => setDiagnostics(!diagnostics)}
                    aria-expanded={diagnostics}
                  >
                    {checkLabel}
                  </button>
                  <span>
                    Ln {cursor.line}, Col {cursor.column}　
                    {file === "sourceCode"
                      ? "Python"
                      : file === "defaultParameters"
                        ? "JSON"
                        : "文本"}{" "}
                    · Spaces: 4
                  </span>
                </footer>
                {diagnostics && (
                  <>
                    <ResizeHandle
                      horizontal
                      reverse
                      label="诊断面板高度"
                      value={sizes.bottom}
                      min={120}
                      max={320}
                      onChange={(bottom) => setSizes({ ...sizes, bottom })}
                    />
                    <section
                      className="qfs-diagnostics"
                      style={{ height: sizes.bottom }}
                    >
                      <header>
                        <strong>静态检查 · {checkLabel}</strong>
                        <button
                          onClick={() => setDiagnostics(false)}
                          aria-label="关闭诊断"
                        >
                          <X />
                        </button>
                      </header>
                      <p>检查 Python 语法、入口签名与参数契约，不执行策略。</p>
                      {validation?.issues.map((issue, i) => (
                        <button
                          className="qfs-issue"
                          key={i}
                          onClick={() => {
                            setFile("sourceCode");
                            requestAnimationFrame(() => {
                              const e = editorRef.current;
                              if (!e) return;
                              const pos =
                                draft.sourceCode
                                  .split("\n")
                                  .slice(0, Math.max(0, (issue.line || 1) - 1))
                                  .join("\n").length +
                                (issue.line && issue.line > 1 ? 1 : 0);
                              e.focus();
                              e.setSelectionRange(pos, pos);
                              e.scrollTop = Math.max(
                                0,
                                ((issue.line || 1) - 3) * 22,
                              );
                              updateCursor();
                            });
                          }}
                        >
                          {issueLocation(issue)} · {issue.message}
                        </button>
                      ))}
                    </section>
                  </>
                )}
              </>
            )}
          </section>
          {contextVisible && (
            <>
              {!narrow && (
                <ResizeHandle
                  reverse
                  label="上下文面板宽度"
                  value={sizes.right}
                  min={280}
                  max={420}
                  onChange={(right) => setSizes({ ...sizes, right })}
                />
              )}
              <aside
                className="qfs-context"
                style={{ width: sizes.right }}
                aria-label="策略上下文"
              >
                <header>
                  <strong>策略上下文</strong>
                  <span>当前对象</span>
                </header>
                <div
                  className="qfs-context-tabs"
                  role="tablist"
                  aria-label="上下文"
                >
                  {[
                    ["parameters", "参数"],
                    ["check", "检查"],
                    ["history", "版本"],
                    ["links", "关联"],
                  ].map(([key, label]) => (
                    <button
                      role="tab"
                      aria-selected={panel === key}
                      key={key}
                      onClick={() => setPanel(key)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <div className="qfs-context-body" role="tabpanel">
                  {!detail || detail.id !== strategyId ? (
                    <p>请选择策略</p>
                  ) : panel === "parameters" ? (
                    <>
                      <h3>默认参数</h3>
                      {parameters && Object.keys(parameters).length ? (
                        Object.entries(parameters).map(([key, value]) => (
                          <label className="qfs-parameter" key={key}>
                            <span>{key}</span>
                            {typeof value === "boolean" ? (
                              <input
                                type="checkbox"
                                checked={value}
                                disabled={controlsDisabled}
                                onChange={(e) =>
                                  updateDraftField(
                                    "defaultParameters",
                                    prettyJson({
                                      ...parameters,
                                      [key]: e.target.checked,
                                    }),
                                  )
                                }
                              />
                            ) : typeof value === "string" ? (
                              <input
                                value={value}
                                disabled={controlsDisabled}
                                onChange={(e) =>
                                  updateDraftField(
                                    "defaultParameters",
                                    prettyJson({
                                      ...parameters,
                                      [key]: e.target.value,
                                    }),
                                  )
                                }
                              />
                            ) : typeof value === "number" ? (
                              <input
                                type="number"
                                value={value}
                                disabled={controlsDisabled}
                                onChange={(e) => {
                                  if (
                                    e.target.value !== "" &&
                                    Number.isFinite(e.target.valueAsNumber)
                                  )
                                    updateDraftField(
                                      "defaultParameters",
                                      prettyJson({
                                        ...parameters,
                                        [key]: e.target.valueAsNumber,
                                      }),
                                    );
                                }}
                              />
                            ) : (
                              <button
                                onClick={() => setFile("defaultParameters")}
                              >
                                在 JSON 中编辑
                              </button>
                            )}
                          </label>
                        ))
                      ) : (
                        <p className="qfs-muted">
                          {parameters
                            ? "暂无默认参数，可在 params.json 中添加。"
                            : "JSON 尚未有效，请在 params.json 中修正。"}
                        </p>
                      )}
                      <div className="qfs-section-head">
                        <h3>参数 Schema</h3>
                        <button
                          disabled={controlsDisabled}
                          onClick={formatSchema}
                        >
                          格式化
                        </button>
                      </div>
                      <textarea
                        className="qfs-schema"
                        aria-label="参数 Schema"
                        value={draft?.parameterSchema || ""}
                        readOnly={controlsDisabled}
                        spellCheck={false}
                        onChange={(e) =>
                          updateDraftField("parameterSchema", e.target.value)
                        }
                      />
                      <p className="qfs-muted">
                        复杂结构保留在 JSON 中编辑，保存时校验格式。
                      </p>
                    </>
                  ) : panel === "check" ? (
                    <>
                      <h3>{checkLabel}</h3>
                      <p>语法、入口签名、默认参数与 Schema 契约。</p>
                      <p className="qfs-muted">
                        静态检查不代表策略运行或回测已通过。
                      </p>
                      <button onClick={() => setDiagnostics(true)}>
                        查看检查详情
                      </button>
                    </>
                  ) : panel === "history" ? (
                    <>
                      <h3>发布版本</h3>
                      {revisionLoading && <p role="status">加载版本…</p>}
                      {!revisions.length ? (
                        <p>暂无发布版本</p>
                      ) : (
                        revisions.map((r) => (
                          <button
                            disabled={busy}
                            className="qfs-record"
                            key={r.id}
                            onClick={() => void openRevision(r)}
                          >
                            <strong>
                              v{r.revision_number}
                              {r.id === detail.current_revision_id
                                ? " · 当前发布版本"
                                : ""}
                            </strong>
                            <small>{formatTimestamp(r.published_at)}</small>
                            <code>{shortHash(r.source_hash)}</code>
                          </button>
                        ))
                      )}
                      <div className="qfs-record">
                        <strong>当前草稿</strong>
                        <small>
                          最后保存 {formatTimestamp(detail.draft.updated_at)}
                        </small>
                      </div>
                      <button
                        disabled={controlsDisabled}
                        onClick={() => void handleArchive()}
                      >
                        <Archive />
                        归档策略
                      </button>
                    </>
                  ) : (
                    <RecentRuns strategyId={detail.id} />
                  )}
                </div>
              </aside>
            </>
          )}
        </div>
      </section>
      {createOpen && (
        <Drawer title="创建策略" onClose={closeCreate}>
          <form onSubmit={handleCreate}>
            <p>填写基本信息并选择初始模板，创建后进入编辑器。</p>
            {error && (
              <p role="alert" className="qfs-error">
                {error}
              </p>
            )}
            <label>
              策略名称
              <input
                autoFocus
                required
                maxLength={100}
                disabled={busy}
                value={createDraft.name}
                onChange={(e) =>
                  setCreateDraft({ ...createDraft, name: e.target.value })
                }
              />
            </label>
            <label>
              初始模板
              <select
                disabled={busy}
                value={createDraft.template}
                onChange={(e) =>
                  setCreateDraft({
                    ...createDraft,
                    template: e.target.value as TemplateKey,
                  })
                }
              >
                {Object.entries(TEMPLATES).map(([key, t]) => (
                  <option value={key} key={key}>
                    {t.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              说明（可选）
              <textarea
                maxLength={10000}
                disabled={busy}
                value={createDraft.description}
                onChange={(e) =>
                  setCreateDraft({
                    ...createDraft,
                    description: e.target.value,
                  })
                }
              />
            </label>
            <p className="qfs-muted">
              {createDraft.template === "blank"
                ? "空白模板需要实现策略逻辑后再发布。"
                : createDraft.template === "rotation"
                  ? "使用原始收盘价计算动量；回测前核对回看窗口与数据范围。"
                  : "保留当前持仓，不产生新的交易意图。"}
            </p>
            <footer>
              <button type="button" disabled={busy} onClick={closeCreate}>
                取消
              </button>
              <button
                className="qfs-primary"
                disabled={busy || !createDraft.name.trim()}
              >
                {busy ? "创建中…" : "创建并进入编辑器"}
              </button>
            </footer>
          </form>
        </Drawer>
      )}
      {revisionPreview && (
        <Drawer
          title={`发布版本 v${revisionPreview.revision_number}`}
          onClose={() => setRevisionPreview(null)}
        >
          <p>
            发布于 {formatTimestamp(revisionPreview.published_at)} · 只读快照
          </p>
          <h3>strategy.py</h3>
          <pre>{revisionPreview.source_code}</pre>
          <h3>参数 Schema</h3>
          <pre>{prettyJson(revisionPreview.parameter_schema)}</pre>
          <h3>默认参数</h3>
          <pre>{prettyJson(revisionPreview.default_parameters)}</pre>
        </Drawer>
      )}
    </OverviewShell>
  );
}
function isSameEditorDraftSafe(detail: StrategyDetail, draft: EditorDraft) {
  try {
    return isSameEditorDraft(detail, draft);
  } catch {
    return false;
  }
}

function sessionValue(key: string, fallback: string) {
  try {
    return sessionStorage.getItem("qfs-" + key) ?? fallback;
  } catch {
    return fallback;
  }
}

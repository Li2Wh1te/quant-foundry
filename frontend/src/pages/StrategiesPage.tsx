import {
  Archive,
  CircleAlert,
  CircleCheck,
  Clock3,
  Code2,
  Copy,
  ExternalLink,
  FileCode2,
  GitBranch,
  History,
  LoaderCircle,
  Plus,
  RefreshCw,
  Rocket,
  Save,
  ShieldCheck,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

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
  StrategyDraft,
  StrategyRevision,
  StrategyRevisionSummary,
  StrategySummary,
  StrategyValidationIssue,
  StrategyValidationResult,
  updateStrategyMetadata,
  validateStrategy
} from "../api/strategies";
import { useAuth } from "../auth/AuthContext";

const SOURCE_TEMPLATE = `"""Private strategy entry point."""


def run(context, parameters):
    """Return one decision for the current step.

    Two modes are supported in the first version:
    - target_weights: submit the full target portfolio as
      {"mode": "target_weights", "targets": {"<instrument_id>": "0.60"}}
    - hold: submit no new trading intent with {"mode": "hold"}
    """
    return {"mode": "hold"}
`;

const EMPTY_PARAMETER_SCHEMA = {
  type: "object",
  properties: {},
  additionalProperties: false
};

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
  sourceCode: string;
};

const EMPTY_CREATE_DRAFT: CreateDraft = {
  name: "",
  description: "",
  sourceCode: SOURCE_TEMPLATE
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
    defaultParameters: prettyJson(detail.draft.default_parameters)
  };
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
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
    return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`).join(",")}}`;
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
    hour12: false
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

function stateLabel(state: StrategySummary["state"]): string {
  return state === "archived" ? "已归档" : "编辑中";
}

function isSameEditorDraft(detail: StrategyDetail, draft: EditorDraft): boolean {
  return detail.name === draft.name
    && (detail.description ?? "") === draft.description
    && detail.draft.source_code === draft.sourceCode
    && canonicalJson(detail.draft.parameter_schema) === canonicalJson(parseJsonObject(draft.parameterSchema, "参数 Schema"))
    && canonicalJson(detail.draft.default_parameters) === canonicalJson(parseJsonObject(draft.defaultParameters, "默认参数"));
}

function sourceLineCount(source: string): number {
  return source.length === 0 ? 1 : source.split("\n").length;
}

function validationFromError(error: StrategyApiError, detail: StrategyDetail | null): StrategyValidationResult | null {
  if (error.issues.length === 0 || !detail) return null;
  return {
    valid: false,
    draft_version: detail.draft.version,
    source_hash: detail.draft.source_hash,
    issues: error.issues
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
  const [validation, setValidation] = useState<StrategyValidationResult | null>(null);
  const [revisionPreview, setRevisionPreview] = useState<StrategyRevision | null>(null);
  const [createDraft, setCreateDraft] = useState<CreateDraft>(EMPTY_CREATE_DRAFT);
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
  const [search, setSearch] = useState("");
  const detailRequestSequence = useRef(0);
  const revisionRequestSequence = useRef(0);

  const selectedSummary = strategies.find((item) => item.id === strategyId) ?? null;
  const isArchived = detail?.state === "archived";
  const isDirty = Boolean(detail && draft && !isSameEditorDraftSafe(detail, draft));
  const publishedCount = strategies.filter((item) => item.current_revision_id !== null).length;
  const draftCount = strategies.length - publishedCount;

  const visibleStrategies = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (!query) return strategies;
    return strategies.filter((item) => `${item.name} ${item.description ?? ""}`.toLocaleLowerCase().includes(query));
  }, [search, strategies]);

  const handleApiError = useCallback((caught: unknown, fallback: string) => {
    if (caught instanceof StrategyApiError && caught.status === 401) {
      logout();
      navigate("/login", { replace: true });
      return;
    }
    setError(caught instanceof Error ? caught.message : fallback);
  }, [logout, navigate]);

  const loadStrategies = useCallback(async (background = false) => {
    if (background) setRefreshing(true); else setLoadingList(true);
    setError(null);
    try {
      const next = await listStrategies();
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
  }, [handleApiError, navigate, strategyId]);

  const loadDetail = useCallback(async (id: string, background = false) => {
    const requestSequence = ++detailRequestSequence.current;
    revisionRequestSequence.current += 1;
    setRevisionPreview(null);
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
        listStrategyRevisions(id)
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
  }, [handleApiError]);

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
    if (isDirty && !window.confirm("当前草稿尚未保存，确定放弃修改并切换策略吗？")) return;
    navigate(`/admin/strategies/${id}`);
  }

  function updateDraftField<K extends keyof EditorDraft>(field: K, value: EditorDraft[K]) {
    setDraft((current) => current ? { ...current, [field]: value } : current);
    setValidation(null);
    setNotice(null);
  }

  async function persistEditor(): Promise<StrategyDetail> {
    if (!detail || !draft) throw new Error("请先选择一个策略。");
    const parameterSchema = parseJsonObject(draft.parameterSchema, "参数 Schema");
    const defaultParameters = parseJsonObject(draft.defaultParameters, "默认参数");
    let nextDetail = detail;

    // Metadata and source use independent optimistic-lock versions in the API.
    // Persist them sequentially so a successful metadata save is never silently
    // discarded while the editor still reports the precise draft-save conflict.
    const metadataChanged = detail.name !== draft.name || (detail.description ?? "") !== draft.description;
    if (metadataChanged) {
      const updated = await updateStrategyMetadata(detail.id, {
        version: nextDetail.version,
        name: draft.name.trim(),
        description: draft.description.trim() || null
      });
      nextDetail = { ...nextDetail, ...updated };
      setStrategies((current) => current.map((item) => item.id === updated.id ? updated : item));
    }

    const draftChanged = detail.draft.source_code !== draft.sourceCode
      || canonicalJson(detail.draft.parameter_schema) !== canonicalJson(parameterSchema)
      || canonicalJson(detail.draft.default_parameters) !== canonicalJson(defaultParameters);
    if (draftChanged) {
      const saved = await saveStrategyDraft(detail.id, {
        version: nextDetail.draft.version,
        source_code: draft.sourceCode,
        parameter_schema: parameterSchema,
        default_parameters: defaultParameters
      });
      nextDetail = { ...nextDetail, draft: saved };
    }

    setDetail(nextDetail);
    setDraft(editorDraftFromDetail(nextDetail));
    setStrategies((current) => current.map((item) => item.id === nextDetail.id
      ? { ...item, ...nextDetail }
      : item));
    return nextDetail;
  }

  async function handleSave(event?: FormEvent) {
    event?.preventDefault();
    if (!detail || !draft || saving || isArchived) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await persistEditor();
      setNotice("草稿已保存到 PostgreSQL。");
    } catch (caught) {
      handleApiError(caught, "策略草稿保存失败。");
    } finally {
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
    if (!detail || validating || saving || isArchived) return;
    setValidating(true);
    setError(null);
    setNotice(null);
    try {
      const result = await validateCurrentDraft();
      if (result?.valid) setNotice("静态校验通过，可以发布当前草稿版本。");
    } catch (caught) {
      handleApiError(caught, "策略校验失败。");
    } finally {
      setValidating(false);
    }
  }

  async function handlePublish() {
    if (!detail || publishing || saving || validating || isArchived) return;
    setPublishing(true);
    setError(null);
    setNotice(null);
    try {
      const current = isDirty ? await persistEditor() : detail;
      const result = await validateStrategy(current.id);
      setValidation(result);
      if (!result.valid) {
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
          published_at: revision.published_at
        },
        ...currentRevisions
      ]);
      await loadDetail(current.id, true);
      setNotice(`已发布策略版本 v${revision.revision_number}。发布版本不可修改。`);
    } catch (caught) {
      if (caught instanceof StrategyApiError) {
        const failedValidation = validationFromError(caught, detail);
        if (failedValidation) setValidation(failedValidation);
      }
      handleApiError(caught, "策略发布失败。");
    } finally {
      setPublishing(false);
    }
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!createDraft.name.trim()) {
      setError("请填写策略名称。");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const created = await createStrategy({
        name: createDraft.name.trim(),
        description: createDraft.description.trim() || undefined,
        source_code: createDraft.sourceCode,
        parameter_schema: EMPTY_PARAMETER_SCHEMA,
        default_parameters: {}
      });
      setCreateOpen(false);
      setCreateDraft(EMPTY_CREATE_DRAFT);
      setStrategies((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      navigate(`/admin/strategies/${created.id}`);
      setNotice("策略已创建，当前是未发布草稿。");
    } catch (caught) {
      handleApiError(caught, "策略创建失败。");
    } finally {
      setSaving(false);
    }
  }

  async function handleArchive() {
    if (!detail || archiving || isArchived) return;
    if (!window.confirm("归档后将不能继续编辑或发布，但版本历史会保留。确定归档吗？")) return;
    setArchiving(true);
    setError(null);
    try {
      await archiveStrategy(detail);
      const next = strategies.filter((item) => item.id !== detail.id);
      setStrategies(next);
      if (next.length > 0) navigate(`/admin/strategies/${next[0].id}`, { replace: true });
      else navigate("/admin/strategies", { replace: true });
      setNotice("策略已归档，源码和版本历史仍保留在数据库中。");
    } catch (caught) {
      handleApiError(caught, "策略归档失败。");
    } finally {
      setArchiving(false);
    }
  }

  async function openRevision(revision: StrategyRevisionSummary) {
    if (!detail) return;
    const requestSequence = ++revisionRequestSequence.current;
    setRevisionLoading(true);
    setError(null);
    try {
      const nextRevision = await getStrategyRevision(detail.id, revision.revision_number);
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
    setRefreshing(true);
    try {
      await loadStrategies(true);
      if (strategyId) await loadDetail(strategyId, true);
    } finally {
      setRefreshing(false);
    }
  }

  const currentRevisionNumber = detail?.current_revision?.revision_number ?? null;

  return (
    <section className="strategies-page" aria-labelledby="strategies-title">
      <div className="page-heading strategies-page__heading">
        <div>
          <span className="workbench-eyebrow">PRIVATE STRATEGY WORKBENCH</span>
          <h2 id="strategies-title">策略工作台</h2>
          <p>在 Web 中编辑私有策略，源码直接保存到 PostgreSQL，不进入项目目录。</p>
        </div>
        <div className="strategies-page__actions">
          <button className="toolbar-button" type="button" disabled={refreshing} onClick={() => void refreshPage()}>
            <RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" />刷新
          </button>
          <button className="task-create-button" type="button" onClick={() => { setCreateOpen(true); setError(null); }}>
            <Plus aria-hidden="true" />新建策略
          </button>
        </div>
      </div>

      {error && <div className="page-error" role="alert"><CircleAlert aria-hidden="true" />{error}</div>}
      {notice && <div className="strategy-message strategy-message--success" role="status"><CircleCheck aria-hidden="true" />{notice}</div>}

      <div className="strategy-stats" aria-label="策略概览">
        <article><span>策略总数</span><strong>{loadingList ? "—" : strategies.length}</strong><small>当前部署中的私有策略</small></article>
        <article><span>已发布</span><strong>{loadingList ? "—" : publishedCount}</strong><small>至少存在一个不可变版本</small></article>
        <article><span>仅草稿</span><strong>{loadingList ? "—" : draftCount}</strong><small>尚未发布可执行版本</small></article>
        <article className="strategy-stat--focus"><span>当前版本</span><strong>{currentRevisionNumber ? `v${currentRevisionNumber}` : "—"}</strong><small>{selectedSummary?.name ?? "尚未选择策略"}</small></article>
      </div>

      <div className="strategies-layout">
        <aside className="strategy-board" aria-label="策略列表">
          <div className="strategy-board__heading">
            <div><span className="workbench-eyebrow">STRATEGIES</span><h3>私有策略</h3></div>
            <span>{strategies.length} 项</span>
          </div>
          <label className="strategy-search">
            <Code2 aria-hidden="true" />
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索策略名称…" aria-label="搜索策略名称" />
          </label>
          <div className="strategy-list">
            {loadingList ? <div className="strategy-empty"><LoaderCircle className="spin" aria-hidden="true" />正在加载策略…</div>
              : visibleStrategies.length > 0 ? visibleStrategies.map((item) => (
                <button
                  className={`strategy-list__item${item.id === strategyId ? " strategy-list__item--selected" : ""}`}
                  key={item.id}
                  type="button"
                  onClick={() => selectStrategy(item.id)}
                >
                  <span className={`strategy-list__dot${item.current_revision_id ? " strategy-list__dot--published" : ""}`} aria-hidden="true" />
                  <span className="strategy-list__copy"><strong>{item.name}</strong><small>{item.description || "尚未填写说明"}</small></span>
                  <span className="strategy-list__meta">{item.current_revision_id ? "已发布" : "草稿"}<br /><time>{formatTimestamp(item.updated_at)}</time></span>
                </button>
              )) : <div className="strategy-empty"><FileCode2 aria-hidden="true" /><strong>{search ? "没有匹配策略" : "还没有策略"}</strong><span>{search ? "换个关键词试试。" : "点击右上角新建第一条私有策略。"}</span></div>}
          </div>
        </aside>

        <main className="strategy-editor" aria-label="策略编辑器">
          {!strategyId ? <StrategyWelcome onCreate={() => setCreateOpen(true)} />
            : loadingDetail || !detail || !draft ? <div className="strategy-editor__loading"><LoaderCircle className="spin" aria-hidden="true" />正在加载策略详情…</div>
              : <>
                <div className="strategy-editor__header">
                  <div className="strategy-editor__title">
                    <span className={`strategy-state strategy-state--${detail.state}`}><i aria-hidden="true" />{stateLabel(detail.state)}</span>
                    <h3>{detail.name}</h3>
                    <p>{detail.current_revision ? `当前发布版本 v${detail.current_revision.revision_number}` : "尚未发布版本"} · 草稿 v{detail.draft.version}</p>
                  </div>
                  <div className="strategy-editor__actions">
                    <button className="toolbar-button" type="button" disabled={refreshing} onClick={() => void loadDetail(detail.id)} title="放弃本地未保存修改并重新读取"><RefreshCw aria-hidden="true" />重载</button>
                    <button className="toolbar-button" type="button" disabled={!isDirty || saving || isArchived} onClick={() => void handleSave()}><Save aria-hidden="true" />{saving ? "保存中" : "保存草稿"}</button>
                    <button className="toolbar-button toolbar-button--validate" type="button" disabled={validating || saving || isArchived} onClick={() => void handleValidate()}><ShieldCheck aria-hidden="true" />{validating ? "校验中" : "校验"}</button>
                    <button className="task-create-button" type="button" disabled={publishing || saving || validating || isArchived} onClick={() => void handlePublish()}><Rocket aria-hidden="true" />{publishing ? "发布中" : "发布版本"}</button>
                  </div>
                </div>
                <div className="strategy-editor__run-gate" role="status">
                  {detail.current_revision
                    ? <span>回测将绑定已发布版本 v{detail.current_revision.revision_number}（不可变）。{isDirty ? "当前草稿有未发布修改，回测不会使用这些修改。" : ""}</span>
                    : <span>请先发布策略，未发布草稿不能进入回测。</span>}
                </div>

                <form className="strategy-editor__form" onSubmit={(event) => void handleSave(event)}>
                  <div className="strategy-metadata-grid">
                    <label><span>策略名称</span><input value={draft.name} disabled={isArchived} onChange={(event) => updateDraftField("name", event.target.value)} maxLength={100} /></label>
                    <label><span>说明</span><input value={draft.description} disabled={isArchived} onChange={(event) => updateDraftField("description", event.target.value)} maxLength={10_000} placeholder="描述策略用途和预期行为" /></label>
                  </div>

                  <div className="strategy-code-panel">
                    <div className="strategy-panel__heading">
                      <div><span className="workbench-eyebrow">SOURCE MODULE</span><h4><FileCode2 aria-hidden="true" />strategy.py</h4></div>
                      <span>{sourceLineCount(draft.sourceCode)} 行 · {new TextEncoder().encode(draft.sourceCode).length.toLocaleString("zh-CN")} bytes</span>
                    </div>
                    <textarea
                      className="strategy-source-editor"
                      value={draft.sourceCode}
                      disabled={isArchived}
                      onChange={(event) => updateDraftField("sourceCode", event.target.value)}
                      onKeyDown={(event) => handleEditorKeyDown(event, draft.sourceCode, (value) => updateDraftField("sourceCode", value))}
                      spellCheck={false}
                      wrap="off"
                      aria-label="策略 Python 源码"
                    />
                    <div className="strategy-editor__hint"><Code2 aria-hidden="true" />必须提供同步的 <code>run(context, parameters)</code> 入口。当前校验只解析源码，不会在 API 进程中执行它。</div>
                  </div>

                  <div className="strategy-contract-grid">
                    <JsonEditor
                      label="参数 Schema"
                      eyebrow="PARAMETER CONTRACT"
                      value={draft.parameterSchema}
                      disabled={isArchived}
                      onChange={(value) => updateDraftField("parameterSchema", value)}
                      hint="顶层应为 object；用于描述策略可配置参数。"
                    />
                    <JsonEditor
                      label="默认参数"
                      eyebrow="DEFAULT VALUES"
                      value={draft.defaultParameters}
                      disabled={isArchived}
                      onChange={(value) => updateDraftField("defaultParameters", value)}
                      hint="运行任务未覆盖时使用的 JSON 对象。"
                    />
                  </div>
                </form>

                <div className="strategy-editor__footer">
                  <div className={`strategy-save-state${isDirty ? " strategy-save-state--dirty" : ""}`}><span aria-hidden="true" />{isDirty ? "有未保存修改" : `已保存 · ${formatTimestamp(detail.draft.updated_at)}`}<small>{shortHash(detail.draft.source_hash)}</small></div>
                  <div className="strategy-editor__footer-actions">
                    <Link className="toolbar-button" to="/admin/tasks"><ExternalLink aria-hidden="true" />任务调度</Link>
                    <button className="danger-text-button" type="button" disabled={archiving || isArchived} onClick={() => void handleArchive()}><Archive aria-hidden="true" />归档策略</button>
                  </div>
                </div>

                <ValidationPanel validation={validation} status={statusText(validation)} />

                <section className="strategy-revisions" aria-labelledby="strategy-revisions-title">
                  <div className="strategy-section-heading"><div><span className="workbench-eyebrow">IMMUTABLE HISTORY</span><h4 id="strategy-revisions-title"><History aria-hidden="true" />版本历史</h4></div><span>{revisions.length} 个已发布版本</span></div>
                  {revisions.length > 0 ? <div className="strategy-revision-list">{revisions.map((revision) => (
                    <button className={`strategy-revision-row${revision.revision_number === currentRevisionNumber ? " strategy-revision-row--current" : ""}`} type="button" key={revision.id} onClick={() => void openRevision(revision)}>
                      <span className="strategy-revision-row__version"><GitBranch aria-hidden="true" />v{revision.revision_number}</span>
                      <span><strong>{revision.revision_number === currentRevisionNumber ? "当前发布版本" : "已发布版本"}</strong><small>{shortHash(revision.source_hash)}</small></span>
                      <time>{formatTimestamp(revision.published_at)}</time>
                      <ExternalLink aria-hidden="true" />
                    </button>
                  ))}</div> : <div className="strategy-history-empty"><Clock3 aria-hidden="true" /><span>发布后会在这里保留不可变版本，便于审计和回溯。</span></div>}
                </section>

                <section className="strategy-scheduling-note" aria-label="调度绑定状态">
                  <div><Clock3 aria-hidden="true" /><span><strong>调度绑定</strong><small>当前阶段先完成策略编写、校验和版本发布；执行阶段将让任务调度引用指定策略版本。</small></span></div>
                  <Link to="/admin/tasks">查看任务调度 <ExternalLink aria-hidden="true" /></Link>
                </section>
              </>}
        </main>
      </div>

      {createOpen && <CreateStrategyDialog draft={createDraft} saving={saving} onChange={setCreateDraft} onClose={() => setCreateOpen(false)} onSubmit={handleCreate} />}
      {revisionPreview && <RevisionPreviewDialog revision={revisionPreview} loading={revisionLoading} onClose={() => setRevisionPreview(null)} />}
      {revisionLoading && !revisionPreview && <div className="strategy-toast"><LoaderCircle className="spin" aria-hidden="true" />正在读取版本…</div>}
    </section>
  );
}

function isSameEditorDraftSafe(detail: StrategyDetail, draft: EditorDraft): boolean {
  try {
    return isSameEditorDraft(detail, draft);
  } catch {
    // Invalid local JSON is necessarily a pending edit and should never be lost.
    return false;
  }
}

function handleEditorKeyDown(
  event: KeyboardEvent<HTMLTextAreaElement>,
  source: string,
  setSource: (value: string) => void
) {
  if (event.key !== "Tab") return;
  event.preventDefault();
  const target = event.currentTarget;
  const start = target.selectionStart;
  const end = target.selectionEnd;
  const next = `${source.slice(0, start)}    ${source.slice(end)}`;
  setSource(next);
  requestAnimationFrame(() => {
    target.selectionStart = start + 4;
    target.selectionEnd = start + 4;
  });
}

function StrategyWelcome({ onCreate }: { onCreate: () => void }) {
  return <div className="strategy-welcome"><div className="strategy-welcome__mark"><Code2 aria-hidden="true" /></div><span className="workbench-eyebrow">PRIVATE BY DESIGN</span><h3>把策略留在你的部署里</h3><p>源码和因子只保存在当前部署的 PostgreSQL。先创建一条策略，再从草稿开始编写；发布后每个版本都会被完整保留。</p><button className="task-create-button" type="button" onClick={onCreate}><Plus aria-hidden="true" />新建第一条策略</button></div>;
}

function JsonEditor({
  label,
  eyebrow,
  value,
  disabled,
  onChange,
  hint
}: {
  label: string;
  eyebrow: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
  hint: string;
}) {
  return <label className="strategy-json-editor"><div className="strategy-panel__heading"><div><span className="workbench-eyebrow">{eyebrow}</span><h4>{label}</h4></div><Copy aria-hidden="true" /></div><textarea value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)} spellCheck={false} wrap="off" aria-label={label} /><small>{hint}</small></label>;
}

function ValidationPanel({ validation, status }: { validation: StrategyValidationResult | null; status: string }) {
  return <section className={`strategy-validation${validation?.valid ? " strategy-validation--valid" : validation ? " strategy-validation--invalid" : ""}`} aria-labelledby="strategy-validation-title">
    <div className="strategy-section-heading"><div><span className="workbench-eyebrow">STATIC CHECK</span><h4 id="strategy-validation-title"><ShieldCheck aria-hidden="true" />发布前校验</h4></div><span>{status}</span></div>
    {!validation ? <div className="strategy-validation__empty"><ShieldCheck aria-hidden="true" /><span>保存草稿后点击“校验”，确认入口和参数契约可用于发布。</span></div>
      : validation.valid ? <div className="strategy-validation__result"><CircleCheck aria-hidden="true" /><span><strong>校验通过</strong><small>当前草稿满足首期静态策略契约，可以发布为不可变版本。</small></span></div>
        : <div className="strategy-issues">{validation.issues.map((issue, index) => <div className="strategy-issue" key={`${issue.code}-${index}`}><CircleAlert aria-hidden="true" /><span><strong>{issue.message}</strong><small>{issueLocation(issue)} · {issue.code}</small></span></div>)}</div>}
  </section>;
}

function CreateStrategyDialog({
  draft,
  saving,
  onChange,
  onClose,
  onSubmit
}: {
  draft: CreateDraft;
  saving: boolean;
  onChange: (draft: CreateDraft) => void;
  onClose: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  function setField<K extends keyof CreateDraft>(field: K, value: CreateDraft[K]) {
    onChange({ ...draft, [field]: value });
  }

  return <div className="prototype-modal strategy-modal-backdrop" role="presentation" onMouseDown={onClose}>
    <form className="prototype-modal__dialog strategy-create-dialog" onSubmit={onSubmit} onMouseDown={(event) => event.stopPropagation()}>
      <div className="prototype-modal__heading"><div><span>NEW PRIVATE STRATEGY</span><h3>创建策略</h3></div><button className="icon-button" type="button" onClick={onClose} aria-label="关闭创建策略对话框"><X aria-hidden="true" /></button></div>
      <p className="strategy-modal__notice"><ShieldCheck aria-hidden="true" />源码会直接写入 PostgreSQL，不会创建本地策略文件。</p>
      <label><span>策略名称</span><input autoFocus value={draft.name} onChange={(event) => setField("name", event.target.value)} placeholder="例如：ETF 趋势策略" maxLength={100} /></label>
      <label><span>说明（可选）</span><input value={draft.description} onChange={(event) => setField("description", event.target.value)} placeholder="描述策略用途" maxLength={10_000} /></label>
      <label><span>初始源码</span><textarea className="strategy-create-source" value={draft.sourceCode} onChange={(event) => setField("sourceCode", event.target.value)} spellCheck={false} wrap="off" /></label>
      <div className="prototype-modal__actions"><button className="secondary-button" type="button" onClick={onClose}>取消</button><button className="task-create-button" type="submit" disabled={saving || !draft.name.trim()}>{saving && <LoaderCircle className="spin" aria-hidden="true" />}{saving ? "创建中" : "创建并编辑"}</button></div>
    </form>
  </div>;
}

function RevisionPreviewDialog({ revision, loading, onClose }: { revision: StrategyRevision; loading: boolean; onClose: () => void }) {
  return <div className="prototype-modal strategy-modal-backdrop" role="presentation" onMouseDown={onClose}>
    <section className="prototype-modal__dialog strategy-revision-dialog" role="dialog" aria-modal="true" aria-labelledby="revision-preview-title" onMouseDown={(event) => event.stopPropagation()}>
      <div className="prototype-modal__heading"><div><span>IMMUTABLE REVISION · v{revision.revision_number}</span><h3 id="revision-preview-title">版本源码快照</h3></div><button className="icon-button" type="button" onClick={onClose} aria-label="关闭版本预览"><X aria-hidden="true" /></button></div>
      <div className="strategy-revision-dialog__facts"><span>发布于 {formatTimestamp(revision.published_at)}</span><code>{revision.source_hash}</code></div>
      <pre><code>{revision.source_code}</code></pre>
      {loading && <span className="strategy-revision-dialog__loading"><LoaderCircle className="spin" aria-hidden="true" />加载中</span>}
      <div className="prototype-modal__actions"><button className="secondary-button" type="button" onClick={onClose}>关闭</button></div>
    </section>
  </div>;
}

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { X } from "lucide-react";
import type { DataSource } from "../../api/dataSources";
import type { SchedulerTask, TaskPayload, TaskType } from "../../api/scheduler";
import { cronPreview, draftFromTask, newTaskDraft, parameterFields, parameterInput, parametersTemplate,
  payloadFromDraft, taskPatch, taskTypeLabel, validateParameters, WEEKDAYS } from "./taskDraft";
import type { TaskDraft } from "./taskDraft";

interface Props {
  task: SchedulerTask | null; types: TaskType[]; sources: DataSource[]; initialType?: string;
  busy: boolean; error: string | null; blocked: boolean;
  onClose: () => void; onSave: (payload: Partial<TaskPayload> | TaskPayload) => void;
}
export function TaskEditor({ task, types, sources, initialType, busy, error, blocked, onClose, onSave }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState(() => task ? draftFromTask(task) : newTaskDraft(initialType ? types.filter(type => type.key === initialType) : types));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const type = types.find(type => type.key === draft.taskType);
  const fields = parameterFields(type);
  const parameters = JSON.parse(draft.parameters) as Record<string, unknown>;
  const source = sources.find(source => source.key === type?.source_key);
  const update = <K extends keyof TaskDraft>(key: K, value: TaskDraft[K]) => setDraft(current => ({ ...current, [key]: value }));
  useEffect(() => {
    const node = dialog.current;
    const previous = document.activeElement as HTMLElement | null;
    node?.showModal();
    return () => { node?.close(); if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    if (error || Object.keys(errors).length) {
      const target = dialog.current?.querySelector<HTMLElement>("[aria-invalid=true]")
        ?? dialog.current?.querySelector<HTMLElement>("[role=alert]");
      target?.focus();
    }
  }, [error, errors]);
  function submit(event: FormEvent) {
    event.preventDefault(); if (busy || blocked) return;
    const issues = validateParameters(parameters, type);
    if (!draft.name.trim()) issues.name = "请填写任务名称。";
    if (!type) issues.form = "当前脚本未注册，无法保存。";
    try {
      const payload = task ? taskPatch(draft, task) : payloadFromDraft(draft);
      const schedule = payload.schedule;
      if (schedule?.type === "once" && new Date(schedule.run_at).getTime() <= Date.now()) issues.form = "单次执行时间必须晚于当前时间。";
      if (Object.keys(issues).length) { setErrors(issues); return; }
      setErrors({}); onSave(payload);
    } catch (caught) { setErrors({ ...issues, form: caught instanceof Error ? caught.message : "请检查执行计划。" }); }
  }
  function chooseType(key: string) {
    // Switching a script resets only its parameter template, never the schedule.
    setDraft(current => ({ ...current, taskType: key, parameters: parametersTemplate(types.find(type => type.key === key)) }));
    setErrors({});
  }
  const frequency = draft.scheduleKind === "cron" ? draft.cronMode : draft.scheduleKind;
  const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return <dialog ref={dialog} className="qft-editor" aria-labelledby="qft-editor-title" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <form onSubmit={submit}>
      <header className="qft-editor-head"><h2 id="qft-editor-title">{task ? "编辑采集任务" : "新建采集任务"}</h2><button type="button" className="qfo-icon-btn" aria-label="关闭任务表单" aria-disabled={busy} onClick={() => !busy && onClose()}><X aria-hidden="true" /></button></header>
      <div className="qft-editor-body">
        {(error || errors.form) && <div className="qft-error" role="alert" tabIndex={-1}>{error || errors.form}{blocked && <p>请关闭表单，刷新列表并核对现有任务后再编辑。</p>}</div>}
        {source && (!source.enabled || !source.configured) && <p className="qft-notice">{source.name} 当前{source.enabled ? "尚未配置" : "已停用"}。可以保存任务计划；数据源恢复可用后，后续计划才会执行，停用期间不补跑。</p>}
        <fieldset disabled={busy || blocked} className="qft-fields">
          <label className="qft-field">任务名称 <span aria-hidden="true">*</span><input autoFocus required maxLength={100} value={draft.name} onChange={e => update("name", e.target.value)} placeholder="例如：上交所交易日历每日同步" aria-invalid={!!errors.name} />{errors.name && <small className="qft-invalid">{errors.name}</small>}</label>
          <div className="qft-form-grid">
            <label className="qft-field">数据源<select value={type?.source_key ?? ""} disabled><option value={type?.source_key ?? ""}>{source?.name ?? (type?.source_key ? "未知来源" : "无关联数据源")}</option></select></label>
            <label className="qft-field">采集脚本 <span aria-hidden="true">*</span><select required value={draft.taskType} onChange={e => chooseType(e.target.value)}>{types.map(item => <option value={item.key} key={item.key}>{taskTypeLabel(item)}</option>)}</select><small>{type && taskTypeLabel(type)}{task && "。更换脚本会重置采集参数。"}</small></label>
          </div>
          <section className="qft-form-section" aria-labelledby="qft-parameters-title"><h3 id="qft-parameters-title">采集参数</h3><div className="qft-form-grid">
            {fields.length === 0 && <p className="qft-help">此脚本无需额外参数。</p>}
            {fields.map(field => {
              const value = parameters[field.key];
              // Merge against the latest draft so rapid native input events cannot
              // restore an older value from another parameter control.
              const set = (next: unknown) => setDraft(current => ({ ...current, parameters: JSON.stringify({ ...JSON.parse(current.parameters), [field.key]: next }) }));
              const id = `qft-param-${field.key}`;
              return <div className={`qft-field${field.type === "boolean" ? " qft-wide" : ""}`} key={field.key}>
                {field.type === "boolean" ? <label className="qft-checkbox"><input type="checkbox" checked={value === true} onChange={e => set(e.target.checked)} aria-describedby={`${id}-help`} />{field.label}</label> : <><label htmlFor={id}>{field.label}{field.required && " *"}</label>
                  {field.key === "exchange" ? <select id={id} required value={String(value ?? "")} onChange={e => set(e.target.value)} aria-describedby={`${id}-help`}><option value="">请选择交易所</option><option value="SSE">上交所（SSE）</option><option value="SZSE">深交所（SZSE）</option>{!!value && !["SSE", "SZSE"].includes(String(value)) && <option value={String(value)}>{String(value)}</option>}</select>
                    : field.enum ? <select id={id} value={String(value ?? "")} onChange={e => set(e.target.value)}>{field.enum.map(item => <option key={String(item)} value={String(item)}>{String(item)}</option>)}</select>
                    : ["integer", "number", "date", "string"].includes(field.type) ? <input id={id} type={field.type === "date" ? "date" : ["integer", "number"].includes(field.type) ? "number" : "text"} required={field.required || !field.nullable && field.default !== undefined} min={field.minimum} max={field.maximum} maxLength={field.maxLength} step={field.type === "integer" ? 1 : "any"} value={parameterInput(value, field)} placeholder={field.nullable ? "留空使用默认规则" : undefined} aria-invalid={!!errors[field.key]} aria-describedby={`${id}-help`} onInput={field.type === "date" ? e => set(e.currentTarget.value || (field.nullable ? null : "")) : undefined} onChange={e => set(e.target.value === "" ? field.nullable ? null : "" : ["integer", "number"].includes(field.type) ? Number(e.target.value) : e.target.value)} />
                      : <p className="qft-help">此参数使用已有配置，当前不支持在表单中编辑。</p>}</>}
                <small id={`${id}-help`}>{field.key === "request_interval_ms" && !field.nullable ? "每次请求之间的等待时间。" : field.help}</small>{errors[field.key] && <small role="alert" className="qft-invalid">{errors[field.key]}</small>}
              </div>;
            })}
          </div></section>
          <section className="qft-form-section" aria-labelledby="qft-schedule-title"><h3 id="qft-schedule-title">执行计划</h3><div className="qft-form-grid">
            <label className="qft-field">调度方式<select value={frequency} onChange={e => {
              const value = e.target.value;
              setDraft(current => ({ ...current, scheduleKind: value === "interval" || value === "once" ? value : "cron", cronMode: value === "interval" || value === "once" ? current.cronMode : value as TaskDraft["cronMode"], cronExpression: value === "advanced" ? cronPreview(current) : current.cronExpression }));
            }}><option value="daily">每天</option><option value="weekdays">周一至周五</option><option value="weekly">每周</option><option value="monthly">每月</option><option value="interval">固定间隔</option><option value="once">单次执行</option><option value="advanced">高级 Cron</option></select></label>
            {draft.scheduleKind === "cron" && <>
              {draft.cronMode !== "advanced" && <label className="qft-field">执行时间<input type="time" required onInput={e => update("cronTime", e.currentTarget.value)} value={draft.cronTime} onChange={e => update("cronTime", e.target.value)} /></label>}
              <label className="qft-field">计划时区<input required maxLength={64} list="qft-timezones" value={draft.timezone} onChange={e => update("timezone", e.target.value)} /><datalist id="qft-timezones"><option value="Asia/Shanghai" /><option value="UTC" /><option value="America/New_York" /><option value="Europe/London" /></datalist><small>使用 IANA 时区名称，保留已有计划时区。</small></label>
              {draft.cronMode === "monthly" && <label className="qft-field">每月执行日<input type="number" required min={1} max={31} value={draft.cronMonthDay} onChange={e => update("cronMonthDay", e.target.value)} /><small>当月不存在该日期时不会执行。</small></label>}
              {draft.cronMode === "advanced" && <label className="qft-field qft-wide">Cron 表达式<input required maxLength={100} value={draft.cronExpression} onChange={e => update("cronExpression", e.target.value)} /><small>格式：分 时 日 月 星期。星期为周一 0 到周日 6。</small></label>}
            </>}
            {draft.scheduleKind === "interval" && <><label className="qft-field">间隔秒数<input type="number" required min={1} max={31536000} value={draft.intervalSeconds} onChange={e => update("intervalSeconds", e.target.value)} /></label><label className="qft-field qft-wide">开始时间<input required type="datetime-local" step="0.001" onInput={e => update("startAt", e.currentTarget.value)} value={draft.startAt} onChange={e => update("startAt", e.target.value)} /><small>按浏览器本地时区 {localZone} 输入。</small></label></>}
            {draft.scheduleKind === "once" && <label className="qft-field">执行时间<input type="datetime-local" required step="0.001" onInput={e => update("runAt", e.currentTarget.value)} value={draft.runAt} onChange={e => update("runAt", e.target.value)} /><small>按浏览器本地时区 {localZone} 输入。</small></label>}
          </div>
          {draft.scheduleKind === "cron" && draft.cronMode === "weekly" && <fieldset className="qft-weekdays"><legend>执行日</legend>{WEEKDAYS.map(day => <label key={day.value}><input type="checkbox" checked={draft.cronWeekdays.includes(day.value)} onChange={e => update("cronWeekdays", e.target.checked ? [...draft.cronWeekdays, day.value] : draft.cronWeekdays.filter(value => value !== day.value))} />{day.label}</label>)}</fieldset>}
          <p className="qft-notice">{draft.scheduleKind === "cron" ? <>{cronPreview(draft)} · {draft.timezone}。{draft.cronMode === "weekdays" ? "周一至周五不排除交易所节假日。" : "按日历规则执行，不自动排除交易所节假日。"}</> : draft.scheduleKind === "once" ? "仅在指定时间执行一次。已完成任务修改计划后会转为暂停，需要手动启用。" : "按固定时间间隔执行。"}</p>
          </section>
          <details className="qft-form-section"><summary>高级设置</summary><div className="qft-form-grid">
            <label className="qft-field">并发数<input type="number" required min={1} max={32} value={draft.concurrencyLimit} onChange={e => update("concurrencyLimit", e.target.value)} /></label>
            <label className="qft-field">重叠策略<select value={draft.overlapPolicy} onChange={e => update("overlapPolicy", e.target.value as "queue" | "skip")}><option value="skip">跳过重叠</option><option value="queue">加入队列</option></select></label>
            {draft.overlapPolicy === "queue" && <label className="qft-field">队列上限<input type="number" required min={1} max={10000} value={draft.queueLimit} onChange={e => update("queueLimit", e.target.value)} /></label>}
            <label className="qft-field">优先级<input type="number" required min={-100} max={100} value={draft.priority} onChange={e => update("priority", e.target.value)} /><small>数值越大，队列中越优先执行。</small></label>
            <label className="qft-field qft-wide">任务说明<textarea maxLength={10000} rows={3} value={draft.description} onChange={e => update("description", e.target.value)} /></label>
          </div></details>
        </fieldset>
      </div>
      <footer className="qft-editor-foot"><span>{busy ? "正在保存，请稍候…" : "保存计划，不触发立即运行"}</span><button type="button" className="qfo-secondary-btn" aria-disabled={busy} onClick={() => !busy && onClose()}>取消</button><button className="qfo-primary-btn" type="submit" aria-disabled={busy || blocked}>保存任务</button></footer>
    </form>
  </dialog>;
}

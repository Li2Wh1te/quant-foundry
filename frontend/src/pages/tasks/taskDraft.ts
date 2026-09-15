import type { SchedulerTask, TaskPayload, TaskSchedule, TaskType } from "../../api/scheduler";

type ScheduleKind = TaskSchedule["type"];
type CronScheduleMode = "daily" | "weekdays" | "weekly" | "monthly" | "advanced";
type JsonSchema = Record<string, unknown>;

export interface TaskDraft {
  name: string; description: string; taskType: string; parameters: string;
  scheduleKind: ScheduleKind; cronExpression: string; timezone: string;
  cronMode: CronScheduleMode; cronTime: string; cronWeekdays: string[]; cronMonthDay: string;
  intervalSeconds: string; startAt: string; runAt: string;
  concurrencyLimit: string; overlapPolicy: "skip" | "queue"; queueLimit: string; priority: string;
}

export const WEEKDAYS = [
  { value: "0", label: "周一" }, { value: "1", label: "周二" }, { value: "2", label: "周三" },
  { value: "3", label: "周四" }, { value: "4", label: "周五" }, { value: "5", label: "周六" },
  { value: "6", label: "周日" }
] as const;

/**
 * APScheduler's CronTrigger uses Monday=0 and Sunday=6. Keeping this mapping
 * next to the UI labels prevents an otherwise very easy one-day schedule shift.
 */
export function cronExpressionFromDraft(draft: TaskDraft): string {
  if (draft.cronMode === "advanced") return draft.cronExpression.trim();

  const [hour, minute] = draft.cronTime.split(":").map(Number);
  if (!Number.isInteger(hour) || !Number.isInteger(minute) || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
    throw new Error("请选择有效的执行时间。");
  }

  if (draft.cronMode === "daily") return `${minute} ${hour} * * *`;
  if (draft.cronMode === "weekdays") return `${minute} ${hour} * * 0-4`;
  if (draft.cronMode === "weekly") {
    if (draft.cronWeekdays.length === 0) throw new Error("请至少选择一个执行日。");
    return `${minute} ${hour} * * ${[...draft.cronWeekdays].sort((left, right) => Number(left) - Number(right)).join(",")}`;
  }

  const day = Number(draft.cronMonthDay);
  if (!Number.isInteger(day) || day < 1 || day > 31) throw new Error("每月执行日必须在 1 到 31 之间。");
  return `${minute} ${hour} ${day} * *`;
}

export function cronEditorValues(expression: string): Pick<TaskDraft, "cronMode" | "cronTime" | "cronWeekdays" | "cronMonthDay"> {
  const defaults = { cronMode: "advanced" as const, cronTime: "18:00", cronWeekdays: ["0"], cronMonthDay: "1" };
  const fields = expression.trim().split(/\s+/);
  if (fields.length !== 5 || !/^\d{1,2}$/.test(fields[0]) || !/^\d{1,2}$/.test(fields[1])) return defaults;

  const minute = Number(fields[0]);
  const hour = Number(fields[1]);
  if (minute > 59 || hour > 23) return defaults;
  const cronTime = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  const [, , dayOfMonth, month, dayOfWeek] = fields;

  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "*") return { ...defaults, cronMode: "daily", cronTime };
  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "0-4") return { ...defaults, cronMode: "weekdays", cronTime };
  if (dayOfMonth === "*" && month === "*" && /^([0-6])(,[0-6])*$/.test(dayOfWeek)) {
    return { ...defaults, cronMode: "weekly", cronTime, cronWeekdays: dayOfWeek.split(",") };
  }
  if (/^(?:[1-9]|[12]\d|3[01])$/.test(dayOfMonth) && month === "*" && dayOfWeek === "*") {
    return { ...defaults, cronMode: "monthly", cronTime, cronMonthDay: dayOfMonth };
  }
  return { ...defaults, cronTime };
}

export function cronPreview(draft: TaskDraft): string {
  if (draft.cronMode === "advanced") return draft.cronExpression || "请输入 Cron 表达式";
  try {
    return cronExpressionFromDraft(draft);
  } catch (error) {
    return error instanceof Error ? error.message : "调度配置无效";
  }
}

export function localDateTimeValue(value?: string): string {
  const date = value ? new Date(value) : new Date(Date.now() + 60_000);
  const valid = Number.isNaN(date.getTime()) ? new Date(Date.now() + 60_000) : date;
  return new Date(valid.getTime() - valid.getTimezoneOffset() * 60_000).toISOString().slice(0, 23);
}

export function schemaValue(schema: JsonSchema): unknown {
  if (Object.hasOwn(schema, "default")) return schema.default;
  const alternatives = Array.isArray(schema.anyOf) ? schema.anyOf : [];
  const nonNullAlternative = alternatives.find((item): item is JsonSchema => (
    typeof item === "object" && item !== null && (item as JsonSchema).type !== "null"
  ));
  if (nonNullAlternative) return schemaValue(nonNullAlternative);
  if (Array.isArray(schema.enum) && schema.enum.length > 0) return schema.enum[0];
  if (schema.type === "object") return schemaObjectValue(schema);
  if (schema.type === "array") return [];
  if (schema.type === "boolean") return false;
  if (schema.type === "integer" || schema.type === "number") {
    return typeof schema.minimum === "number" ? schema.minimum : 0;
  }
  return "";
}

export function schemaObjectValue(schema: JsonSchema): Record<string, unknown> {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return {};
  const required = new Set(
    Array.isArray(schema.required) ? schema.required.filter((item): item is string => typeof item === "string") : []
  );
  return Object.fromEntries(
    Object.entries(properties)
      .filter(([, value]) => typeof value === "object" && value !== null && !Array.isArray(value))
      .filter(([key, value]) => required.has(key) || Object.hasOwn(value as JsonSchema, "default"))
      .map(([key, value]) => [key, schemaValue(value as JsonSchema)])
  );
}

export function parametersTemplate(taskType: TaskType | undefined): string {
  return JSON.stringify(schemaObjectValue(taskType?.parameter_schema ?? {}), null, 2);
}

export function taskTypeLabel(taskType: TaskType): string {
  return taskType.english_name ? `${taskType.name}（${taskType.english_name}）` : taskType.name;
}

/**
 * Resolve a persisted task type key to the stable user-facing bilingual label.
 * Unknown keys can occur while a task-type registry is being upgraded; keep
 * those internal identifiers out of ordinary UI text instead of leaking them
 * as a fallback.
 */
export function taskTypeLabelByKey(taskTypeKey: string, taskTypes: TaskType[]): string {
  const taskType = taskTypes.find((candidate) => candidate.key === taskTypeKey);
  return taskType ? taskTypeLabel(taskType) : "未注册任务类型";
}

export function newTaskDraft(types: TaskType[]): TaskDraft {
  const selectedType = types[0];
  return {
    name: "", description: "", taskType: selectedType?.key ?? "",
    parameters: parametersTemplate(selectedType),
    scheduleKind: "cron", cronExpression: "0 18 * * 0-4", timezone: "Asia/Shanghai",
    cronMode: "weekdays", cronTime: "18:00", cronWeekdays: ["0", "1", "2", "3", "4"], cronMonthDay: "1",
    intervalSeconds: "900", startAt: localDateTimeValue(), runAt: localDateTimeValue(),
    concurrencyLimit: "1", overlapPolicy: "skip", queueLimit: "1", priority: "0",
    ...sourceScheduleDefaults(selectedType?.key ?? ""),
  };
}

/** Polling schedules drain bounded batches; the collector owns daily/weekly
 * freshness boundaries. Existing saved schedules are never rewritten here. */
export function sourceScheduleDefaults(key: string): Partial<TaskDraft> {
  if (!key.startsWith("data.ths.")) return {};
  const catalog = ["data.ths.tickers", "data.ths.calendar", "data.ths.index_catalog"].includes(key);
  return { scheduleKind: "cron", cronMode: catalog ? "daily" : "advanced",
    cronTime: "20:00", cronExpression: catalog ? "0 20 * * *" : "*/10 * * * *",
    timezone: "Asia/Shanghai", priority: catalog ? "100" :
      ["etf_daily", "stock_daily", "index_daily", "fund_nav", "stock_actions", "stock_recent_dump", "stock_actions_dump", "stock_quote", "etf_quote", "index_quote"].includes(key.replace("data.ths.", "")) ? "80" : key === "data.ths.stock_daily_dump" ? "10" : "50" };
}

export function draftFromTask(task: SchedulerTask): TaskDraft {
  const schedule = task.schedule;
  const cronExpression = schedule.type === "cron" ? schedule.expression : "0 18 * * 0-4";
  return {
    name: task.name, description: task.description ?? "", taskType: task.task_type,
    parameters: JSON.stringify(task.parameters, null, 2), scheduleKind: schedule.type,
    cronExpression,
    timezone: schedule.type === "cron" ? schedule.timezone : "Asia/Shanghai",
    ...cronEditorValues(cronExpression),
    intervalSeconds: schedule.type === "interval" ? String(schedule.seconds) : "900",
    startAt: schedule.type === "interval" ? localDateTimeValue(schedule.start_at) : localDateTimeValue(),
    runAt: schedule.type === "once" ? localDateTimeValue(schedule.run_at) : localDateTimeValue(),
    concurrencyLimit: String(task.concurrency_limit), overlapPolicy: task.overlap_policy,
    queueLimit: String(task.queue_limit), priority: String(task.priority)
  };
}

export function scheduleFromDraft(draft: TaskDraft): TaskSchedule {
  if (draft.scheduleKind === "cron") return { type: "cron", expression: cronExpressionFromDraft(draft), timezone: draft.timezone.trim() };
  if (draft.scheduleKind === "interval") return { type: "interval", seconds: Number(draft.intervalSeconds), start_at: new Date(draft.startAt).toISOString() };
  return { type: "once", run_at: new Date(draft.runAt).toISOString() };
}

export function payloadFromDraft(draft: TaskDraft): TaskPayload {
  const parameters = JSON.parse(draft.parameters) as unknown;
  if (!parameters || Array.isArray(parameters) || typeof parameters !== "object") throw new Error("任务参数必须是 JSON 对象。");
  return {
    name: draft.name.trim(), description: draft.description.trim() || null, task_type: draft.taskType,
    parameters: parameters as Record<string, unknown>, schedule: scheduleFromDraft(draft),
    concurrency_limit: Number(draft.concurrencyLimit), overlap_policy: draft.overlapPolicy,
    queue_limit: Number(draft.queueLimit), priority: Number(draft.priority)
  };
}


export interface ParameterField {
  key: string; label: string; help: string; type: string; required: boolean; nullable: boolean;
  minimum?: number; maximum?: number; maxLength?: number; enum?: unknown[]; default?: unknown;
  itemEnum?: unknown[]; minItems?: number; maxItems?: number;
}
const PARAMETER_OPTION_COPY: Record<string, string> = {
  "fund-etf": "ETF", "fund-lof": "LOF", "fund-reits": "公募 REITs", "fund-otc": "场外基金",
  "a-share": "A 股", "a-share-index": "指数与板块", incremental: "日常更新", reconcile: "历史核对", backfill: "首次回补与失败补采",
};
export function parameterOptionLabel(value: unknown): string {
  return PARAMETER_OPTION_COPY[String(value)] ?? String(value);
}
const PARAMETER_COPY: Record<string, [string, string]> = {
  quota_tabs: ["QDII 分类代码", "填写已明确的官方分类代码，以逗号分隔。默认仅覆盖文档示例 nazhi100、remen，不代表全部分类。"],
  asset_types: ["资产类型", "仅采集当前接口支持的已选类型；不同来源的数据独立保存。"],
  subjects: ["指定标的或关联对象", "可用英文逗号分隔完整代码或公司、经理 ID；留空采集所有适用对象。"],
  mode: ["采集方式", "日常更新会补采新增及失败对象；历史核对会复查接口可用历史。"],
  max_requests: ["每批接口请求预算", "达到预算后保存断点并结束本批，下次继续。"],
  max_seconds: ["每批时间预算（秒）", "达到预算后在安全边界退出；当前请求或文件校验可能延长实际时间。"],
  batch_size: ["每批对象数", "每次最多处理这些对象，后续运行继续未完成范围；首次回补建议使用较低优先级并定期运行。"],
  refresh_today: ["再次检查本期已完成对象", "用于净值补查或手动复查；已有成功版本会保留。"],
  exchange: ["交易所", "上交所使用 SSE，深交所使用 SZSE。"],
  initial_start_date: ["首次采集起始日期", "已有采集进度时，从保存的进度继续执行。"],
  request_interval_ms: ["请求间隔（毫秒）", "留空沿用数据源配置；0 表示不额外等待。"],
  lookback_trading_days: ["回看交易日数", "校验最近若干交易日的复权因子。"],
  start_date: ["采集开始日期", "留空由脚本根据采集进度确定。"],
  end_date: ["采集结束日期", "留空使用脚本默认结束日期。"],
  reconciliation_days: ["近期校验天数", "用于现金分红近期校验的回看窗口。"],
  coverage_confirmed: ["确认数据覆盖完整", "仅在已独立确认所选日期范围的数据完整时勾选。勾选后，无停牌事件的记录可被视为正常交易；未勾选时保持未知。"]
};
/** Registry metadata supplies constraints; reviewed copy explains the current
 * ingestion fields without exposing their stable machine identifiers. */
export function parameterFields(type?: TaskType): ParameterField[] {
  const schema = type?.parameter_schema ?? {};
  const required = Array.isArray(schema.required) ? schema.required : [];
  return Object.entries((schema.properties ?? {}) as Record<string, JsonSchema>).map(([key, spec]) => {
    const alternatives = (spec.anyOf ?? []) as JsonSchema[];
    const shape = alternatives.find(item => item.type !== "null") ?? spec;
    const itemShape = shape.items as JsonSchema | undefined;
    const copy = key === "subjects" && ["data.ths.anomaly_stock", "data.ths.rank_trend"].includes(type?.key ?? "")
      ? ["指定标的或关联对象", "此任务必须指定股票，使用逗号分隔完整代码；不会默认采集全市场。"]
      : PARAMETER_COPY[key];
    return { key, label: copy?.[0] ?? "扩展采集参数", help: copy?.[1] ?? "此参数由当前脚本定义。",
      type: shape.format === "date" ? "date" : String(shape.type ?? "unsupported"),
      required: required.includes(key), nullable: alternatives.some(item => item.type === "null"),
      minimum: shape.minimum as number | undefined, maximum: shape.maximum as number | undefined,
      maxLength: shape.maxLength as number | undefined, enum: shape.enum as unknown[] | undefined,
      itemEnum: itemShape && Object.hasOwn(itemShape, "const") ? [itemShape.const] : itemShape?.enum as unknown[] | undefined,
      minItems: shape.minItems as number | undefined, maxItems: shape.maxItems as number | undefined,
      default: spec.default };
  });
}
export function parameterInput(value: unknown, field: ParameterField): string {
  if (value == null) return "";
  if (Array.isArray(value)) return value.map(parameterOptionLabel).join(", ");
  if (field.enum) return parameterOptionLabel(value);
  if (field.type === "date" && /^\d{8}$/.test(String(value))) return String(value).replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3");
  return String(value);
}
export function validateParameters(parameters: Record<string, unknown>, type?: TaskType): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of parameterFields(type)) {
    const value = parameters[field.key];
    if (value == null || value === "") {
      if (field.required || !field.nullable && field.default !== undefined) errors[field.key] = `请填写${field.label}。`;
      continue;
    }
    if (["integer", "number"].includes(field.type)) {
      const number = Number(value);
      if (!Number.isFinite(number) || field.type === "integer" && !Number.isInteger(number)
        || field.minimum !== undefined && number < field.minimum || field.maximum !== undefined && number > field.maximum)
        errors[field.key] = `请填写有效的${field.label}${field.minimum !== undefined ? `（${field.minimum}–${field.maximum ?? "不限"}）` : ""}。`;
    }
    if (field.type === "array" && (!Array.isArray(value)
      || field.minItems !== undefined && value.length < field.minItems
      || field.maxItems !== undefined && value.length > field.maxItems
      || field.itemEnum && value.some(item => !field.itemEnum!.includes(item)))) {
      errors[field.key] = `请选择或填写有效的${field.label}。`;
    }
    if (field.key === "subjects" && Array.isArray(value) && value.some(item =>
      typeof item !== "string" || !/^[A-Za-z0-9_.-]{1,64}$/.test(item))) {
      errors[field.key] = "请用逗号分隔有效的完整代码或关联对象 ID，不要留空项。";
    }
  }
  if (parameters.start_date && parameters.end_date && String(parameters.start_date) > String(parameters.end_date)) errors.end_date = "结束日期不能早于开始日期。";
  return errors;
}
function scheduleFields(draft: TaskDraft): (keyof TaskDraft)[] {
  // Inactive controls contain fresh defaults and must never mark a plan dirty.
  if (draft.scheduleKind === "once") return ["scheduleKind", "runAt"];
  if (draft.scheduleKind === "interval") return ["scheduleKind", "intervalSeconds", "startAt"];
  return ["scheduleKind", "cronMode", "timezone", ...(draft.cronMode === "advanced" ? ["cronExpression" as const]
    : ["cronTime" as const, ...(draft.cronMode === "weekly" ? ["cronWeekdays" as const] : draft.cronMode === "monthly" ? ["cronMonthDay" as const] : [])])];
}
/** Compare editor values before converting dates. Untouched plans keep exact
 * timestamps, non-default timezones and expired once dates on unrelated edits. */
export function taskPatch(draft: TaskDraft, original: SchedulerTask): Partial<TaskPayload> {
  const previous = draftFromTask(original);
  const next = payloadFromDraft(draft);
  const patch: Partial<TaskPayload> = {};
  for (const field of ["name", "description", "task_type", "parameters", "concurrency_limit", "overlap_policy", "queue_limit", "priority"] as const) {
    if (JSON.stringify(next[field]) !== JSON.stringify(original[field])) Object.assign(patch, { [field]: next[field] });
  }
  if (scheduleFields(draft).some(field => JSON.stringify(draft[field]) !== JSON.stringify(previous[field]))) patch.schedule = next.schedule;
  return patch;
}

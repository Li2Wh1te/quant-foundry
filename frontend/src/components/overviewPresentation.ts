import type { OverviewRun } from "../api/overview";

export const RUN_LABELS: Record<OverviewRun["status"], string> = {
  queued: "排队中", running: "运行中", succeeded: "成功", failed: "失败",
  skipped: "已跳过", interrupted: "已中断", cancelled: "已取消",
  timed_out: "已超时", indeterminate: "状态不确定"
};

export function runTone(status: OverviewRun["status"]): string {
  if (status === "succeeded") return "success";
  if (["failed", "interrupted", "timed_out", "indeterminate"].includes(status)) return "failed";
  if (status === "running" || status === "queued") return "running";
  return "neutral";
}

export function taskTypeLabel(run: OverviewRun): string {
  return `${run.task_type_name}（${run.task_type_english_name}）`;
}

/** Always include the date: a historical run must never look like today's run
 * just because its clock time matches. All operator times use Shanghai. */
export function overviewTime(value: string | null | undefined, full = false): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", ...(full ? { year: "numeric" as const } : {}),
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    ...(full ? { second: "2-digit" as const } : {}), hourCycle: "h23"
  }).format(new Date(value));
}

export function overviewDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 1) return "< 1 秒";
  const whole = Math.floor(seconds);
  if (whole < 60) return `${whole} 秒`;
  if (whole < 3600) return `${Math.floor(whole / 60)} 分 ${whole % 60} 秒`;
  return `${Math.floor(whole / 3600)} 时 ${Math.floor(whole % 3600 / 60)} 分`;
}

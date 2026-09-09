import type { TaskRun, TaskSchedule, TaskState, WorkspaceTask } from "../../api/scheduler";
import { cronEditorValues } from "./taskDraft";

export const taskStateLabel = (state: TaskState) => ({ active: "已启用", paused: "已暂停", completed: "已完成", archived: "已归档" })[state];
export const runStateLabel = (state: TaskRun["status"]) => ({ queued: "等待执行", running: "运行中", succeeded: "成功", failed: "失败", skipped: "已跳过", interrupted: "已中断", cancelled: "已取消", timed_out: "已超时", indeterminate: "状态不确定" })[state];
export function taskTime(value?: string | null): string {
  if (!value) return "—";
  if (!Number.isFinite(new Date(value).getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(value));
}
export function formatSchedule(schedule: TaskSchedule): string {
  if (schedule.type === "once") return `单次 · ${taskTime(schedule.run_at)}`;
  if (schedule.type === "interval") return `每 ${schedule.seconds} 秒`;
  const value = cronEditorValues(schedule.expression);
  const dayNames = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const prefix = { daily: "每天", weekdays: "周一至周五", weekly: value.cronWeekdays.map(day => dayNames[Number(day)]).join("、"), monthly: `每月 ${value.cronMonthDay} 日`, advanced: "Cron" }[value.cronMode];
  return value.cronMode === "advanced" ? `Cron · ${schedule.expression}` : `${prefix} ${value.cronTime}`;
}
export function nextRunLabel(task: WorkspaceTask): string {
  if (!task.registered) return "脚本未注册";
  if (task.state === "completed") return "已完成 · 无后续计划";
  if (task.state === "paused") return "已暂停 · 计划保留";
  if (task.source_enabled === false) return "数据源停用 · 计划保留";
  if (task.source_configured === false) return "数据源待配置";
  return task.next_run_at ? taskTime(task.next_run_at) : "暂无后续计划";
}
export function canRun(task: WorkspaceTask): boolean {
  return task.registered && ["active", "paused"].includes(task.state) && task.source_enabled !== false && task.source_configured !== false;
}
export function runSummary(run: TaskRun): string {
  // Vendor errors and internal steps remain in expandable technical detail.
  // Only explicit Chinese messages are eligible as the operator summary.
  const message = run.result?.message;
  if (typeof message === "string" && /[\u4e00-\u9fff]/.test(message) && message.length < 500) return message;
  return ({ queued: "任务已进入队列，等待执行。", running: "任务正在采集数据，最终结果尚未确认。", succeeded: "本次采集执行成功。", failed: "本次采集失败，请查看日志定位原因。", skipped: "本次采集已跳过，未执行数据采集。", interrupted: "服务在采集完成前停止，本次执行已中断。", cancelled: "本次采集已取消。", timed_out: "本次采集超时，请检查运行日志。", indeterminate: "本次采集结果尚不能确认，请先核对数据和日志。" })[run.status];
}

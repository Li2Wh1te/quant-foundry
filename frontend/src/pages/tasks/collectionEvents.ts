/** Operator-facing event copy is separate from stable structured log keys. */
export const collectionEvents: Record<string, { title: string; summary: string }> = {
  foundation_work_failed: { title: "底座处理失败", summary: "底座工作未完成，已提交检查点保留，请查看工作详情。" },
  foundation_worker_restarting: { title: "底座进程恢复", summary: "底座处理进程正在退避重启，回测进程继续运行。" },
  tonghuashun_collection_completed: {
    title: "同花顺采集完成", summary: "同花顺本次范围采集完成，记录计数及完成标记请查看运行详情。",
  },
  tonghuashun_collection_failed: {
    title: "同花顺采集失败", summary: "同花顺本次范围未完整完成，成功数据已保留，失败范围等待重试。",
  },
};

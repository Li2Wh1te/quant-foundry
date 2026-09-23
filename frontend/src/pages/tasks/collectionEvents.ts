/** Operator-facing event copy is separate from stable structured log keys. */
export const collectionEvents: Record<string, { title: string; summary: string }> = {
  foundation_updates_advanced: { title: "本地正式化更新", summary: "本批已提交进度保留，选中、发布及失败数量请查看运行详情。" },
  foundation_updates_failed: { title: "本地正式化更新失败", summary: "失败来源已记录，其他来源的已提交进度保留，请查看详情定位重试。" },
  foundation_update_source_failed: { title: "本地来源正式化失败", summary: "该来源处理异常，检查点与错误详情已保留，其他来源继续处理。" },
  foundation_work_failed: { title: "底座处理失败", summary: "底座工作未完成，已提交检查点保留，请查看工作详情。" },
  foundation_worker_restarting: { title: "底座进程恢复", summary: "底座处理进程正在退避重启，回测进程继续运行。" },
  tonghuashun_collection_completed: {
    title: "同花顺采集完成", summary: "同花顺本次范围采集完成，记录计数及完成标记请查看运行详情。",
  },
  tonghuashun_collection_failed: {
    title: "同花顺采集失败", summary: "同花顺本次范围未完整完成，成功数据已保留，失败范围等待重试。",
  },
};

/** Operator-facing event copy is separate from stable structured log keys. */
export const collectionEvents: Record<string, { title: string; summary: string }> = {
  foundation_full_stopped: { title: "全量执行未完成", summary: "执行条件未满足，原发布检查点与来源证据保留，请查看停止原因。" },
  foundation_full_started: { title: "全量正式化启动", summary: "已固定本次处理范围，发布检查点尚未推进。" },
  foundation_full_source_processed: { title: "全量来源处理结果", summary: "来源发布或隔离结果已保存，具体计数及检查点请查看详情。" },
  foundation_full_source_failed: { title: "全量来源处理失败", summary: "该来源执行异常，原检查点保留，其余来源继续处理。" },
  foundation_full_finished: { title: "全量执行结束", summary: "已遍历本次提交范围，发布结果及未解决来源请查看详情。" },
  foundation_table_bootstrap_sealed: { title: "本地表差异已固定", summary: "原始捕获与当前本地表的差异已保存；业务发布进度请查看相应任务。" },
  foundation_table_bootstrap_failed: { title: "本地表差异核对失败", summary: "该领域核对失败，检查点未推进，其余领域继续。" },
  foundation_table_updates_advanced: { title: "本地表持续正式化", summary: "已提交本次变化的处理进度，发布和未完成计数请查看详情。" },
  foundation_table_updates_failed: { title: "本地表正式化失败", summary: "失败变化已保留，其他已提交进度和检查点不受影响。" },
  foundation_table_update_failed: { title: "本地表变化处理失败", summary: "该条变化处理异常，检查点与错误详情已保留。" },
  foundation_table_update_rejected: { title: "本地表变化未通过校验", summary: "该条变化未能发布，来源和错误详情已保留。" },
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

/** Operator-facing event copy is separate from stable structured log keys. */
export const collectionEvents: Record<string, { title: string; summary: string }> = {
  tonghuashun_collection_completed: {
    title: "同花顺采集完成", summary: "同花顺本次范围采集完成，记录计数及完成标记请查看运行详情。",
  },
  tonghuashun_collection_failed: {
    title: "同花顺采集失败", summary: "同花顺本次范围未完整完成，成功数据已保留，失败范围等待重试。",
  },
};

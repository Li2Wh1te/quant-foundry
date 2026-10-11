import rotationSource from "../../../../examples/strategies/etf_rotation.py?raw";
export const EMPTY_SCHEMA = { type: "object", properties: {}, additionalProperties: false };
export const TEMPLATES = {
  hold: {
    label: "S3 不交易模板",
    source: '"""S3 callbacks; no orders are submitted."""\nfrom quantfoundry.api import log\n\ndef initialize(context):\n    log.info("S3 strategy initialized")\n\ndef handle_data(context, data):\n    pass\n',
    schema: EMPTY_SCHEMA, parameters: {},
  },
  blank: {
    label: "S3 空白 Python 模板",
    source: '"""Complete the S3 callback before running."""\nfrom quantfoundry.api import get_price, order_target_percent, log\n\ndef initialize(context):\n    context.state.security = context.params.get("security", "510300.SH")\n\ndef handle_data(context, data):\n    raise NotImplementedError("Strategy is not implemented")\n',
    schema: { type: "object", properties: { security: { type: "string" } }, additionalProperties: false }, parameters: { security: "510300.SH" },
  },
  rotation: {
    label: "S3 ETF 轮动模板",
    source: rotationSource,
    schema: { type: "object", properties: {
      securities: { type: "array", minItems: 1, items: { type: "string" }, description: "运行标的集合内的 ETF 代码" },
      window: { type: "integer", minimum: 2, maximum: 252, description: "原始价回看样本数" },
      weight: { type: "string", description: "目标比例，十进制字符串" },
      end_date: { type: "string", description: "与运行结束日期一致，避免最后会话后新挂单" },
    }, required: ["securities", "window", "weight", "end_date"], additionalProperties: false },
    parameters: { securities: ["510300.SH", "510500.SH"], window: 20, weight: "0.6", end_date: "2026-10-09" },
  },
};
export type TemplateKey = keyof typeof TEMPLATES;

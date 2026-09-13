import rotationSource from "./rotation.py?raw";
export const EMPTY_SCHEMA = {
  type: "object",
  properties: {},
  additionalProperties: false,
};
export const TEMPLATES = {
  hold: {
    label: "持有 / 不交易模板",
    source:
      '"""Keep existing positions without submitting new targets."""\n\ndef run(context, parameters):\n    return {"mode": "hold"}\n',
    schema: EMPTY_SCHEMA,
    parameters: {},
  },
  blank: {
    label: "空白 Python 模板",
    source:
      '"""Implement the strategy before publishing."""\n\ndef run(context, parameters):\n    # TODO: implement a hold or target_weights decision.\n    raise NotImplementedError("Strategy is not implemented")\n',
    schema: EMPTY_SCHEMA,
    parameters: {},
  },
  rotation: {
    label: "ETF 轮动模板",
    source: rotationSource,
    schema: {
      type: "object",
      properties: {
        lookback: {
          type: "integer",
          minimum: 2,
          maximum: 252,
          description: "原始收盘价回看交易日数",
        },
        holding_size: {
          type: "integer",
          minimum: 1,
          maximum: 20,
          description: "最多持有 ETF 数量",
        },
      },
      required: ["lookback", "holding_size"],
      additionalProperties: false,
    },
    parameters: { lookback: 60, holding_size: 3 },
  },
};
export type TemplateKey = keyof typeof TEMPLATES;

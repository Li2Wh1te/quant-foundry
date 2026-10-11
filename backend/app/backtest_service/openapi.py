"""Documentation projection of the shared schema; native D01 validates input."""
from copy import deepcopy
import json
from pathlib import Path

RUN_SCHEMA = json.loads(Path(__file__).with_name("run_request.schema.json").read_text())


def run_schema():
    definitions = RUN_SCHEMA["$defs"]

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(deepcopy(definitions[value["$ref"].split("/")[-1]]))
        return {k: inline(v) for k, v in value.items() if k not in ("$schema", "$id", "$defs")}

    return inline(RUN_SCHEMA)


def request_body(schema):
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


RUN_BODY = request_body(run_schema())
BATCH_BODY = request_body({"type": "object", "additionalProperties": False, "required": ["configs"],
    "properties": {"configs": {"type": "array", "minItems": 1, "maxItems": 256, "items": run_schema()}}})
COMPARE_BODY = request_body({"type": "object", "additionalProperties": False, "required": ["run_ids"],
    "properties": {"run_ids": {"type": "array", "minItems": 2, "maxItems": 16, "uniqueItems": True,
                               "items": {"type": "string", "format": "uuid"}}}})

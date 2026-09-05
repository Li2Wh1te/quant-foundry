"""Shared, non-executing JSON Schema profile for published strategy inputs.

Only the documented subset is accepted. Unsupported keywords are errors rather
than silently ignored constraints; publication and run binding use this same
validator so a revision cannot promise stronger validation than execution.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import math
import re


ANNOTATIONS = {"title", "description", "default", "examples", "$schema"}
KEYWORDS = ANNOTATIONS | {
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minLength", "maxLength", "pattern", "minItems", "maxItems",
    "uniqueItems", "minProperties", "maxProperties",
}
TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _finite_number(value):
    # JSON integers have arbitrary precision; converting them to float solely
    # for finiteness checks can overflow on otherwise valid integer inputs.
    return type(value) is int or (type(value) is float and math.isfinite(value))


def validate_schema(schema, path="$", *, root=True):
    """Return field-addressed errors for malformed or unsupported schemas."""
    if not isinstance(schema, Mapping):
        return [f"{path}: Schema 必须是对象"]
    errors = [f"{path}.{key}: 不支持此 Schema 约束" for key in schema if key not in KEYWORDS]
    kind = schema.get("type")
    if kind is not None and (not isinstance(kind, str) or kind not in TYPES):
        errors.append(f"{path}.type: 不支持此类型")
    if root and kind not in (None, "object"):
        errors.append(f"{path}.type: 顶层必须为 object")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        errors.append(f"{path}.properties: 必须是对象")
        properties = {}
    for key, rule in properties.items():
        errors.extend(validate_schema(rule, f"{path}.properties.{key}", root=False))
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
        errors.append(f"{path}.required: 必须是字符串数组")
    elif len(set(required)) != len(required):
        errors.append(f"{path}.required: 字段不能重复")
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, bool):
        errors.append(f"{path}.additionalProperties: 仅支持布尔值")
    if "items" in schema:
        errors.extend(validate_schema(schema["items"], f"{path}.items", root=False))
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        errors.append(f"{path}.enum: 必须是非空数组")
    for key in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            errors.append(f"{path}.{key}: 必须是非负整数")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key in schema and (type(schema[key]) not in (int, float) or not _finite_number(schema[key]) or (key == "multipleOf" and schema[key] <= 0)):
            errors.append(f"{path}.{key}: 必须是有效数值")
    if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
        errors.append(f"{path}.uniqueItems: 必须是布尔值")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (TypeError, re.error):
            errors.append(f"{path}.pattern: 正则表达式无效")
    if not errors and "default" in schema:
        errors.extend(validate_value(schema, schema["default"], f"{path}.default"))
    return errors


def _equal(left, right):
    # JSON booleans are distinct from numbers, unlike Python's bool/int.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def validate_value(schema, value, path="$"):
    """Validate JSON values only after validating the schema definition."""
    errors = []
    number = type(value) in (int, float)
    kinds = {"object": isinstance(value, Mapping), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": type(value) is bool,
             "integer": number and _finite_number(value) and int(value) == value,
             "number": number and _finite_number(value), "null": value is None}
    kind = schema.get("type")
    if kind and not kinds[kind]:
        return [f"{path}: 参数类型必须为 {kind}"]
    if number and not _finite_number(value):
        return [f"{path}: 数值必须有限"]
    if "enum" in schema and not any(_equal(value, item) for item in schema["enum"]):
        errors.append(f"{path}: 不在允许的枚举值中")
    if "const" in schema and not _equal(value, schema["const"]):
        errors.append(f"{path}: 不等于指定常量")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        errors.extend(f"{path}.{key}: 缺少必填参数" for key in schema.get("required", []) if key not in value)
        for key, item in value.items():
            if key in properties:
                errors.extend(validate_value(properties[key], item, f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: 未声明的参数")
        errors.extend(_length(schema, value, path, "Properties"))
    if isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(validate_value(schema.get("items", {}), item, f"{path}[{index}]"))
        errors.extend(_length(schema, value, path, "Items"))
        if schema.get("uniqueItems") and any(_equal(item, previous) for i, item in enumerate(value) for previous in value[:i]):
            errors.append(f"{path}: 数组元素不能重复")
    if isinstance(value, str):
        errors.extend(_length(schema, value, path, "Length"))
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: 不符合 pattern 约束")
    if number:
        for key, valid in (("minimum", lambda bound: value >= bound), ("maximum", lambda bound: value <= bound), ("exclusiveMinimum", lambda bound: value > bound), ("exclusiveMaximum", lambda bound: value < bound)):
            if key in schema and not valid(schema[key]):
                errors.append(f"{path}: 不符合 {key}={schema[key]} 约束")
        if "multipleOf" in schema:
            try:
                if Decimal(str(value)) % Decimal(str(schema["multipleOf"])) != 0:
                    errors.append(f"{path}: 不符合 multipleOf 约束")
            except InvalidOperation:
                errors.append(f"{path}: multipleOf 数值超出支持范围")
    return errors


def _length(schema, value, path, suffix):
    errors = []
    for prefix, valid in (("min", lambda bound: len(value) >= bound), ("max", lambda bound: len(value) <= bound)):
        key = prefix + suffix
        if key in schema and not valid(schema[key]):
            errors.append(f"{path}: 不符合 {key}={schema[key]} 约束")
    return errors


def validate_parameters(schema, parameters):
    errors = validate_schema(schema)
    if errors:
        return errors
    if not isinstance(parameters, Mapping):
        return ["$: 参数必须是对象"]
    return validate_value(schema, parameters)

"""Static, non-executing validation for private strategy source contracts.

The checks in this module deliberately do not claim to sandbox user code.  They
parse source only, so saving or validating a draft cannot import modules, invoke
decorators, or otherwise execute a user's private strategy in the API process.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class StrategyValidationIssue:
    """One display-safe failure found while checking a strategy draft."""

    code: str
    message: str
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True)
class StrategyValidationResult:
    """The complete result from validation that never executes private source."""

    issues: tuple[StrategyValidationIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether the draft satisfies the initial execution contract."""
        return not self.issues


def validate_strategy_draft(
    source_code: str,
    *,
    parameter_schema: Mapping[str, Any],
    default_parameters: Mapping[str, Any],
) -> StrategyValidationResult:
    """Validate syntax, the fixed entry point, and the parameter object shape.

    This first contract accepts a single synchronous module-level entry point:
    ``def run(context, parameters):``.  Keeping the signature exact prevents
    the future worker protocol from having to guess which arguments private code
    expects.  Rich JSON Schema support can be added later without executing the
    strategy or changing the persistent data format.
    """
    try:
        module = ast.parse(source_code, mode="exec")
    except SyntaxError as exc:
        return StrategyValidationResult(
            issues=(
                StrategyValidationIssue(
                    code="syntax_error",
                    message="Python 语法错误。",
                    line=exc.lineno,
                    column=exc.offset,
                ),
            )
        )

    issues = [*_validate_entry_point(module)]
    issues.extend(_validate_parameter_contract(parameter_schema, default_parameters))
    return StrategyValidationResult(issues=tuple(issues))


def _validate_entry_point(module: ast.Module) -> list[StrategyValidationIssue]:
    """Require exactly one undecorated synchronous module-level ``run`` function."""
    entry_points = [
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "run"
    ]
    if not entry_points:
        return [
            StrategyValidationIssue(
                code="entrypoint_missing",
                message="策略必须定义模块级 run(context, parameters) 函数。",
            )
        ]
    if len(entry_points) > 1:
        return [
            StrategyValidationIssue(
                code="entrypoint_duplicate",
                message="策略只能定义一个模块级 run(context, parameters) 函数。",
                line=entry_points[1].lineno,
                column=entry_points[1].col_offset + 1,
            )
        ]

    entry_point = entry_points[0]
    if isinstance(entry_point, ast.AsyncFunctionDef):
        return [
            StrategyValidationIssue(
                code="entrypoint_async",
                message="run 函数必须是同步函数。",
                line=entry_point.lineno,
                column=entry_point.col_offset + 1,
            )
        ]
    if entry_point.decorator_list:
        return [
            StrategyValidationIssue(
                code="entrypoint_decorator",
                message="run 函数暂不支持装饰器。",
                line=entry_point.lineno,
                column=entry_point.col_offset + 1,
            )
        ]
    if not _has_exact_run_signature(entry_point):
        return [
            StrategyValidationIssue(
                code="entrypoint_signature",
                message="run 函数签名必须为 run(context, parameters)。",
                line=entry_point.lineno,
                column=entry_point.col_offset + 1,
            )
        ]
    return []


def _has_exact_run_signature(entry_point: ast.FunctionDef) -> bool:
    """Keep the first worker API intentionally narrow and unambiguous."""
    arguments = entry_point.args
    return (
        not arguments.posonlyargs
        and [argument.arg for argument in arguments.args] == ["context", "parameters"]
        and not arguments.defaults
        and arguments.vararg is None
        and not arguments.kwonlyargs
        and arguments.kwarg is None
    )


def _validate_parameter_contract(
    parameter_schema: Mapping[str, Any], default_parameters: Mapping[str, Any]
) -> list[StrategyValidationIssue]:
    """Check the minimal JSON Schema profile required by this first API stage."""
    issues: list[StrategyValidationIssue] = []
    declared_type = parameter_schema.get("type")
    if declared_type is not None and declared_type != "object":
        issues.append(
            StrategyValidationIssue(
                code="parameter_schema_type",
                message="参数 Schema 的顶层 type 只能为 object。",
            )
        )

    properties = parameter_schema.get("properties")
    if properties is not None and not isinstance(properties, Mapping):
        issues.append(
            StrategyValidationIssue(
                code="parameter_schema_properties",
                message="参数 Schema 的 properties 必须是对象。",
            )
        )
    elif isinstance(properties, Mapping) and any(
        not isinstance(key, str) for key in properties
    ):
        issues.append(
            StrategyValidationIssue(
                code="parameter_schema_property_name",
                message="参数 Schema 的 properties 键必须是字符串。",
            )
        )

    required = parameter_schema.get("required")
    if required is not None:
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            issues.append(
                StrategyValidationIssue(
                    code="parameter_schema_required",
                    message="参数 Schema 的 required 必须是字符串数组。",
                )
            )
        elif len(set(required)) != len(required):
            issues.append(
                StrategyValidationIssue(
                    code="parameter_schema_required_duplicate",
                    message="参数 Schema 的 required 不能包含重复字段。",
                )
            )

    if not isinstance(default_parameters, Mapping):
        # Storage validation already rejects this condition. The explicit check
        # keeps this validator independently safe for future callers.
        issues.append(
            StrategyValidationIssue(
                code="default_parameters_type",
                message="默认参数必须是对象。",
            )
        )
    if issues:
        return issues
    from app.strategies.parameter_contract import validate_parameters

    issues.extend(
        StrategyValidationIssue(code="parameter_contract_invalid", message=message)
        for message in validate_parameters(parameter_schema, default_parameters)
    )
    return issues

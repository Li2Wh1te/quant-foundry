#!/usr/bin/env python3
"""Generate installed strategy declarations/runtime refusal and client hints.

The strategy .pyi is the canonical public signature; client.py is authoritative
for the executable RunConfig. --check rejects drift without rewriting files.
This does not generate or advertise a strategy engine implementation.
"""
from __future__ import annotations
import argparse
import ast
import copy
import keyword
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk/python/quantfoundry"

def rust_wire_stub() -> str:
    """Project named serde DTO fields to JSON wire TypedDicts.

    Strictly limited to the current non-generic structs; fail on new unmapped
    types instead of silently emitting Any. Complex enum variants are explicit
    unions. Runtime strategy views use Decimal/int, wire DTOs use str/int.
    """
    selected = {"EventIdentity", "MarketUnits", "Bar", "TradeTick", "QuoteTick", "EventKey",
                "OrderIntent", "Order", "OrderResult", "Fill", "TradePage", "PositionView",
                "AccountView", "RunConfigFields", "CostOverrides", "FeeConfig", "DatedFeeComponent",
                "CommissionConfig", "FeeScope", "Instrument", "EffectiveRange", "RunSummary", "PreflightResponse", "AcceptedRunConfig", "Capabilities",
                "DependencyState", "DependencyContext", "DataRequest", "ActualScope", "BatchMetadata", "RunScope",
                "EquityPoint", "UserRecord", "LogRecord", "ResultBatch", "BatchRequest", "TradingSession", "QfError"}
    aliases = {"String":"str", "bool":"bool", "u64":"int", "u32":"int", "u16":"int", "i64":"int",
               "Nanoseconds":"str", "Sequence":"str", "SecurityKey":"str", "SessionKey":"str", "ChannelKey":"str",
               "Quantity":"int", "Money":"str", "Price":"str", "ExactDecimal":"str", "Value":"Any", "FiniteStatistic":"float",
               "RuleDate":"str", "Exchange":"Literal['shanghai', 'shenzhen', 'beijing', 'unknown']",
               "Product":"Literal['main_board_stock', 'star_stock', 'chi_next_stock', 'beijing_stock', 'equity_etf', 'bond_etf', 'money_etf', 'gold_etf', 'commodity_etf', 'cross_border_etf', 'index', 'unknown']",
               "InvestorKind":"Literal['resident_individual', 'resident_enterprise', 'other', 'unknown']",
               "RunConfig":"RunConfigFields", "ErrorCode":"str", "QfError":"ErrorDTO",
               "Currency":"Literal['CNY']", "QuantityUnit":"Literal['shares']",
               "TimeUnit":"Literal['utc_nanoseconds']", "RoundingPolicy":"Literal['half_even', 'half_up', 'toward_zero', 'away_from_zero']",
               "Frequency":"Literal['1d', '1m', '5m', '15m', '30m', '60m', 'tick']",
               "ExecutionModel":"Literal['bar_next_interval_v1', 'trade_tick_v1', 'quote_tick_v1']",
               "ResultSampling":"Literal['session_close', 'bar_close']", "Side":"Literal['buy', 'sell']",
               "TimeInForce":"Literal['day', 'gtc']", "EventPhase":"Literal['settlement', 'market', 'notification', 'callback']",
               "Adjustment":"Literal['none', 'pre', 'post']", "RunStatus":"Literal['queued', 'starting', 'running', 'succeeded', 'failed', 'cancelled']",
               "OrderStatus":"Literal['accepted', 'open', 'partially_filled', 'filled', 'cancelled', 'expired', 'rejected']",
               "FeeComponentKind":"Literal['stamp_duty', 'transfer_fee', 'regulatory_fee', 'handling_fee']", "LogLevel":"Literal['info', 'warning', 'error']"}

    def split(value: str) -> list[str]:
        result, part, depth = [], [], 0
        for char in value:
            depth += (char == "<") - (char == ">")
            if char == "," and depth == 0:
                result.append("".join(part).strip()); part = []
            else:
                part.append(char)
        if part:
            result.append("".join(part).strip())
        return [item for item in result if item]

    def convert(value: str) -> str:
        value = value.strip()
        if "::" in value and "<" not in value:
            value = value.rsplit("::", 1)[-1]
        if value in aliases:
            return aliases[value]
        if value in selected or value in {"OrderStyle", "IntentValue", "FeeFact", "RecordValue", "ResultRecord", "RuleOrigin"}:
            return value
        match = re.fullmatch(r"(Option|Vec|Map|BTreeMap)<(.*)>", value)
        if match:
            inner = split(match[2])
            if match[1] == "Option": return convert(inner[0]) + " | None"
            if match[1] == "Vec": return "list[" + convert(inner[0]) + "]"
            return "dict[" + ", ".join(map(convert, inner)) + "]"
        raise ValueError(f"unmapped Rust wire type: {value}")

    source = "\n".join(p.read_text() for p in sorted((ROOT / "engine/crates/qf-core/src").rglob("*.rs")))
    source = re.sub(r"//[^\n]*", "", source)
    output = ["# Generated from qf-core serde DTO fields; decimal/time wire values are strings.",
              "from typing import Any, Literal, NotRequired, TypedDict\n"]
    found = set()
    for name, body in re.findall(r"pub struct (\w+)\s*\{([^{}]*)\}", source):
        if name not in selected: continue
        found.add(name)
        fields = []
        for field in split(re.sub(r"#\[[^\]]*\]", "", body)):
            match = re.fullmatch(r"\s*(?:pub\s+)?(\w+)\s*:\s*(.*?)\s*", field, re.S)
            if not match: raise ValueError(f"unmapped Rust field {name}: {field}")
            hint = convert(match[2])
            # Config defaults/omitted options are not required request keys.
            optional = name == "CostOverrides" or (name == "RunConfigFields" and match[1] in {
                "benchmark", "currency", "reference_calendar", "seed", "participation_rate", "slippage_bps",
                "risk_free_rate", "annualization_sessions", "result_sampling", "cost_overrides"})
            if optional:
                if name == "CostOverrides" or match[1] in {"reference_calendar", "cost_overrides"}:
                    hint = hint.removesuffix(" | None")
                hint = "NotRequired[" + hint + "]"
            fields.append((match[1], hint))
        public_name = 'ErrorDTO' if name == 'QfError' else name
        if any(keyword.iskeyword(field) for field, _ in fields):
            # Preserve Rust's exact JSON key (e.g. EffectiveRange.from) without
            # emitting invalid Python syntax or a competing renamed wire field.
            hints = ", ".join(f"{field!r}: {hint!r}" for field, hint in fields)
            output.append(f"{public_name} = TypedDict({public_name!r}, {{{hints}}})")
        else:
            output.append(f"class {public_name}(TypedDict):")
            output.extend(f"    {field}: {hint}" for field, hint in fields)
        output.append("")
    if found != selected: raise ValueError(f"missing Rust DTOs: {selected-found}")
    output.append('''class OfficialRuleOrigin(TypedDict):
    kind: Literal['official']
    reference: str
class SyntheticRuleOrigin(TypedDict):
    kind: Literal['synthetic']
    reference: str
RuleOrigin = OfficialRuleOrigin | SyntheticRuleOrigin
class MarketStyle(TypedDict):
    kind: Literal['market']
class LimitStyle(TypedDict):
    kind: Literal['limit']
    price: str
OrderStyle = MarketStyle | LimitStyle
class QuantityIntent(TypedDict):
    kind: Literal['quantity', 'target_quantity']
    value: int
class DecimalIntent(TypedDict):
    kind: Literal['value', 'target_value', 'target_percent']
    value: str
IntentValue = QuantityIntent | DecimalIntent
class ApplicableFee(TypedDict):
    applicability: Literal['applicable']
    rate: str
    basis: str
class InapplicableFee(TypedDict):
    applicability: Literal['inapplicable']
    basis: str
FeeFact = ApplicableFee | InapplicableFee
class BarEvent(TypedDict):
    kind: Literal['bar']
    event: Bar
class TradeEvent(TypedDict):
    kind: Literal['trade_tick']
    event: TradeTick
class QuoteEvent(TypedDict):
    kind: Literal['quote_tick']
    event: QuoteTick
MarketEvent = BarEvent | TradeEvent | QuoteEvent
class RunSucceeded(TypedDict):
    status: Literal['succeeded']
    result_id: str
class RunFailed(TypedDict):
    status: Literal['failed']
    error: ErrorDTO
    partial: bool
class RunCancelled(TypedDict):
    status: Literal['cancelled']
    partial: bool
RunOutcome = RunSucceeded | RunFailed | RunCancelled
class DecimalRecord(TypedDict):
    kind: Literal['decimal']
    value: str
class IntegerRecord(TypedDict):
    kind: Literal['integer']
    value: int
class TextRecord(TypedDict):
    kind: Literal['text']
    value: str
class StatisticRecord(TypedDict):
    kind: Literal['statistic']
    value: float
RecordValue = DecimalRecord | IntegerRecord | TextRecord | StatisticRecord
class EquityRecord(TypedDict):
    kind: Literal['equity']
    record: EquityPoint
class OrderRecord(TypedDict):
    kind: Literal['order']
    record: Order
class TradeRecord(TypedDict):
    kind: Literal['trade']
    record: Fill
class PositionRecord(TypedDict):
    kind: Literal['position']
    record: PositionView
class UserRecordEnvelope(TypedDict):
    kind: Literal['record']
    record: UserRecord
class LogRecordEnvelope(TypedDict):
    kind: Literal['log']
    record: LogRecord
ResultRecord = EquityRecord | OrderRecord | TradeRecord | PositionRecord | UserRecordEnvelope | LogRecordEnvelope
''')
    return "\n".join(output)

def client_stub() -> str:
    tree = ast.parse((SDK / "client.py").read_text())
    output = [ast.ImportFrom(module="typing", names=[ast.alias(name=v) for v in ("Any", "Mapping", "Sequence")], level=0)]
    for item in tree.body:
        if isinstance(item, ast.ClassDef) and item.name in {"RunConfig", "BacktestClient"}:
            item = copy.deepcopy(item)
            for method in item.body:
                if isinstance(method, ast.FunctionDef):
                    method.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
            output.append(item)
    return "# Generated from SDK client.py; run scripts/generate_s3_contracts.py.\n" + ast.unparse(ast.Module(body=output, type_ignores=[])) + "\n"

def strategy_runtime() -> str:
    tree = ast.parse((ROOT / "contracts/strategy_api.pyi").read_text())
    # Return annotations use forward references; pandas is only a type dependency.
    output = ast.parse('''from __future__ import annotations
from typing import TYPE_CHECKING
from .client import _unavailable
from ._native import ContractError
if TYPE_CHECKING:
    from pandas import DataFrame
''').body
    data_names = {'Filter', 'CurrentQuote', 'Tick', 'get_price', 'get_current_data', 'get_fundamentals',
                  'get_valuation', 'get_index_stocks', 'get_industry', 'get_instruments'}
    for item in tree.body:
        if isinstance(item, (ast.FunctionDef, ast.ClassDef)) and item.name in data_names:
            continue
        if isinstance(item, ast.ImportFrom) and item.module == "pandas":
            continue
        item = copy.deepcopy(item)
        if isinstance(item, ast.FunctionDef):
            item.body = ast.parse("_unavailable()").body
        elif isinstance(item, ast.ClassDef):
            if item.name == "StrategyError":
                output.extend(ast.parse("StrategyError = ContractError").body)
                continue
            for method in item.body:
                if isinstance(method, ast.FunctionDef):
                    method.body = ast.parse("_unavailable()").body
            if item.name == "MarketOrder":
                item.body = ast.parse("pass").body
            if item.name == "LimitOrder":
                # Precision conversion is implemented; no trading action occurs.
                item.body[-1].body = ast.parse("from .numeric import price_input\nself.price = price_input(price)").body
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == "log":
            item.value = ast.Call(func=ast.Name(id="_Logger", ctx=ast.Load()), args=[], keywords=[])
        output.append(item)
    output.append(ast.ImportFrom(module='data', names=[ast.alias(name=n) for n in sorted(data_names)], level=1))
    return "# Generated from contracts/strategy_api.pyi. D01 runtime functions explicitly refuse.\n" + ast.unparse(ast.Module(body=output, type_ignores=[])) + "\n"

def outputs() -> dict[Path, str]:
    declarations = (ROOT / "contracts/strategy_api.pyi").read_text()
    wire = rust_wire_stub()
    return {ROOT / "contracts/client.pyi": client_stub(), SDK / "client.pyi": client_stub(),
            SDK / "api.pyi": declarations, SDK / "api.py": strategy_runtime(),
            ROOT / "contracts/shared_types.pyi": wire, SDK / "contracts.pyi": wire,
            SDK / "contracts.py": "from __future__ import annotations\n" + wire}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path, expected in outputs().items():
        ast.parse(expected, filename=str(path))
        if args.check:
            if not path.exists() or path.read_text() != expected:
                raise SystemExit(f"stale generated contract: {path.relative_to(ROOT)}")
        else:
            path.write_text(expected)
    print("S3 public Python signatures are synchronized")

if __name__ == "__main__":
    main()

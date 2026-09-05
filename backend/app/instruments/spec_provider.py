"""Point-in-time orchestration for one instrument specification.

The provider is intentionally a thin composition root.  Identity, mapping,
rule-fact, exception, capability, and calendar stores remain replaceable
ports; this module only applies their fixed read order and turns failures
into an immutable qualification result.  It never reads the mutable ETF
catalogue and never creates a dynamic candidate set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import UUID

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.instruments.domain import (
    CorporateActionRequirement,
    InstrumentCapabilities,
    InstrumentDisplay,
    InstrumentIdentityFact,
    InstrumentSpec,
    VersionedReference,
)
from app.instruments.rule_exceptions_repository import PersistedExceptionSet
from app.instruments.rules.contracts import (
    ParseMode,
    ResolutionStatus,
    RuleFactCandidate,
    RulePackageIssue,
    RulePackageIssueCode,
    RulePackageResolution,
    canonical_payload,
    exception_set_content_hash,
    stable_hash,
)
from app.instruments.rules.etf_china import PACKAGE_KEY, PACKAGE_VERSION, register_china_listed_etf_rules
from app.instruments.rules.registry import RulePackageRegistry
from app.instruments.rules.resolver import RulePackageResolver


DEFAULT_RULE_PACKAGE_REFERENCE = VersionedReference(
    key=PACKAGE_KEY, version=PACKAGE_VERSION
)
DEFAULT_MAPPING_SOURCE = "etf_ingestion"


@dataclass(frozen=True, slots=True)
class InstrumentQualificationIssue:
    """One machine-readable reason a single instrument is not eligible."""

    code: str
    message: str
    field: str | None = None
    instrument_id: UUID | None = None
    details: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise DomainValidationError("qualification issue code must be non-blank")
        if not isinstance(self.message, str) or not self.message.strip():
            raise DomainValidationError("qualification issue message must be non-blank")
        if self.instrument_id is not None and not isinstance(self.instrument_id, UUID):
            raise DomainValidationError("qualification issue instrument_id must be a UUID")
        if not isinstance(self.details, Mapping):
            raise DomainValidationError("qualification issue details must be a mapping")
        # Keep details JSON-safe and immutable.  The rule contracts' canonical
        # serializer also normalizes UUID/date/Decimal values for hashing.
        canonical_payload(self.details)
        object.__setattr__(self, "details", _freeze_json_like(self.details))


@dataclass(frozen=True, slots=True)
class InstrumentSpecQualification:
    """Result consumed by task package 15 for one instrument and one instant."""

    instrument_id: UUID
    status: ResolutionStatus
    spec: InstrumentSpec | None
    issues: tuple[InstrumentQualificationIssue, ...] = ()
    provenance: Mapping[str, Any] = MappingProxyType({})
    calendar_id: str | None = None
    identity_evidence: Mapping[str, Any] = MappingProxyType({})
    mapping_evidence: Mapping[str, Any] = MappingProxyType({})
    rule_evidence: Mapping[str, Any] = MappingProxyType({})
    capability_evidence: Mapping[str, Any] = MappingProxyType({})
    resolution_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        if not isinstance(self.status, ResolutionStatus):
            raise DomainValidationError("status must be a ResolutionStatus")
        if self.spec is not None and not isinstance(self.spec, InstrumentSpec):
            raise DomainValidationError("spec must be an InstrumentSpec or None")
        issues = tuple(self.issues)
        if any(not isinstance(item, InstrumentQualificationIssue) for item in issues):
            raise DomainValidationError("issues must contain InstrumentQualificationIssue instances")
        if self.status is ResolutionStatus.READY and (self.spec is None or issues):
            raise DomainValidationError("a ready qualification requires a complete spec and no issues")
        if self.status is ResolutionStatus.BLOCKED and self.spec is not None:
            raise DomainValidationError("a blocked qualification cannot carry a spec")
        for name in (
            "provenance",
            "identity_evidence",
            "mapping_evidence",
            "rule_evidence",
            "capability_evidence",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise DomainValidationError(f"{name} must be a mapping")
            canonical_payload(value)
            object.__setattr__(self, name, _freeze_json_like(value))
        if self.calendar_id is not None and (
            not isinstance(self.calendar_id, str) or not self.calendar_id.strip()
        ):
            raise DomainValidationError("calendar_id must be non-blank when provided")
        computed = _qualification_hash(self)
        if self.resolution_hash and self.resolution_hash != computed:
            raise DomainValidationError("resolution_hash does not match qualification content")
        object.__setattr__(self, "resolution_hash", computed)

    @property
    def eligible(self) -> bool:
        return self.status is ResolutionStatus.READY and self.spec is not None

    @property
    def ready(self) -> bool:
        return self.eligible

    @property
    def blocked(self) -> bool:
        return not self.eligible

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    @property
    def rule_resolution_hash(self) -> str | None:
        value = self.rule_evidence.get("resolution_hash")
        return value if isinstance(value, str) else None

    @property
    def identity_status(self) -> str:
        return "blocked" if any(issue.code.startswith("identity_") for issue in self.issues) else "ready"

    @property
    def mapping_status(self) -> str:
        return "blocked" if any(issue.code.startswith("identity_mapping_") for issue in self.issues) else "ready"

    @property
    def rule_status(self) -> str:
        return "blocked" if any(issue.code.startswith("RULE_") for issue in self.issues) else "ready"

    @property
    def qualification_hash(self) -> str:
        return self.resolution_hash


# Common names used by downstream task packages; they are aliases, not a
# second result model.
InstrumentEligibility = InstrumentSpecQualification
SingleInstrumentQualification = InstrumentSpecQualification
QualificationIssue = InstrumentQualificationIssue


class InstrumentSpecProvider:
    """Compose existing PIT repositories into complete ETF ``InstrumentSpec`` objects."""

    def __init__(
        self,
        identity_repository: object | None = None,
        display_repository: object | None = None,
        mapping_repository: object | None = None,
        rule_registry: RulePackageRegistry | None = None,
        rule_fact_repository: object | None = None,
        exception_repository: object | None = None,
        capability_provider: object | None = None,
        calendar_provider: object | None = None,
        *,
        mapping_source: str = DEFAULT_MAPPING_SOURCE,
        default_rule_package_reference: VersionedReference = DEFAULT_RULE_PACKAGE_REFERENCE,
        **aliases: object,
    ) -> None:
        # Accept the vocabulary used by the task-package documents without
        # duplicating implementations or forcing callers to wrap repositories.
        identity_repository = aliases.pop("identity_provider", identity_repository)
        identity_repository = aliases.pop("identity_facts", identity_repository)
        display_repository = aliases.pop("display_provider", display_repository)
        display_repository = aliases.pop("display_facts", display_repository)
        mapping_repository = aliases.pop("mapping_provider", mapping_repository)
        mapping_repository = aliases.pop("code_mapping_repository", mapping_repository)
        rule_registry = aliases.pop("registry", rule_registry)
        rule_registry = aliases.pop("rule_package_registry", rule_registry)
        rule_fact_repository = aliases.pop("rule_fact_provider", rule_fact_repository)
        rule_fact_repository = aliases.pop("rule_facts_repository", rule_fact_repository)
        rule_fact_repository = aliases.pop("facts_repository", rule_fact_repository)
        rule_fact_repository = aliases.pop("facts", rule_fact_repository)
        exception_repository = aliases.pop("exception_provider", exception_repository)
        exception_repository = aliases.pop("exception_set_repository", exception_repository)
        exception_repository = aliases.pop("exceptions", exception_repository)
        capability_provider = aliases.pop("capabilities_provider", capability_provider)
        capability_provider = aliases.pop("status_provider", capability_provider)
        if "capabilities" in aliases and capability_provider is None:
            capability_provider = aliases.pop("capabilities")
        calendar_provider = aliases.pop("session_provider", calendar_provider)
        calendar_provider = aliases.pop("calendar_resolver", calendar_provider)
        if aliases:
            unknown = ", ".join(sorted(aliases))
            raise TypeError(f"unexpected InstrumentSpecProvider arguments: {unknown}")
        if not isinstance(default_rule_package_reference, VersionedReference):
            raise DomainValidationError("default_rule_package_reference must be a VersionedReference")
        if not isinstance(mapping_source, str) or not mapping_source.strip():
            raise DomainValidationError("mapping_source must be non-blank text")
        self.identity_repository = identity_repository
        if display_repository is None and any(
            callable(getattr(identity_repository, name, None))
            for name in ("resolve_display_at", "resolve_display")
        ):
            display_repository = identity_repository
        self.display_repository = display_repository
        self.mapping_repository = mapping_repository or identity_repository
        self.rule_fact_repository = rule_fact_repository or (
            identity_repository
            if any(callable(getattr(identity_repository, name, None)) for name in ("list_facts", "list_rule_facts"))
            else None
        )
        self.exception_repository = exception_repository or (
            identity_repository
            if any(callable(getattr(identity_repository, name, None)) for name in ("load_exception_set", "resolve_exception_set"))
            else None
        )
        self.capability_provider = capability_provider
        self.calendar_provider = calendar_provider
        self.mapping_source = mapping_source.strip()
        self.default_rule_package_reference = default_rule_package_reference
        if rule_registry is None:
            rule_registry = RulePackageRegistry()
            # The v1 package is a schema/semantic contract, not a source of
            # production values; registering it here keeps the provider usable
            # in small composition roots while preserving exact key/version use.
            register_china_listed_etf_rules(rule_registry)
        if not isinstance(rule_registry, RulePackageRegistry):
            raise DomainValidationError("rule_registry must be a RulePackageRegistry")
        self.rule_registry = rule_registry
        self._resolver = RulePackageResolver(rule_registry)

    def resolve_spec(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
        rule_package_reference: VersionedReference | None = None,
        exception_set_reference: VersionedReference | None = None,
    ) -> InstrumentSpec | None:
        """Return one complete spec, or ``None`` when the instant is blocked."""

        result = self.qualify(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
            rule_package_reference=rule_package_reference,
            exception_set_reference=exception_set_reference,
        )
        return result.spec

    def qualify(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
        rule_package_reference: VersionedReference | None = None,
        exception_set_reference: VersionedReference | None = None,
    ) -> InstrumentSpecQualification:
        """Resolve and qualify one instrument without querying any universe."""

        if not isinstance(instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        effective = _aware_datetime(effective_at, "effective_at")
        cutoff = _aware_datetime(data_cutoff, "data_cutoff")
        package = rule_package_reference or self.default_rule_package_reference
        if not isinstance(package, VersionedReference):
            raise DomainValidationError("rule_package_reference must be a VersionedReference")

        issues: list[InstrumentQualificationIssue] = []
        provenance: dict[str, Any] = {
            "instrument_id": str(instrument_id),
            "effective_at": effective.isoformat(),
            "data_cutoff": cutoff.isoformat(),
            "rule_package_reference": canonical_payload(package),
        }
        identity_fact: InstrumentIdentityFact | None = None
        display = InstrumentDisplay(instrument_id=instrument_id)
        mapping = None

        # 1. Exact package load, then PIT identity/display/mapping.  The
        # ordering is deliberate and shared by fixed preflight and task 15.
        try:
            definition = self.rule_registry.require(package)
            provenance["rule_package_semantic_hash"] = definition.semantic_hash
        except DomainValidationError as exc:
            issues.append(
                self._issue(
                    RulePackageIssueCode.RULE_PACKAGE_MISMATCH.value,
                    str(exc) or "规则包不存在或版本不匹配",
                    instrument_id=instrument_id,
                    field="rule_package_reference",
                    details={"rule_package_reference": canonical_payload(package)},
                )
            )
            return self._blocked(instrument_id, issues, provenance)

        identity_resolution = None
        try:
            identity_resolution = _invoke(
                self.identity_repository,
                ("resolve_identity_at", "resolve_identity", "resolve"),
                instrument_id,
                effective_at=effective,
                data_cutoff=cutoff,
            )
        except Exception as exc:  # repository errors are data qualification failures
            if self.identity_repository is not None:
                issues.append(self._exception_issue(exc, instrument_id, "identity"))
        if identity_resolution is not None and hasattr(identity_resolution, "identity_fact"):
            identity_fact = getattr(identity_resolution, "identity_fact", None)
        elif isinstance(identity_resolution, InstrumentIdentityFact):
            identity_fact = identity_resolution
        if identity_fact is None:
            issues.append(
                self._issue(
                    "identity_fact_missing",
                    "请求时点缺少可见的 PIT 身份事实",
                    instrument_id=instrument_id,
                )
            )
            return self._blocked(instrument_id, issues, provenance)
        if identity_fact.instrument_id != instrument_id:
            issues.append(self._issue("identity_fact_instrument_mismatch", "身份事实与请求标的不一致", instrument_id=instrument_id))
        if identity_fact.asset_class not in definition.supported_asset_classes:
            issues.append(
                self._issue(
                    RulePackageIssueCode.RULE_PACKAGE_MISMATCH.value,
                    "身份资产类别与规则包不匹配",
                    instrument_id=instrument_id,
                    field="asset_class",
                    details={"asset_class": identity_fact.asset_class},
                )
            )
        if not identity_fact.exchange:
            issues.append(self._issue("identity_exchange_missing", "身份事实缺少交易所，禁止从代码推断", instrument_id=instrument_id, field="exchange"))
        if not identity_fact.currency:
            issues.append(self._issue("identity_currency_missing", "身份事实缺少币种", instrument_id=instrument_id, field="currency"))
        if not identity_fact.calendar_id:
            issues.append(self._issue("identity_calendar_missing", "身份事实缺少交易日历", instrument_id=instrument_id, field="calendar_id"))
        provenance["identity"] = _fact_provenance(identity_fact)

        if self.display_repository is not None:
            try:
                display_result = _invoke(
                    self.display_repository,
                    ("resolve_display_at", "resolve_display", "resolve"),
                    instrument_id,
                    effective_at=effective,
                    data_cutoff=cutoff,
                )
                if display_result is not None and hasattr(display_result, "display"):
                    display_result = getattr(display_result, "display")
                if isinstance(display_result, InstrumentDisplay):
                    display = display_result
                elif display_result is not None:
                    issues.append(self._issue("display_fact_invalid", "PIT 展示事实无法转换为展示对象", instrument_id=instrument_id))
            except Exception as exc:
                # Display labels are optional, but a corrupt authoritative row is
                # not silently ignored because it would make the result ambiguous.
                issues.append(self._exception_issue(exc, instrument_id, "display"))
        provenance["display"] = {
            "instrument_id": str(display.instrument_id),
            "trading_code": display.trading_code,
            "name": display.name,
            "display_name": display.display_name,
        }

        if self.mapping_repository is None:
            issues.append(self._issue("identity_mapping_incomplete", "缺少 PIT 代码映射读取能力", instrument_id=instrument_id, field="mapping"))
        else:
            try:
                mappings = _invoke(
                    self.mapping_repository,
                    ("resolve_code_mappings", "list_mappings", "mappings"),
                    instrument_id,
                    source=self.mapping_source,
                    start_date=effective.date(),
                    end_date=effective.date(),
                    data_cutoff=cutoff,
                )
                mappings = tuple(mappings or ())
                if len(mappings) != 1 or not mappings[0].covers(effective.date()):
                    issues.append(self._issue("identity_mapping_incomplete", "PIT 代码映射未完整覆盖请求时点", instrument_id=instrument_id, field="mapping"))
                else:
                    mapping = mappings[0]
                    if getattr(mapping, "instrument_id", instrument_id) != instrument_id:
                        issues.append(self._issue("identity_mapping_instrument_mismatch", "代码映射与请求标的不一致", instrument_id=instrument_id, field="mapping"))
                        mapping = None
                    elif getattr(mapping, "source", self.mapping_source) != self.mapping_source:
                        issues.append(self._issue("identity_mapping_source_mismatch", "代码映射来源与请求来源不一致", instrument_id=instrument_id, field="mapping"))
                        mapping = None
                    else:
                        provenance["mapping"] = _fact_provenance(mapping)
            except Exception as exc:
                issues.append(self._exception_issue(exc, instrument_id, "mapping"))

        if issues:
            return self._blocked(instrument_id, issues, provenance, calendar_id=identity_fact.calendar_id)

        # 2. Rules: exact facts, exact named exception set, fixed resolver.
        facts: tuple[RuleFactCandidate, ...] = ()
        try:
            facts = tuple(
                _invoke(
                    self.rule_fact_repository,
                    ("list_facts", "list_rule_facts", "facts"),
                    instrument_id,
                    package,
                    start_date=effective.date(),
                    end_date=effective.date(),
                    data_cutoff=cutoff,
                )
                or ()
            )
        except Exception as exc:
            if self.rule_fact_repository is not None:
                issues.append(self._exception_issue(exc, instrument_id, "rule_fact"))
        if not facts:
            issues.append(self._issue(RulePackageIssueCode.RULE_FACT_MISSING.value, "缺少请求时点可见的规则事实", instrument_id=instrument_id))

        exception_definition = None
        persisted_exception = None
        if exception_set_reference is not None:
            if self.exception_repository is None:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_EXCEPTION_SET_MISSING.value,
                        "缺少例外集合读取能力",
                        instrument_id=instrument_id,
                        field="exception_set_reference",
                    )
            )
            else:
                try:
                    persisted_exception = _invoke(
                        self.exception_repository,
                        ("load_exception_set", "resolve_exception_set", "get_exception_set"),
                        exception_set_reference,
                        data_cutoff=cutoff,
                    )
                    if isinstance(persisted_exception, PersistedExceptionSet):
                        exception_definition = persisted_exception.definition
                    elif hasattr(persisted_exception, "definition"):
                        exception_definition = persisted_exception.definition
                    else:
                        exception_definition = persisted_exception
                    if exception_definition is None:
                        issues.append(self._issue(RulePackageIssueCode.RULE_EXCEPTION_SET_MISSING.value, "例外集合在数据截止点不可见", instrument_id=instrument_id))
                except Exception as exc:
                    issues.append(self._exception_issue(exc, instrument_id, "exception_set"))

        resolution: RulePackageResolution | None = None
        if not any(issue.code == RulePackageIssueCode.RULE_FACT_MISSING.value for issue in issues) and not any(
            issue.code == RulePackageIssueCode.RULE_EXCEPTION_SET_MISSING.value for issue in issues
        ):
            try:
                resolution = self._resolver.resolve(
                    package,
                    instrument_id=instrument_id,
                    asset_class=identity_fact.asset_class,
                    effective_date=effective.date(),
                    data_cutoff=cutoff,
                    facts=facts,
                    exception_sets=(() if exception_definition is None else (exception_definition,)),
                    mode=ParseMode.FORMAL,
                )
                issues.extend(self._rule_issues(resolution.issues))
            except Exception as exc:
                issues.append(self._exception_issue(exc, instrument_id, "rule_resolution"))
        if resolution is not None:
            provenance["rule"] = _resolution_provenance(resolution, facts, persisted_exception)

        if issues:
            return self._blocked(
                instrument_id,
                issues,
                provenance,
                calendar_id=identity_fact.calendar_id,
                mapping=mapping,
            )

        assert resolution is not None  # guarded by the issue checks above
        calendar = self._calendar(identity_fact.calendar_id, effective, cutoff)
        if calendar is None:
            issues.append(self._issue("calendar_session_missing", "交易日历未返回有效会话能力", instrument_id=instrument_id, field="trading_hours"))
        capabilities, capability_evidence = self._capabilities(instrument_id, effective, cutoff)
        if capabilities is None:
            issues.append(self._issue(RulePackageIssueCode.RULE_CAPABILITY_FACT_MISSING.value, "缺少标的能力事实，禁止构造半规格", instrument_id=instrument_id, field="capabilities"))
        if issues:
            provenance["capabilities"] = capability_evidence
            return self._blocked(instrument_id, issues, provenance, calendar_id=identity_fact.calendar_id, mapping=mapping)

        values = resolution.normalized_values
        if str(values.get("currency", "")).upper() != identity_fact.currency.upper():
            issues.append(self._issue(RulePackageIssueCode.RULE_FIELD_CONFLICT.value, "身份币种与规则事实币种不一致", instrument_id=instrument_id, field="currency"))
        if issues:
            return self._blocked(instrument_id, issues, provenance, calendar_id=identity_fact.calendar_id, mapping=mapping)
        trading_hours, template = _calendar_values(calendar, values["trading_session_template"])
        if template != values["trading_session_template"]:
            issues.append(
                self._issue(
                    RulePackageIssueCode.RULE_FIELD_CONFLICT.value,
                    "交易日历返回的会话模板与规则事实不一致",
                    instrument_id=instrument_id,
                    field="trading_session_template",
                )
            )
        if trading_hours is None:
            issues.append(self._issue("calendar_session_missing", "交易日历未解析出交易时段", instrument_id=instrument_id, field="trading_hours"))
            return self._blocked(instrument_id, issues, provenance, calendar_id=identity_fact.calendar_id, mapping=mapping)

        valid_from, valid_to = _intersection_interval(identity_fact, resolution)
        try:
            spec = InstrumentSpec(
                instrument_id=instrument_id,
                display=display,
                asset_class=identity_fact.asset_class,
                exchange=identity_fact.exchange,
                currency=identity_fact.currency,
                calendar_id=identity_fact.calendar_id,
                price_precision=values["price_precision"],
                quantity_precision=values["quantity_precision"],
                price_tick=values["price_tick"],
                lot_size=values["lot_size"],
                minimum_order_quantity=values["minimum_order_quantity"],
                contract_multiplier=values["contract_multiplier"],
                trading_session_template=template,
                trading_hours=trading_hours,
                settlement_rule_class=values["settlement_rule_class"],
                sellable_rule=values["sellable_rule"],
                fee_categories=frozenset(values["fee_categories"]),
                trading_status_policy=values["trading_status_applicability"],
                order_types=frozenset(values["order_types"]),
                price_limit_rule=values["price_limit_rule"],
                cash_availability_rule=values["cash_availability_rule"],
                position_availability_rule=values["position_availability_rule"],
                capabilities=capabilities,
                rule_package_reference=package,
                rule_exception_reference=resolution.exception_set_reference,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        except Exception as exc:
            issues.append(self._exception_issue(exc, instrument_id, "spec"))
            return self._blocked(instrument_id, issues, provenance, calendar_id=identity_fact.calendar_id, mapping=mapping)

        provenance["calendar"] = _provenance_value(calendar)
        provenance["capabilities"] = capability_evidence
        return InstrumentSpecQualification(
            instrument_id=instrument_id,
            status=ResolutionStatus.READY,
            spec=spec,
            provenance=provenance,
            calendar_id=identity_fact.calendar_id,
            identity_evidence=_fact_provenance(identity_fact),
            mapping_evidence=_fact_provenance(mapping) if mapping is not None else {},
            rule_evidence=provenance.get("rule", {}),
            capability_evidence=capability_evidence,
        )

    # Names below are intentionally aliases for task-package consumers.
    resolve_qualification = qualify
    resolve_eligibility = qualify
    evaluate = qualify

    def _calendar(self, calendar_id: str, effective: datetime, cutoff: datetime) -> object | None:
        provider = self.calendar_provider
        if provider is None:
            return None
        try:
            return _invoke(
                provider,
                ("resolve_session", "resolve_trading_hours", "resolve_calendar_at", "resolve_calendar", "resolve", "session", "fact"),
                calendar_id,
                effective_at=effective,
                effective_date=effective.date(),
                data_cutoff=cutoff,
            )
        except TypeError:
            # Minimal calendar ports commonly expose ``fact(calendar_id, day)``.
            try:
                return provider.fact(calendar_id, effective.date())
            except Exception:
                return None
        except Exception:
            return None

    def _capabilities(self, instrument_id: UUID, effective: datetime, cutoff: datetime) -> tuple[InstrumentCapabilities | None, Mapping[str, Any]]:
        provider = self.capability_provider
        if isinstance(provider, InstrumentCapabilities):
            return provider, {"source": "injected", "value": _capabilities_payload(provider)}
        if provider is None:
            return None, {}
        try:
            result = _invoke(
                provider,
                ("resolve_capabilities", "check_capabilities", "resolve", "get_capabilities", "capabilities"),
                instrument_id,
                effective_at=effective,
                data_cutoff=cutoff,
            )
            evidence: Mapping[str, Any] = {}
            if isinstance(result, tuple) and len(result) == 2:
                result, evidence = result
            if isinstance(result, InstrumentCapabilities):
                return result, evidence or {"source": type(provider).__name__}
            if isinstance(result, Mapping):
                raw = result.get("capabilities", result)
                if isinstance(raw, InstrumentCapabilities):
                    return raw, result.get("provenance", {}) if isinstance(result.get("provenance", {}), Mapping) else {}
                if isinstance(raw, Mapping):
                    return InstrumentCapabilities(**raw), result.get("provenance", {}) if isinstance(result.get("provenance", {}), Mapping) else {}
        except Exception:
            pass
        return None, {}

    @staticmethod
    def _issue(code: str, message: str, *, instrument_id: UUID, field: str | None = None, details: Mapping[str, Any] | None = None) -> InstrumentQualificationIssue:
        return InstrumentQualificationIssue(code=code, message=message, field=field, instrument_id=instrument_id, details=details or {})

    def _exception_issue(self, exc: Exception, instrument_id: UUID, field: str) -> InstrumentQualificationIssue:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not code.strip():
            code = "instrument_spec_unresolvable"
        details = getattr(exc, "details", {})
        if not isinstance(details, Mapping):
            details = {"error_type": type(exc).__name__}
        return self._issue(code, str(exc) or "标的规格资格解析失败", instrument_id=instrument_id, field=field, details=details)

    @staticmethod
    def _rule_issues(issues: Sequence[RulePackageIssue]) -> list[InstrumentQualificationIssue]:
        return [
            InstrumentQualificationIssue(
                code=issue.code.value,
                message=issue.message,
                field=issue.field,
                instrument_id=issue.instrument_id,
                details=issue.details or {},
            )
            for issue in issues
        ]

    @staticmethod
    def _blocked(instrument_id: UUID, issues: Sequence[InstrumentQualificationIssue], provenance: Mapping[str, Any], *, calendar_id: str | None = None, mapping: object | None = None) -> InstrumentSpecQualification:
        return InstrumentSpecQualification(
            instrument_id=instrument_id,
            status=ResolutionStatus.BLOCKED,
            spec=None,
            issues=tuple(issues),
            provenance=provenance,
            calendar_id=calendar_id,
            identity_evidence=provenance.get("identity", {}),
            mapping_evidence=_fact_provenance(mapping) if mapping is not None else provenance.get("mapping", {}),
            rule_evidence=provenance.get("rule", {}),
            capability_evidence=provenance.get("capabilities", {}),
        )


# Descriptive aliases keep imports stable if a caller prefers the
# orchestration terminology used in the task package.
InstrumentSpecOrchestrator = InstrumentSpecProvider
DefaultInstrumentSpecProvider = InstrumentSpecProvider
InstrumentSpecResolver = InstrumentSpecProvider
SingleInstrumentQualificationProvider = InstrumentSpecProvider


def resolve_instrument_qualification(provider: InstrumentSpecProvider, instrument_id: UUID, *, effective_at: datetime, data_cutoff: datetime, rule_package_reference: VersionedReference | None = None, exception_set_reference: VersionedReference | None = None) -> InstrumentSpecQualification:
    """Small functional façade for task 15; it never performs universe work."""

    if not isinstance(provider, InstrumentSpecProvider):
        raise DomainValidationError("provider must be an InstrumentSpecProvider")
    return provider.qualify(
        instrument_id,
        effective_at=effective_at,
        data_cutoff=data_cutoff,
        rule_package_reference=rule_package_reference,
        exception_set_reference=exception_set_reference,
    )


def _invoke(target: object | None, names: Sequence[str], *args: object, **kwargs: object) -> object:
    if target is None:
        raise DomainValidationError("required provider is not configured")
    if callable(target) and not any(callable(getattr(target, name, None)) for name in names):
        return target(*args, **kwargs)
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            first: TypeError | None = None
            reductions = (
                (),
                ("effective_date",),
                ("effective_at",),
                ("effective_at", "effective_date"),
                ("effective_at", "effective_date", "data_cutoff"),
            )
            for removed in reductions:
                reduced = {key: value for key, value in kwargs.items() if key not in removed}
                try:
                    return method(*args, **reduced)
                except TypeError as exc:
                    first = first or exc
            if first is not None:
                raise first
    raise DomainValidationError(f"provider {type(target).__name__} has no supported method")


def _fact_provenance(fact: object | None) -> dict[str, Any]:
    if fact is None:
        return {}
    names = (
        "fact_id", "fact_version", "logical_fact_key", "instrument_id", "source",
        "source_code", "trading_code", "mapping_source", "asset_class", "exchange",
        "currency", "calendar_id", "valid_from", "valid_to", "known_at", "observed_at",
        "source_revision", "quality_status", "fixture_only", "content_hash",
    )
    return {name: canonical_payload(getattr(fact, name, None)) for name in names if hasattr(fact, name)}


def _provenance_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return canonical_payload(value)
    return {name: canonical_payload(getattr(value, name)) for name in ("calendar_id", "session_date", "is_open", "timezone", "definition_version", "source", "fact_id", "known_at", "content_hash") if hasattr(value, name)}


def _resolution_provenance(resolution: RulePackageResolution, facts: Sequence[RuleFactCandidate], persisted_exception: object | None) -> dict[str, Any]:
    by_ref = {fact.fact_reference: fact for fact in facts}
    selected = []
    for summary in resolution.selected_facts:
        candidate = by_ref.get(summary.fact_reference)
        selected.append({
            "fact_reference": canonical_payload(summary.fact_reference),
            "source": summary.source,
            "source_revision": summary.source_revision,
            "valid_from": canonical_payload(summary.valid_from),
            "valid_to": canonical_payload(summary.valid_to),
            "known_at": canonical_payload(summary.known_at),
            "observed_at": canonical_payload(summary.observed_at),
            "quality_status": canonical_payload(summary.quality_status),
            "fixture_only": summary.fixture_only,
            "content_hash": getattr(candidate, "content_hash", None),
            "exception_set_reference": canonical_payload(summary.exception_set_reference),
        })
    # Fact repositories may return rows in different physical orders.  The
    # parse order is represented by the exception marker, while equal-phase
    # provenance is canonicalized before it contributes to the qualification
    # hash.
    selected.sort(
        key=lambda item: (
            item["exception_set_reference"] is not None,
            stable_hash(canonical_payload(item)),
        )
    )
    result = {
        "resolution_hash": resolution.semantic_hash,
        "package_reference": canonical_payload(resolution.package_reference),
        "parser_revision": resolution.parser_revision,
        "exception_reference": canonical_payload(resolution.exception_reference),
        "exception_set_reference": canonical_payload(resolution.exception_set_reference),
        "selected_facts": selected,
        "normalized_values": canonical_payload(resolution.normalized_values),
        "capability_declarations": canonical_payload(resolution.capability_declarations),
    }
    if persisted_exception is not None and hasattr(persisted_exception, "content_hash"):
        result["exception_set_hash"] = persisted_exception.content_hash
    elif resolution.exception_set_reference is not None:
        result["exception_set_hash"] = exception_set_content_hash(persisted_exception) if hasattr(persisted_exception, "entries") else None
    return result


def _intersection_interval(identity: InstrumentIdentityFact, resolution: RulePackageResolution) -> tuple[datetime, datetime | None]:
    starts = [identity.valid_from]
    ends: list[date] = []
    if identity.valid_to is not None:
        ends.append(identity.valid_to)
    for summary in resolution.selected_facts:
        if summary.valid_from is not None:
            starts.append(summary.valid_from)
        if summary.valid_to is not None:
            ends.append(summary.valid_to)
    start = max(starts)
    end = min(ends) if ends else None
    start_at = datetime.combine(start, time.min, tzinfo=UTC)
    end_at = datetime.combine(end, time.min, tzinfo=UTC) if end is not None else None
    return start_at, end_at


def _calendar_values(calendar: object, expected_template: object) -> tuple[object | None, VersionedReference]:
    template = expected_template
    hours = calendar
    if isinstance(calendar, Mapping):
        hours = calendar.get("trading_hours", calendar.get("sessions", calendar))
        supplied = calendar.get("trading_session_template")
        if supplied is not None:
            template = supplied
    else:
        for name in ("trading_hours", "sessions", "resolved_sessions", "sessions_override", "default_sessions"):
            if hasattr(calendar, name):
                hours = getattr(calendar, name)
                break
        supplied = getattr(calendar, "trading_session_template", None)
        if supplied is not None:
            template = supplied
    if isinstance(template, Mapping):
        template = VersionedReference(key=template.get("key"), version=template.get("version"))
    if not isinstance(template, VersionedReference):
        raise DomainValidationError("trading_session_template must resolve to a VersionedReference")
    if hours is calendar:
        hours = _provenance_value(calendar)
    if isinstance(hours, (list, tuple)):
        normalized_hours = []
        for window in hours:
            semantic = getattr(window, "semantic_payload", None)
            normalized_hours.append(semantic() if callable(semantic) else window)
        hours = normalized_hours
    return hours, template


def _freeze_json_like(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({str(k): freeze(v) for k, v in item.items()})
        if isinstance(item, (list, tuple)):
            return tuple(freeze(v) for v in item)
        if isinstance(item, (set, frozenset)):
            return frozenset(freeze(v) for v in item)
        return item
    return freeze(value)


def _capabilities_payload(value: InstrumentCapabilities) -> dict[str, Any]:
    return {
        "position_sides": sorted(value.position_sides),
        "order_types": sorted(value.order_types),
        "margin_supported": value.margin_supported,
        "corporate_action_requirement": value.corporate_action_requirement.value,
    }


def _qualification_hash(result: InstrumentSpecQualification) -> str:
    issue_payloads = [
        {
            "code": issue.code,
            "field": issue.field,
            "instrument_id": issue.instrument_id,
            "details": issue.details,
        }
        for issue in result.issues
    ]
    issue_payloads.sort(key=lambda item: stable_hash(canonical_payload(item)))
    payload = {
        "kind": "instrument_spec_qualification",
        "instrument_id": result.instrument_id,
        "status": result.status,
        "spec": None if result.spec is None else {
            "instrument_id": result.spec.instrument_id,
            "display": {
                "instrument_id": result.spec.display.instrument_id,
                "trading_code": result.spec.display.trading_code,
                "name": result.spec.display.name,
                "display_name": result.spec.display.display_name,
            },
            "asset_class": result.spec.asset_class,
            "exchange": result.spec.exchange,
            "currency": result.spec.currency,
            "calendar_id": result.spec.calendar_id,
            "price_precision": result.spec.price_precision,
            "quantity_precision": result.spec.quantity_precision,
            "price_tick": result.spec.price_tick,
            "lot_size": result.spec.lot_size,
            "minimum_order_quantity": result.spec.minimum_order_quantity,
            "contract_multiplier": result.spec.contract_multiplier,
            "trading_session_template": result.spec.trading_session_template,
            "trading_hours": result.spec.trading_hours,
            "settlement_rule_class": result.spec.settlement_rule_class,
            "sellable_rule": result.spec.sellable_rule,
            "fee_categories": result.spec.fee_categories,
            "trading_status_policy": result.spec.trading_status_policy,
            "order_types": result.spec.order_types,
            "price_limit_rule": result.spec.price_limit_rule,
            "cash_availability_rule": result.spec.cash_availability_rule,
            "position_availability_rule": result.spec.position_availability_rule,
            "capabilities": _capabilities_payload(result.spec.capabilities),
            "rule_package_reference": result.spec.rule_package_reference,
            "rule_exception_reference": result.spec.rule_exception_reference,
            "valid_from": result.spec.valid_from,
            "valid_to": result.spec.valid_to,
        },
        "calendar_id": result.calendar_id,
        "provenance": result.provenance,
        "issues": issue_payloads,
    }
    return stable_hash(canonical_payload(payload))


__all__ = [
    "DEFAULT_MAPPING_SOURCE",
    "DEFAULT_RULE_PACKAGE_REFERENCE",
    "DefaultInstrumentSpecProvider",
    "InstrumentEligibility",
    "InstrumentQualificationIssue",
    "InstrumentSpecOrchestrator",
    "InstrumentSpecProvider",
    "InstrumentSpecQualification",
    "InstrumentSpecResolver",
    "QualificationIssue",
    "SingleInstrumentQualificationProvider",
    "SingleInstrumentQualification",
    "resolve_instrument_qualification",
]

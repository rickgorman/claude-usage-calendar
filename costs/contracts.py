"""Frozen, implementation-neutral contracts for cost estimation.

This module deliberately contains no rate lookup or monetary calculation.  Source
adapters, the pricing engine, and serializers depend on these value objects without
depending on one another.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import Enum
from types import MappingProxyType
from typing import Protocol, TypeAlias

PRICING_PACK_SCHEMA_VERSION = 1
COST_ESTIMATES_SCHEMA_VERSION = 1
COSTS_ENABLED_BY_DEFAULT = False
CURRENCY = "USD"
VALUATION_BASIS = "published_rate_equivalent_snapshot"
USD_JSON_QUANTUM = Decimal("0.000001")
USD_DISPLAY_QUANTUM = Decimal("0.01")
USD_ROUNDING = ROUND_HALF_UP
TOKENS_PER_MILLION = 1_000_000
MAX_IDENTIFIER_LENGTH = 256
FORBIDDEN_MAPPING_KEYS = frozenset({"__proto__", "prototype", "constructor"})
_MONEY_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{6}$")
_PRICE_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,12})?$")


class Provider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    XAI = "xai"
    CURSOR = "cursor"


class Meter(str, Enum):
    INPUT_UNCACHED = "input.uncached"
    INPUT_CACHE_READ = "input.cache_read"
    INPUT_CACHE_WRITE_5M = "input.cache_write_5m"
    INPUT_CACHE_WRITE_1H = "input.cache_write_1h"
    OUTPUT = "output"


class Unit(str, Enum):
    TOKENS = "tokens"
    REQUESTS = "requests"
    COUNT = "count"


class RateUnit(str, Enum):
    MILLION_TOKENS = "million_tokens"


class CostStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNPRICED = "unpriced"
    DISABLED = "disabled"


class MeasurementQuality(str, Enum):
    PROVIDER_REPORTED = "provider_reported"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    MIXED = "mixed"


class DiagnosticReason(str, Enum):
    """Bounded machine-readable reasons shared by all cost workstreams."""

    SEMANTICS_INCONSISTENT = "SEMANTICS_INCONSISTENT"
    UNSUPPORTED_SEMANTICS_VERSION = "UNSUPPORTED_SEMANTICS_VERSION"
    MALFORMED_SOURCE_RECORD = "MALFORMED_SOURCE_RECORD"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    MISSING_PROVIDER = "MISSING_PROVIDER"
    UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"
    MISSING_MODEL = "MISSING_MODEL"
    MISSING_CHANNEL = "MISSING_CHANNEL"
    MISSING_VARIANT = "MISSING_VARIANT"
    MISSING_SERVICE_TIER = "MISSING_SERVICE_TIER"
    MISSING_CONTEXT_BAND = "MISSING_CONTEXT_BAND"
    MISSING_REQUEST_TOTAL = "MISSING_REQUEST_TOTAL"
    UNKNOWN_CACHE_TTL = "UNKNOWN_CACHE_TTL"
    UNKNOWN_ROUTED_MODEL = "UNKNOWN_ROUTED_MODEL"
    UNSUPPORTED_CHARGE = "UNSUPPORTED_CHARGE"
    UNSUPPORTED_METER = "UNSUPPORTED_METER"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    BOUNDED_COMPACTION = "BOUNDED_COMPACTION"
    RATE_NOT_FOUND = "RATE_NOT_FOUND"
    RATE_AMBIGUOUS = "RATE_AMBIGUOUS"


JsonScalar: TypeAlias = None | bool | int | float | str
FrozenJson: TypeAlias = (
    JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]
)
JsonObject: TypeAlias = Mapping[str, FrozenJson]


def freeze_json(value: object) -> FrozenJson:
    """Return a deeply immutable JSON-shaped value without losing source fields."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            key = str(key)
            _require_safe_mapping_key(key, "source metadata key")
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"source metadata is not JSON-shaped: {type(value).__name__}")


def _require_aware_datetime(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")


def _require_nonempty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_safe_mapping_key(value: str, field_name: str) -> None:
    _require_nonempty(value, field_name)
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field_name} exceeds {MAX_IDENTIFIER_LENGTH} characters")
    if value in FORBIDDEN_MAPPING_KEYS:
        raise ValueError(f"{field_name} is a forbidden mapping key")


def _require_optional_identifier(value: str | None, field_name: str) -> None:
    if value is not None:
        _require_safe_mapping_key(value, field_name)


def _require_record_quality(value: MeasurementQuality) -> None:
    if not isinstance(value, MeasurementQuality):
        raise TypeError("measurement_quality must be a MeasurementQuality")
    if value is MeasurementQuality.MIXED:
        raise ValueError("record-level measurement quality is never mixed")


def _require_quantity(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("quantity must be an integer")
    if value < 0:
        raise ValueError("quantity must be nonnegative")


def _freeze_assumptions(values: tuple[str, ...]) -> tuple[str, ...]:
    frozen = tuple(values)
    if any(not isinstance(value, str) or not value for value in frozen):
        raise ValueError("assumptions must contain only non-empty strings")
    return frozen


@dataclass(frozen=True, slots=True)
class RawUsageRecord:
    """Lossless record passed to exactly one source-specific semantic adapter."""

    source_record_id: str
    agent: str
    provider: str | None
    model: str | None
    channel: str | None
    variant: str | None
    service_tier: str | None
    context_band: str | None
    occurred_at: datetime
    measurement_quality: MeasurementQuality
    semantics_version: str
    assumptions: tuple[str, ...] = ()
    source: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty(self.source_record_id, "source_record_id")
        _require_safe_mapping_key(self.agent, "agent")
        _require_nonempty(self.semantics_version, "semantics_version")
        _require_aware_datetime(self.occurred_at)
        _require_optional_identifier(self.provider, "provider")
        for name in ("model", "channel", "variant", "service_tier", "context_band"):
            _require_optional_identifier(getattr(self, name), name)
        _require_record_quality(self.measurement_quality)
        object.__setattr__(self, "assumptions", _freeze_assumptions(self.assumptions))
        frozen_source = freeze_json(self.source)
        if not isinstance(frozen_source, Mapping):
            raise TypeError("source must be a mapping")
        object.__setattr__(self, "source", frozen_source)


@dataclass(frozen=True, slots=True)
class BillableQuantity:
    """A source-semantics-aware quantity; it is not itself a priced amount."""

    source_record_id: str
    agent: str
    provider: Provider
    model: str
    channel: str
    variant: str
    service_tier: str
    context_band: str
    occurred_at: datetime
    meter: Meter
    quantity: int
    unit: Unit
    measurement_quality: MeasurementQuality
    semantics_version: str
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty(self.source_record_id, "source_record_id")
        _require_safe_mapping_key(self.agent, "agent")
        _require_nonempty(self.semantics_version, "semantics_version")
        _require_aware_datetime(self.occurred_at)
        if not isinstance(self.provider, Provider):
            raise TypeError("billable provider must be a known Provider")
        for name in ("model", "channel", "variant", "service_tier", "context_band"):
            _require_safe_mapping_key(getattr(self, name), name)
        if not isinstance(self.meter, Meter):
            raise TypeError("meter must be a priced token Meter")
        if self.unit is not Unit.TOKENS:
            raise ValueError("billable v1 meters use token quantities")
        _require_quantity(self.quantity)
        _require_record_quality(self.measurement_quality)
        object.__setattr__(self, "assumptions", _freeze_assumptions(self.assumptions))


@dataclass(frozen=True, slots=True)
class UnpricedQuantity:
    """Visible source usage that cannot honestly form a complete RateKey."""

    source_record_id: str
    agent: str
    provider: str | None
    model: str | None
    channel: str | None
    variant: str | None
    service_tier: str | None
    context_band: str | None
    occurred_at: datetime
    meter: Meter | None
    quantity: int
    unit: Unit
    measurement_quality: MeasurementQuality
    semantics_version: str
    reason: DiagnosticReason
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty(self.source_record_id, "source_record_id")
        _require_safe_mapping_key(self.agent, "agent")
        _require_nonempty(self.semantics_version, "semantics_version")
        _require_aware_datetime(self.occurred_at)
        _require_optional_identifier(self.provider, "provider")
        for name in ("model", "channel", "variant", "service_tier", "context_band"):
            _require_optional_identifier(getattr(self, name), name)
        if self.meter is not None and not isinstance(self.meter, Meter):
            raise TypeError("meter must be a Meter or None")
        if not isinstance(self.unit, Unit):
            raise TypeError("unit must be a Unit")
        _require_quantity(self.quantity)
        _require_record_quality(self.measurement_quality)
        if not isinstance(self.reason, DiagnosticReason):
            raise TypeError("reason must be a bounded DiagnosticReason")
        object.__setattr__(self, "assumptions", _freeze_assumptions(self.assumptions))


@dataclass(frozen=True, slots=True)
class RateKey:
    """The complete exact-match lookup key.  No field permits wildcard matching."""

    provider: Provider
    model: str
    channel: str
    variant: str
    service_tier: str
    context_band: str
    meter: Meter
    valuation_date: date
    measurement_quality: MeasurementQuality

    def __post_init__(self) -> None:
        if not isinstance(self.provider, Provider):
            raise TypeError("rate-key provider must be a known Provider")
        for name in ("model", "channel", "variant", "service_tier", "context_band"):
            _require_safe_mapping_key(getattr(self, name), name)
        if not isinstance(self.meter, Meter):
            raise TypeError("rate-key meter must be a Meter")
        if not isinstance(self.valuation_date, date):
            raise TypeError("valuation_date must be a date")
        _require_record_quality(self.measurement_quality)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    reason: DiagnosticReason
    source_record_id: str
    meter: Meter | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.source_record_id, "source_record_id")


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    quantities: tuple[BillableQuantity, ...] = ()
    unpriced_quantities: tuple[UnpricedQuantity, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantities", tuple(self.quantities))
        object.__setattr__(self, "unpriced_quantities", tuple(self.unpriced_quantities))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


class UsageNormalizer(Protocol):
    def __call__(self, record: RawUsageRecord) -> NormalizationResult:
        """Convert one raw record without aggregating or selecting a rate."""


class ContractViolation(ValueError):
    """Raised when a schema-shaped cost payload violates application invariants."""


def _money(value: object, name: str) -> Decimal:
    if not isinstance(value, str) or not _MONEY_PATTERN.fullmatch(value):
        raise ContractViolation(f"{name} must be a six-decimal nonnegative string")
    return Decimal(value)


def _exact_component_cost(component: Mapping[str, object], name: str) -> Decimal:
    quantity = component.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise ContractViolation(f"{name}.quantity must be a nonnegative integer")
    price = component.get("price")
    if (
        not isinstance(price, str)
        or len(price) > 64
        or not _PRICE_PATTERN.fullmatch(price)
    ):
        raise ContractViolation(f"{name}.price must be a bounded decimal string")
    exact = Decimal(quantity) * Decimal(price) / Decimal(TOKENS_PER_MILLION)
    displayed = exact.quantize(USD_JSON_QUANTUM, rounding=USD_ROUNDING)
    if _money(component.get("cost_usd"), f"{name}.cost_usd") != displayed:
        raise ContractViolation(
            f"{name}.cost_usd does not equal its rounded exact cost"
        )
    return exact


def _cost_precision(components: Sequence[Mapping[str, object]]) -> int:
    maximum_digits = 1
    for component in components:
        quantity = component.get("quantity")
        price = component.get("price")
        quantity_digits = len(str(quantity)) if isinstance(quantity, int) else 1
        price_digits = len(price.replace(".", "")) if isinstance(price, str) else 1
        maximum_digits = max(maximum_digits, quantity_digits + price_digits)
    carry_digits = len(str(max(1, len(components))))
    return max(50, maximum_digits + carry_digits + 12)


def _validate_component_date(component: Mapping[str, object], name: str) -> None:
    occurred_on = component.get("occurred_on")
    if not isinstance(occurred_on, str):
        raise ContractViolation(f"{name}.occurred_on must be an ISO date")
    try:
        parsed = date.fromisoformat(occurred_on)
    except ValueError as error:
        raise ContractViolation(f"{name}.occurred_on must be an ISO date") from error
    if parsed.isoformat() != occurred_on:
        raise ContractViolation(f"{name}.occurred_on must be a canonical ISO date")
    occurred_hour = component.get("occurred_hour")
    if occurred_hour is not None and (
        isinstance(occurred_hour, bool)
        or not isinstance(occurred_hour, int)
        or not 0 <= occurred_hour <= 23
    ):
        raise ContractViolation(
            f"{name}.occurred_hour must be an integer from 0 through 23"
        )


def _sequence(value: object, name: str) -> Sequence[Mapping[str, object]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise ContractViolation(f"{name} must be an array of objects")
    return value


def _expected_quality(items: Sequence[Mapping[str, object]]) -> str | None:
    qualities = {item.get("measurement_quality") for item in items}
    if not qualities:
        return None
    if not qualities <= {
        item.value
        for item in MeasurementQuality
        if item is not MeasurementQuality.MIXED
    }:
        raise ContractViolation("component measurement quality is not bounded")
    return (
        next(iter(qualities)) if len(qualities) == 1 else MeasurementQuality.MIXED.value
    )


def _validate_rollup(
    name: str,
    rollup: Mapping[str, object],
    priced: Sequence[Mapping[str, object]],
    unpriced: Sequence[Mapping[str, object]],
) -> None:
    status = rollup.get("status")
    expected_statuses = (
        {CostStatus.PARTIAL.value}
        if priced and unpriced
        else {CostStatus.COMPLETE.value}
        if priced
        else {CostStatus.UNPRICED.value}
        if unpriced
        else {CostStatus.COMPLETE.value, CostStatus.DISABLED.value}
    )
    if status not in expected_statuses:
        raise ContractViolation(f"{name} status does not match priced/unpriced usage")

    subtotal = _money(rollup.get("priced_subtotal_usd"), f"{name}.priced_subtotal_usd")
    with localcontext() as decimal_context:
        decimal_context.prec = _cost_precision(priced)
        exact_component_sum = sum(
            (
                _exact_component_cost(item, f"{name}.component[{index}]")
                for index, item in enumerate(priced)
            ),
            Decimal(0),
        )
        rounded_exact_subtotal = exact_component_sum.quantize(
            USD_JSON_QUANTUM, rounding=USD_ROUNDING
        )
    if subtotal != rounded_exact_subtotal:
        raise ContractViolation(
            f"{name} subtotal does not equal its once-rounded exact component sum"
        )

    total = rollup.get("total_usd")
    if status == CostStatus.COMPLETE.value:
        if _money(total, f"{name}.total_usd") != subtotal:
            raise ContractViolation(f"{name} complete total must equal subtotal")
    elif total is not None:
        raise ContractViolation(f"{name} non-complete total must be null")

    expected_quality = _expected_quality([*priced, *unpriced])
    if expected_quality is not None and rollup.get("quality") != expected_quality:
        raise ContractViolation(f"{name} quality does not match record-level qualities")


def _validate_scopes(
    name: str,
    value: Mapping[str, object],
    priced: Sequence[Mapping[str, object]],
    unpriced: Sequence[Mapping[str, object]],
) -> None:
    scope_names = ("hourly_usage", "daily_usage", "monthly_usage", "yearly_usage")
    present = [scope for scope in scope_names if scope in value]
    if len(present) != len(scope_names):
        raise ContractViolation("cost rollup scopes must be emitted together")

    def validate_mapping(scope_name, key_for):
        mapping = value.get(scope_name)
        if not isinstance(mapping, Mapping):
            raise ContractViolation(f"{name}.{scope_name} must be an object")
        expected = {key_for(item) for item in [*priced, *unpriced]}
        if set(mapping) != expected:
            raise ContractViolation(
                f"{name}.{scope_name} does not cover components exactly"
            )
        for key, rollup in mapping.items():
            if not isinstance(rollup, Mapping):
                raise ContractViolation(f"{name}.{scope_name}.{key} must be an object")
            _validate_rollup(
                f"{name}.{scope_name}.{key}",
                rollup,
                [item for item in priced if key_for(item) == key],
                [item for item in unpriced if key_for(item) == key],
            )

    hourly = value.get("hourly_usage")
    if not isinstance(hourly, Mapping):
        raise ContractViolation(f"{name}.hourly_usage must be an object")
    expected_days = {item.get("occurred_on") for item in [*priced, *unpriced]}
    if set(hourly) != expected_days:
        raise ContractViolation(f"{name}.hourly_usage does not cover dates exactly")
    for occurred_on, raw_hours in hourly.items():
        if not isinstance(raw_hours, Mapping):
            raise ContractViolation(
                f"{name}.hourly_usage.{occurred_on} must be an object"
            )
        day_priced = [item for item in priced if item.get("occurred_on") == occurred_on]
        day_unpriced = [
            item for item in unpriced if item.get("occurred_on") == occurred_on
        ]
        expected_hours = {
            str(item.get("occurred_hour")) for item in [*day_priced, *day_unpriced]
        }
        if set(raw_hours) != expected_hours:
            raise ContractViolation(
                f"{name}.hourly_usage.{occurred_on} does not cover hours exactly"
            )
        for hour, rollup in raw_hours.items():
            if not isinstance(rollup, Mapping):
                raise ContractViolation("hourly rollup must be an object")
            _validate_rollup(
                f"{name}.hourly_usage.{occurred_on}.{hour}",
                rollup,
                [item for item in day_priced if str(item.get("occurred_hour")) == hour],
                [item for item in day_unpriced if str(item.get("occurred_hour")) == hour],
            )

    validate_mapping("daily_usage", lambda item: item.get("occurred_on"))
    validate_mapping(
        "monthly_usage", lambda item: str(item.get("occurred_on"))[:7]
    )
    validate_mapping("yearly_usage", lambda item: str(item.get("occurred_on"))[:4])


def validate_cost_estimates(payload: Mapping[str, object]) -> None:
    """Enforce cross-field invariants after schema validation and before serialization.

    This validates already-calculated backend values; it never selects rates or
    calculates a cost estimate.
    """

    if not isinstance(payload, Mapping):
        raise ContractViolation("cost estimates must be an object")
    priced = _sequence(payload.get("components"), "components")
    unpriced = _sequence(payload.get("unpriced_components"), "unpriced_components")
    diagnostics = _sequence(payload.get("diagnostics"), "diagnostics")
    for index, item in enumerate([*priced, *unpriced]):
        _validate_component_date(item, f"component[{index}]")
    if len(diagnostics) > 10_000:
        raise ContractViolation("diagnostics exceeds the bounded maximum")
    bounded_reasons = {reason.value for reason in DiagnosticReason}
    for item in [*unpriced, *diagnostics]:
        if item.get("reason") not in bounded_reasons:
            raise ContractViolation("diagnostic reason is not bounded")

    _validate_rollup("cost_estimates", payload, priced, unpriced)
    _validate_scopes("cost_estimates", payload, priced, unpriced)

    agents = payload.get("agents")
    if not isinstance(agents, Mapping):
        raise ContractViolation("agents must be an object")
    scoped_agents = {item.get("agent") for item in [*priced, *unpriced]}
    if set(agents) != scoped_agents:
        raise ContractViolation("agent rollups do not cover component agents exactly")
    for agent, rollup in agents.items():
        _require_safe_mapping_key(agent, "agent rollup key")
        if not isinstance(rollup, Mapping):
            raise ContractViolation("agent rollup must be an object")
        agent_priced = [item for item in priced if item.get("agent") == agent]
        agent_unpriced = [item for item in unpriced if item.get("agent") == agent]
        _validate_rollup(f"agents.{agent}", rollup, agent_priced, agent_unpriced)
        _validate_scopes(f"agents.{agent}", rollup, agent_priced, agent_unpriced)

        daily = rollup.get("daily_usage")
        if not isinstance(daily, Mapping):
            raise ContractViolation("daily_usage must be an object")
        dates = {item.get("occurred_on") for item in [*agent_priced, *agent_unpriced]}
        if set(daily) != dates:
            raise ContractViolation(
                "daily rollups do not cover component dates exactly"
            )
        for occurred_on, day_rollup in daily.items():
            if not isinstance(day_rollup, Mapping):
                raise ContractViolation("daily rollup must be an object")
            day_priced = [
                item for item in agent_priced if item.get("occurred_on") == occurred_on
            ]
            day_unpriced = [
                item
                for item in agent_unpriced
                if item.get("occurred_on") == occurred_on
            ]
            _validate_rollup(
                f"agents.{agent}.daily_usage.{occurred_on}",
                day_rollup,
                day_priced,
                day_unpriced,
            )

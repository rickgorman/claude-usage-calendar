"""Versioned JSON serialization for already-priced cost results.

This boundary deliberately does not select rates, calculate money, or interpret
legacy usage data.  It only turns the immutable engine result into the strict v1
wire representation and checks the resulting application invariants.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import cast

from .contracts import (
    COST_ESTIMATES_SCHEMA_VERSION,
    CURRENCY,
    USD_JSON_QUANTUM,
    VALUATION_BASIS,
    CostStatus,
    Diagnostic,
    FrozenJson,
    JsonObject,
    UnpricedQuantity,
    validate_cost_estimates,
)
from .engine import CostResult, CostRollup, PricedComponent
from .pricing import PricingPackKind

_FORBIDDEN_KEYS = frozenset({"__proto__", "prototype", "constructor"})
_IDENTIFIER_MAX_LENGTH = 256
_LABEL_MAX_LENGTH = 2048
_MONEY_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{6}$")
_PRICE_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,12})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STATUS_VALUES = frozenset({"complete", "partial", "unpriced", "disabled"})
_QUALITY_VALUES = frozenset({"provider_reported", "derived", "estimated", "mixed"})
_RECORD_QUALITY_VALUES = _QUALITY_VALUES - {"mixed"}
_METER_VALUES = frozenset(
    {
        "input.uncached",
        "input.cache_read",
        "input.cache_write_5m",
        "input.cache_write_1h",
        "output",
    }
)
_REASON_VALUES = frozenset(
    {
        "SEMANTICS_INCONSISTENT",
        "UNSUPPORTED_SEMANTICS_VERSION",
        "MALFORMED_SOURCE_RECORD",
        "INVALID_QUANTITY",
        "MISSING_PROVIDER",
        "UNKNOWN_PROVIDER",
        "MISSING_MODEL",
        "MISSING_CHANNEL",
        "MISSING_VARIANT",
        "MISSING_SERVICE_TIER",
        "MISSING_CONTEXT_BAND",
        "MISSING_REQUEST_TOTAL",
        "UNKNOWN_CACHE_TTL",
        "UNKNOWN_ROUTED_MODEL",
        "UNSUPPORTED_CHARGE",
        "UNSUPPORTED_METER",
        "ADAPTER_UNAVAILABLE",
        "BOUNDED_COMPACTION",
        "RATE_NOT_FOUND",
        "RATE_AMBIGUOUS",
    }
)


def _enum_value(value: object) -> str:
    """Return an enum's JSON value without accepting arbitrary strings."""

    raw_value = getattr(value, "value", None)
    if not isinstance(raw_value, str):
        raise TypeError(f"expected a string-valued enum, got {type(value).__name__}")
    return raw_value


def _fixed_usd(value: Decimal | None) -> str | None:
    """Emit an engine-produced USD amount without rounding it again."""

    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError("USD amounts must be Decimal instances")
    if not value.is_finite() or value < 0:
        raise ValueError("USD amounts must be finite and nonnegative")
    if value.as_tuple().exponent != USD_JSON_QUANTUM.as_tuple().exponent:
        raise ValueError("USD amounts must already be quantized to six decimal places")
    rendered = format(value, "f")
    if len(rendered) > 64 or not _MONEY_PATTERN.fullmatch(rendered):
        raise ValueError("USD amounts must be bounded six-decimal strings")
    return rendered


def _price(value: Decimal) -> str:
    """Emit a Decimal price in ordinary notation, never scientific notation."""

    if not isinstance(value, Decimal):
        raise TypeError("prices must be Decimal instances")
    if not value.is_finite() or value < 0:
        raise ValueError("prices must be finite and nonnegative")
    rendered = format(value, "f")
    fraction = rendered.partition(".")[2]
    if (
        len(rendered) > 64
        or len(fraction) > 12
        or not _PRICE_PATTERN.fullmatch(rendered)
    ):
        raise ValueError("prices support at most twelve fractional decimal places")
    return rendered


def _rollup(rollup: CostRollup) -> dict[str, object]:
    return {
        "status": _enum_value(rollup.status),
        "quality": _enum_value(rollup.quality),
        "total_usd": _fixed_usd(rollup.total_usd),
        "priced_subtotal_usd": _fixed_usd(rollup.priced_subtotal_usd),
    }


def _priced_component(component: PricedComponent) -> dict[str, object]:
    key = component.rate_key
    return {
        "source_record_ids": list(component.source_record_ids),
        "occurred_on": component.occurred_on.isoformat(),
        "occurred_hour": component.occurred_hour,
        "agent": component.agent,
        "provider": _enum_value(key.provider),
        "model": key.model,
        "channel": key.channel,
        "variant": key.variant,
        "service_tier": key.service_tier,
        "context_band": key.context_band,
        "meter": _enum_value(key.meter),
        "measurement_quality": _enum_value(key.measurement_quality),
        "quantity": component.quantity,
        "unit": _enum_value(component.unit),
        "assumptions": list(component.assumptions),
        "rate_unit": "million_tokens",
        "price": _price(component.price),
        "cost_usd": _fixed_usd(component.cost_usd),
    }


def _unpriced_component(quantity: UnpricedQuantity) -> dict[str, object]:
    return {
        "source_record_ids": [quantity.source_record_id],
        "occurred_on": quantity.occurred_at.date().isoformat(),
        "occurred_hour": quantity.occurred_at.hour,
        "agent": quantity.agent,
        "provider": quantity.provider,
        "model": quantity.model,
        "channel": quantity.channel,
        "variant": quantity.variant,
        "service_tier": quantity.service_tier,
        "context_band": quantity.context_band,
        "meter": _enum_value(quantity.meter) if quantity.meter is not None else None,
        "measurement_quality": _enum_value(quantity.measurement_quality),
        "quantity": quantity.quantity,
        "unit": _enum_value(quantity.unit),
        "assumptions": list(quantity.assumptions),
        "reason": _enum_value(quantity.reason),
    }


def _diagnostic(diagnostic: Diagnostic) -> dict[str, object]:
    serialized: dict[str, object] = {
        "reason": _enum_value(diagnostic.reason),
        "source_record_id": diagnostic.source_record_id,
    }
    if diagnostic.meter is not None:
        serialized["meter"] = _enum_value(diagnostic.meter)
    if diagnostic.detail is not None:
        serialized["detail"] = diagnostic.detail
    return serialized


def _disclaimer(valuation_as_of: date) -> str:
    return (
        "Published-rate-equivalent estimate, not an invoice. "
        f"Snapshot-valued as of {valuation_as_of.isoformat()}."
    )


def _require_identifier(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _IDENTIFIER_MAX_LENGTH
        or value in _FORBIDDEN_KEYS
    ):
        raise ValueError(f"{name} must be a safe bounded identifier")


def _require_optional_identifier(value: object, name: str) -> None:
    if value is not None:
        _require_identifier(value, name)


def _require_label(value: object, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > _LABEL_MAX_LENGTH:
        raise ValueError(f"{name} must be a bounded non-empty label")


def _require_date(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date")


def _require_one_of(value: object, allowed: frozenset[str], name: str) -> None:
    if value not in allowed:
        raise ValueError(f"{name} is not a supported value")


def _require_money(value: object, name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not _MONEY_PATTERN.fullmatch(value)
    ):
        raise ValueError(f"{name} must be a six-decimal nonnegative string")


def _require_labels(value: object, name: str, maximum: int | None = None) -> None:
    if not isinstance(value, list) or (maximum is not None and len(value) > maximum):
        raise ValueError(f"{name} must be a bounded label array")
    for index, item in enumerate(value):
        _require_label(item, f"{name}[{index}]")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicate labels")


def _require_rollup(value: object, name: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    if set(value) != {"status", "quality", "total_usd", "priced_subtotal_usd"}:
        raise ValueError(f"{name} has an invalid shape")
    _require_one_of(value["status"], _STATUS_VALUES, f"{name}.status")
    _require_one_of(value["quality"], _QUALITY_VALUES, f"{name}.quality")
    _require_money(value["total_usd"], f"{name}.total_usd", nullable=True)
    _require_money(value["priced_subtotal_usd"], f"{name}.priced_subtotal_usd")


def _require_dimensions(value: object, name: str, *, priced: bool) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    required = {
        "source_record_ids",
        "occurred_on",
        "occurred_hour",
        "agent",
        "provider",
        "model",
        "channel",
        "variant",
        "service_tier",
        "context_band",
        "meter",
        "measurement_quality",
        "quantity",
        "unit",
        "assumptions",
    }
    expected = required | ({"rate_unit", "price", "cost_usd"} if priced else {"reason"})
    if set(value) != expected:
        raise ValueError(f"{name} has an invalid shape")
    source_ids = value["source_record_ids"]
    if not isinstance(source_ids, list) or not source_ids:
        raise ValueError(f"{name}.source_record_ids must be a non-empty array")
    for index, source_id in enumerate(source_ids):
        _require_identifier(source_id, f"{name}.source_record_ids[{index}]")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError(f"{name}.source_record_ids must be unique")
    _require_date(value["occurred_on"], f"{name}.occurred_on")
    occurred_hour = value["occurred_hour"]
    if (
        isinstance(occurred_hour, bool)
        or not isinstance(occurred_hour, int)
        or not 0 <= occurred_hour <= 23
    ):
        raise ValueError(f"{name}.occurred_hour must be an integer from 0 through 23")
    _require_identifier(value["agent"], f"{name}.agent")
    _require_optional_identifier(value["provider"], f"{name}.provider")
    for dimension in ("model", "channel", "variant", "service_tier", "context_band"):
        _require_optional_identifier(value[dimension], f"{name}.{dimension}")
    meter = value["meter"]
    if meter is not None:
        _require_one_of(meter, _METER_VALUES, f"{name}.meter")
    _require_one_of(
        value["measurement_quality"],
        _RECORD_QUALITY_VALUES,
        f"{name}.measurement_quality",
    )
    if (
        isinstance(value["quantity"], bool)
        or not isinstance(value["quantity"], int)
        or value["quantity"] < 0
    ):
        raise ValueError(f"{name}.quantity must be a nonnegative integer")
    _require_one_of(
        value["unit"], frozenset({"tokens", "requests", "count"}), f"{name}.unit"
    )
    _require_labels(value["assumptions"], f"{name}.assumptions")
    if priced:
        if (
            value["provider"] not in {"anthropic", "openai", "xai", "cursor"}
            or not all(
                value[dimension] is not None
                for dimension in (
                    "model",
                    "channel",
                    "variant",
                    "service_tier",
                    "context_band",
                    "meter",
                )
            )
            or value["unit"] != "tokens"
            or value["rate_unit"] != "million_tokens"
        ):
            raise ValueError(f"{name} does not have complete priced dimensions")
        price = value["price"]
        if (
            not isinstance(price, str)
            or len(price) > 64
            or not _PRICE_PATTERN.fullmatch(price)
        ):
            raise ValueError(f"{name}.price must be a bounded decimal string")
        _require_money(value["cost_usd"], f"{name}.cost_usd")
    else:
        _require_one_of(value["reason"], _REASON_VALUES, f"{name}.reason")


def _validate_schema_shape(payload: Mapping[str, object]) -> None:
    """Enforce the v1 schema's structural rules without a runtime dependency."""

    expected = {
        "schema_version",
        "basis",
        "currency",
        "valuation_as_of",
        "pricing_pack",
        "status",
        "quality",
        "total_usd",
        "priced_subtotal_usd",
        "hourly_usage",
        "daily_usage",
        "monthly_usage",
        "yearly_usage",
        "agents",
        "components",
        "unpriced_components",
        "diagnostics",
        "assumptions",
        "disclaimer",
    }
    if set(payload) != expected:
        raise ValueError("cost estimates has an invalid v1 shape")
    if payload["schema_version"] != COST_ESTIMATES_SCHEMA_VERSION:
        raise ValueError("cost estimates has an unsupported schema version")
    if payload["basis"] != VALUATION_BASIS or payload["currency"] != CURRENCY:
        raise ValueError("cost estimates has invalid fixed metadata")
    _require_date(payload["valuation_as_of"], "valuation_as_of")

    provenance = payload["pricing_pack"]
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "id",
        "kind",
        "sha256",
    }:
        raise ValueError("pricing_pack has an invalid shape")
    _require_identifier(provenance["id"], "pricing_pack.id")
    _require_one_of(
        provenance["kind"], frozenset({"builtin", "custom"}), "pricing_pack.kind"
    )
    if not isinstance(provenance["sha256"], str) or not _SHA256_PATTERN.fullmatch(
        provenance["sha256"]
    ):
        raise ValueError("pricing_pack.sha256 must be a lowercase SHA-256")

    _require_rollup(
        {
            "status": payload["status"],
            "quality": payload["quality"],
            "total_usd": payload["total_usd"],
            "priced_subtotal_usd": payload["priced_subtotal_usd"],
        },
        "cost_estimates",
    )
    agents = payload["agents"]
    if not isinstance(agents, Mapping):
        raise TypeError("agents must be an object")
    for agent, rollup in agents.items():
        _require_identifier(agent, "agents key")
        if not isinstance(rollup, Mapping):
            raise TypeError(f"agents.{agent} must be an object")
        if set(rollup) != {
            "display_name",
            "status",
            "quality",
            "total_usd",
            "priced_subtotal_usd",
            "hourly_usage",
            "daily_usage",
            "monthly_usage",
            "yearly_usage",
        }:
            raise ValueError(f"agents.{agent} has an invalid shape")
        _require_label(rollup["display_name"], f"agents.{agent}.display_name")
        _require_rollup(
            {
                key: rollup[key]
                for key in ("status", "quality", "total_usd", "priced_subtotal_usd")
            },
            f"agents.{agent}",
        )
        _require_scope_shapes(rollup, f"agents.{agent}")

    _require_scope_shapes(payload, "cost_estimates")

    for key, priced in (("components", True), ("unpriced_components", False)):
        components = payload[key]
        if not isinstance(components, list) or len(components) > 100_000:
            raise ValueError(f"{key} must be a bounded array")
        for index, component in enumerate(components):
            _require_dimensions(component, f"{key}[{index}]", priced=priced)

    diagnostics = payload["diagnostics"]
    if not isinstance(diagnostics, list) or len(diagnostics) > 10_000:
        raise ValueError("diagnostics must be a bounded array")
    for index, diagnostic in enumerate(diagnostics):
        if not isinstance(diagnostic, Mapping) or set(diagnostic) - {
            "reason",
            "source_record_id",
            "meter",
            "detail",
        }:
            raise ValueError(f"diagnostics[{index}] has an invalid shape")
        if not {"reason", "source_record_id"} <= set(diagnostic):
            raise ValueError(f"diagnostics[{index}] is missing required fields")
        _require_one_of(
            diagnostic["reason"], _REASON_VALUES, f"diagnostics[{index}].reason"
        )
        _require_identifier(
            diagnostic["source_record_id"], f"diagnostics[{index}].source_record_id"
        )
        if "meter" in diagnostic:
            _require_one_of(
                diagnostic["meter"], _METER_VALUES, f"diagnostics[{index}].meter"
            )
        if "detail" in diagnostic:
            _require_label(diagnostic["detail"], f"diagnostics[{index}].detail")
    _require_labels(payload["assumptions"], "assumptions", maximum=1000)
    _require_label(payload["disclaimer"], "disclaimer")


def serialize_cost_estimates(result: CostResult) -> JsonObject:
    """Serialize a typed result and enforce the frozen application invariants."""

    if not isinstance(result, CostResult):
        raise TypeError("result must be a CostResult")
    if _enum_value(result.rollup.status) == CostStatus.DISABLED.value:
        raise ValueError("disabled costs must be omitted from machine JSON")
    if not isinstance(result.provenance.kind, PricingPackKind):
        raise TypeError("pricing pack provenance kind must be application-owned")

    agents: dict[str, object] = {}
    for agent in result.agents:
        serialized_agent = {
            "display_name": agent.display_name,
            **_rollup(agent.rollup),
            **_serialized_scopes(
                agent.hourly, agent.daily, agent.monthly, agent.yearly
            ),
        }
        if agent.agent in agents:
            raise ValueError(f"duplicate agent rollup: {agent.agent}")
        agents[agent.agent] = serialized_agent

    payload: dict[str, object] = {
        "schema_version": COST_ESTIMATES_SCHEMA_VERSION,
        "basis": VALUATION_BASIS,
        "currency": CURRENCY,
        "valuation_as_of": result.valuation_as_of.isoformat(),
        "pricing_pack": {
            "id": result.provenance.pack_id,
            "kind": _enum_value(result.provenance.kind),
            "sha256": result.provenance.sha256,
        },
        **_rollup(result.rollup),
        **_serialized_scopes(
            result.hourly, result.daily, result.monthly, result.yearly
        ),
        "agents": agents,
        "components": [_priced_component(item) for item in result.components],
        "unpriced_components": [
            _unpriced_component(item) for item in result.unpriced_quantities
        ],
        "diagnostics": [_diagnostic(item) for item in result.diagnostics],
        "assumptions": list(result.assumptions),
        "disclaimer": _disclaimer(result.valuation_as_of),
    }

    _validate_schema_shape(payload)
    # This must remain the final gate: schema-shaped output alone cannot prove
    # component totals, status, quality, agent, and daily rollups reconcile.
    validate_cost_estimates(payload)
    return cast(JsonObject, payload)


def _serialized_scopes(hourly, daily, monthly, yearly) -> dict[str, object]:
    hourly_usage: dict[str, dict[str, object]] = {}
    for item in hourly:
        day = item.occurred_on.isoformat()
        hours = hourly_usage.setdefault(day, {})
        hour = str(item.hour)
        if hour in hours:
            raise ValueError(f"duplicate hourly cost rollup: {day} hour {hour}")
        hours[hour] = _rollup(item.rollup)
    return {
        "hourly_usage": hourly_usage,
        "daily_usage": {
            item.occurred_on.isoformat(): _rollup(item.rollup) for item in daily
        },
        "monthly_usage": {item.month: _rollup(item.rollup) for item in monthly},
        "yearly_usage": {item.year: _rollup(item.rollup) for item in yearly},
    }


def _require_scope_shapes(value: Mapping[str, object], name: str) -> None:
    hourly = value["hourly_usage"]
    if not isinstance(hourly, Mapping):
        raise TypeError(f"{name}.hourly_usage must be an object")
    for occurred_on, raw_hours in hourly.items():
        _require_date(occurred_on, f"{name}.hourly_usage key")
        if not isinstance(raw_hours, Mapping):
            raise TypeError(f"{name}.hourly_usage.{occurred_on} must be an object")
        for hour, rollup in raw_hours.items():
            if not isinstance(hour, str) or not hour.isascii() or not hour.isdigit():
                raise ValueError(f"{name}.hourly_usage hour must be canonical")
            parsed_hour = int(hour)
            if str(parsed_hour) != hour or not 0 <= parsed_hour <= 23:
                raise ValueError(f"{name}.hourly_usage hour must be 0 through 23")
            _require_rollup(rollup, f"{name}.hourly_usage.{occurred_on}.{hour}")

    daily = value["daily_usage"]
    if not isinstance(daily, Mapping):
        raise TypeError(f"{name}.daily_usage must be an object")
    for occurred_on, rollup in daily.items():
        _require_date(occurred_on, f"{name}.daily_usage key")
        _require_rollup(rollup, f"{name}.daily_usage.{occurred_on}")

    monthly = value["monthly_usage"]
    if not isinstance(monthly, Mapping):
        raise TypeError(f"{name}.monthly_usage must be an object")
    for month, rollup in monthly.items():
        if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
            raise ValueError(f"{name}.monthly_usage key must be YYYY-MM")
        _require_rollup(rollup, f"{name}.monthly_usage.{month}")

    yearly = value["yearly_usage"]
    if not isinstance(yearly, Mapping):
        raise TypeError(f"{name}.yearly_usage must be an object")
    for year, rollup in yearly.items():
        if not isinstance(year, str) or not re.fullmatch(r"[0-9]{4}", year):
            raise ValueError(f"{name}.yearly_usage key must be YYYY")
        _require_rollup(rollup, f"{name}.yearly_usage.{year}")


def attach_cost_estimates(
    legacy_payload: Mapping[str, FrozenJson], result: CostResult | None
) -> JsonObject:
    """Return legacy machine JSON unchanged when costs are disabled.

    When a result is present, copy only the root mapping and append the strict
    ``cost_estimates`` section.  Nested legacy data is deliberately untouched.
    """

    if result is None:
        return legacy_payload
    payload = dict(legacy_payload)
    payload["cost_estimates"] = serialize_cost_estimates(result)
    return cast(JsonObject, payload)


__all__ = [
    "attach_cost_estimates",
    "serialize_cost_estimates",
    "validate_cost_estimates",
]

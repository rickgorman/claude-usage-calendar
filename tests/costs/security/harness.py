from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from costs.contracts import (
    BillableQuantity,
    CostStatus,
    Diagnostic,
    DiagnosticReason,
    MeasurementQuality,
    Meter,
    Provider,
    RateKey,
    Unit,
    UnpricedQuantity,
)
from costs.engine import (
    AgentCostRollup,
    CostResult,
    CostRollup,
    DailyCostRollup,
    PricedComponent,
)
from costs.pricing import PricingPackKind, PricingProvenance

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "costs" / "fixtures"
SECURITY_FIXTURES = FIXTURES / "security"
SCHEMAS = ROOT / "costs" / "schemas"


class DuplicateJsonName(ValueError):
    """A deterministic signal that a raw JSON object repeated a member name."""


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonName(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def strict_json_bytes(raw: bytes) -> Any:
    """Decode the exact corpus bytes as UTF-8 and reject repeated names."""

    text = raw.decode("utf-8", errors="strict")
    return json.loads(text, object_pairs_hook=_unique_object)


def set_path(target: Any, path: list[Any], value: Any) -> None:
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


def materialize_pack_case(
    base: Mapping[str, Any], case: Mapping[str, Any]
) -> dict[str, Any]:
    """Build one bounded malformed pack without depending on product parsing code."""

    payload = copy.deepcopy(base)
    operation = case["operation"]
    if operation == "set":
        set_path(payload, list(case["path"]), case["value"])
    elif operation == "repeat":
        set_path(payload, list(case["path"]), case["value"] * case["count"])
    elif operation == "duplicate_rate":
        payload["rates"].append(copy.deepcopy(payload["rates"][case["index"]]))
    elif operation == "nested_unknown":
        nested: dict[str, Any] = {"leaf": True}
        for _ in range(case["depth"]):
            nested = {"nested": nested}
        payload["unexpected"] = nested
    elif operation == "rate_cardinality":
        rate = payload["rates"][0]
        payload["rates"] = []
        for index in range(case["count"]):
            item = copy.deepcopy(rate)
            item["model"] = f"bounded-model-{index}"
            payload["rates"].append(item)
    elif operation == "padding":
        payload["unexpected_padding"] = "p" * case["count"]
    else:
        raise AssertionError(f"unknown adversarial operation: {operation}")
    return payload


def raw_record(payload: Mapping[str, Any]):
    from costs.contracts import RawUsageRecord

    values = dict(payload)
    values["occurred_at"] = datetime.fromisoformat(values["occurred_at"])
    values["measurement_quality"] = MeasurementQuality(values["measurement_quality"])
    values["assumptions"] = tuple(values.get("assumptions", ()))
    return RawUsageRecord(**values)


def billable(
    source_record_id: str,
    quantity: int,
    *,
    agent: str = "codex",
    provider: Provider = Provider.OPENAI,
    model: str = "property-model-v1",
    meter: Meter = Meter.INPUT_UNCACHED,
    occurred_at: datetime,
) -> BillableQuantity:
    return BillableQuantity(
        source_record_id=source_record_id,
        agent=agent,
        provider=provider,
        model=model,
        channel="api",
        variant="standard",
        service_tier="default",
        context_band="standard",
        occurred_at=occurred_at,
        meter=meter,
        quantity=quantity,
        unit=Unit.TOKENS,
        measurement_quality=MeasurementQuality.PROVIDER_REPORTED,
        semantics_version="property.v1",
    )


def unpriced(
    source_record_id: str,
    quantity: int,
    *,
    occurred_at: datetime,
) -> UnpricedQuantity:
    return UnpricedQuantity(
        source_record_id=source_record_id,
        agent="codex",
        provider="openai",
        model="property-model-v1",
        channel="api",
        variant="standard",
        service_tier="default",
        context_band="standard",
        occurred_at=occurred_at,
        meter=Meter.OUTPUT,
        quantity=quantity,
        unit=Unit.TOKENS,
        measurement_quality=MeasurementQuality.PROVIDER_REPORTED,
        semantics_version="property.v1",
        reason=DiagnosticReason.RATE_NOT_FOUND,
    )


def _rollup(payload: Mapping[str, Any]) -> CostRollup:
    return CostRollup(
        CostStatus(payload["status"]),
        MeasurementQuality(payload["quality"]),
        Decimal(payload["total_usd"]) if payload["total_usd"] is not None else None,
        Decimal(payload["priced_subtotal_usd"]),
    )


def cost_result_from_payload(payload: Mapping[str, Any]) -> CostResult:
    provenance_payload = payload["pricing_pack"]
    provenance = PricingProvenance(
        provenance_payload["id"],
        PricingPackKind(provenance_payload["kind"]),
        provenance_payload["sha256"],
    )
    valuation = date.fromisoformat(payload["valuation_as_of"])

    components = []
    for item in payload["components"]:
        key = RateKey(
            Provider(item["provider"]),
            item["model"],
            item["channel"],
            item["variant"],
            item["service_tier"],
            item["context_band"],
            Meter(item["meter"]),
            valuation,
            MeasurementQuality(item["measurement_quality"]),
        )
        exact = Decimal(item["quantity"]) * Decimal(item["price"]) / Decimal(1_000_000)
        components.append(
            PricedComponent(
                tuple(item["source_record_ids"]),
                item["agent"],
                key,
                date.fromisoformat(item["occurred_on"]),
                item["quantity"],
                Unit(item["unit"]),
                Decimal(item["price"]),
                exact,
                Decimal(item["cost_usd"]),
                tuple(item["assumptions"]),
            )
        )

    unpriced_items = []
    for item in payload["unpriced_components"]:
        unpriced_items.append(
            UnpricedQuantity(
                source_record_id=item["source_record_ids"][0],
                agent=item["agent"],
                provider=item["provider"],
                model=item["model"],
                channel=item["channel"],
                variant=item["variant"],
                service_tier=item["service_tier"],
                context_band=item["context_band"],
                occurred_at=datetime.fromisoformat(
                    item["occurred_on"] + "T00:00:00+00:00"
                ),
                meter=Meter(item["meter"]) if item["meter"] else None,
                quantity=item["quantity"],
                unit=Unit(item["unit"]),
                measurement_quality=MeasurementQuality(item["measurement_quality"]),
                semantics_version="dashboard.fixture.v1",
                reason=DiagnosticReason(item["reason"]),
                assumptions=tuple(item["assumptions"]),
            )
        )

    diagnostics = tuple(
        Diagnostic(
            DiagnosticReason(item["reason"]),
            item["source_record_id"],
            Meter(item["meter"]) if item.get("meter") else None,
            item.get("detail"),
        )
        for item in payload["diagnostics"]
    )
    agents = []
    for agent, item in payload["agents"].items():
        daily = tuple(
            DailyCostRollup(date.fromisoformat(day), _rollup(day_payload))
            for day, day_payload in item["daily_usage"].items()
        )
        agents.append(
            AgentCostRollup(agent, item["display_name"], _rollup(item), daily)
        )
    return CostResult(
        valuation,
        provenance,
        _rollup(payload),
        tuple(components),
        tuple(unpriced_items),
        diagnostics,
        tuple(agents),
        tuple(payload["assumptions"]),
    )


def inject_dashboard_surface(payload: dict[str, Any], surface: str, value: str) -> None:
    if surface == "agent":
        payload["agents"]["composer"]["display_name"] = value
    elif surface == "provider":
        payload["unpriced_components"][-1]["provider"] = value
    elif surface == "model":
        payload["components"][0]["model"] = value
    elif surface == "session":
        payload["unpriced_components"][-1]["source_record_ids"] = [value]
    elif surface == "pack":
        payload["pricing_pack"]["id"] = value
    elif surface == "warning":
        payload["diagnostics"][0]["detail"] = value
    elif surface == "legend":
        payload["agents"]["claude"]["display_name"] = value
    elif surface == "tooltip":
        payload["unpriced_components"][0]["assumptions"] = [value]
    elif surface == "explain":
        payload["assumptions"] = [value]
    else:
        raise AssertionError(f"unmapped dashboard surface: {surface}")


class HtmlProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag_shapes: list[tuple[str, tuple[str, ...]]] = []
        self.event_attributes: list[str] = []
        self.remote_attributes: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = tuple(sorted(name for name, _ in attrs))
        self.tag_shapes.append((tag, names))
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.event_attributes.append(name)
            if (
                name.lower() in {"href", "src", "action", "formaction"}
                and value
                and value.strip()
                .lower()
                .startswith(("http://", "https://", "//", "javascript:"))
            ):
                self.remote_attributes.append(value)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


def probe_html(html: str) -> HtmlProbe:
    probe = HtmlProbe()
    probe.feed(html)
    probe.close()
    return probe

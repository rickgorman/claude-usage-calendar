"""Golden coverage for the v1 cost serializer."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from costs.contracts import (
    ContractViolation,
    CostStatus,
    Diagnostic,
    DiagnosticReason,
    MeasurementQuality,
    Meter,
    Provider,
    RateKey,
    Unit,
    UnpricedQuantity,
    validate_cost_estimates,
)
from costs.engine import (
    AgentCostRollup,
    CostResult,
    CostRollup,
    DailyCostRollup,
    HourlyCostRollup,
    MonthlyCostRollup,
    PricedComponent,
    YearlyCostRollup,
    price_quantities,
)
from costs.pricing import (
    PricingLoadOptions,
    PricingPackKind,
    PricingProvenance,
    load_pricing_pack,
)
from costs.serializer import attach_cost_estimates, serialize_cost_estimates

FIXTURES = Path(__file__).resolve().parents[2] / "costs" / "fixtures" / "serializer"
SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "costs"
    / "schemas"
    / "cost-estimates-v1.schema.json"
)
DAY = date(2026, 8, 1)
OCCURRED_AT = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def rollup(
    status: CostStatus, quality: MeasurementQuality, subtotal: str
) -> CostRollup:
    amount = Decimal(subtotal)
    return CostRollup(
        status=status,
        quality=quality,
        total_usd=amount if status is CostStatus.COMPLETE else None,
        priced_subtotal_usd=amount,
    )


def priced_component(
    quality: MeasurementQuality = MeasurementQuality.PROVIDER_REPORTED,
) -> PricedComponent:
    return PricedComponent(
        source_record_ids=("source-1",),
        agent="claude",
        rate_key=RateKey(
            provider=Provider.ANTHROPIC,
            model="model-v1",
            channel="api",
            variant="standard",
            service_tier="default",
            context_band="standard",
            meter=Meter.INPUT_UNCACHED,
            valuation_date=DAY,
            measurement_quality=quality,
        ),
        occurred_on=DAY,
        quantity=10,
        unit=Unit.TOKENS,
        price=Decimal(1),
        exact_cost_usd=Decimal("0.000010"),
        cost_usd=Decimal("0.000010"),
        occurred_hour=12,
    )


def unpriced_component(
    quality: MeasurementQuality = MeasurementQuality.DERIVED,
) -> UnpricedQuantity:
    return UnpricedQuantity(
        source_record_id="source-2",
        agent="claude",
        provider="future_provider",
        model="future-model-v1",
        channel=None,
        variant=None,
        service_tier=None,
        context_band=None,
        occurred_at=OCCURRED_AT,
        meter=None,
        quantity=7,
        unit=Unit.TOKENS,
        measurement_quality=quality,
        semantics_version="test.v1",
        reason=DiagnosticReason.UNKNOWN_PROVIDER,
        assumptions=("provider is not in v1",),
    )


def result_for(status: CostStatus) -> CostResult:
    if status is CostStatus.COMPLETE:
        components = (priced_component(),)
        unpriced = ()
        quality = MeasurementQuality.PROVIDER_REPORTED
        root_rollup = rollup(status, quality, "0.000010")
    elif status is CostStatus.PARTIAL:
        components = (priced_component(),)
        unpriced = (unpriced_component(MeasurementQuality.ESTIMATED),)
        quality = MeasurementQuality.MIXED
        root_rollup = rollup(status, quality, "0.000010")
    elif status is CostStatus.UNPRICED:
        components = ()
        unpriced = (unpriced_component(),)
        quality = MeasurementQuality.DERIVED
        root_rollup = rollup(status, quality, "0.000000")
    else:
        raise ValueError("disabled results are deliberately not serializable")

    hourly = (HourlyCostRollup(occurred_on=DAY, hour=12, rollup=root_rollup),)
    daily = (DailyCostRollup(occurred_on=DAY, rollup=root_rollup),)
    monthly = (MonthlyCostRollup(month="2026-08", rollup=root_rollup),)
    yearly = (YearlyCostRollup(year="2026", rollup=root_rollup),)
    agents = (
        AgentCostRollup(
            agent="claude",
            display_name="Claude",
            rollup=root_rollup,
            daily=daily,
            hourly=hourly,
            monthly=monthly,
            yearly=yearly,
        ),
    )
    return CostResult(
        valuation_as_of=DAY,
        provenance=PricingProvenance(
            pack_id="fixture.serializer.v1",
            kind=PricingPackKind.CUSTOM,
            sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        ),
        rollup=root_rollup,
        components=components,
        unpriced_quantities=unpriced,
        diagnostics=(
            Diagnostic(
                reason=DiagnosticReason.UNKNOWN_PROVIDER,
                source_record_id="source-2",
                detail="future_provider cannot be priced",
            ),
        )
        if unpriced
        else (),
        agents=agents,
        assumptions=("serializer golden fixture",),
        hourly=hourly,
        daily=daily,
        monthly=monthly,
        yearly=yearly,
    )


def schema_accepts(payload):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        command = shutil.which("check-jsonschema")
        prefix = [command] if command else None
        if prefix is None and shutil.which("uvx"):
            prefix = ["uvx", "--from", "check-jsonschema", "check-jsonschema"]
        if prefix is None:
            raise AssertionError(
                "jsonschema module or check-jsonschema CLI is required"
            )
        with tempfile.TemporaryDirectory() as directory:
            payload_path = Path(directory) / "cost-estimates.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(
                [*prefix, "--schemafile", str(SCHEMA), str(payload_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        return result.returncode == 0
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return not list(Draft202012Validator(schema).iter_errors(payload))


class BadDate:
    def isoformat(self):
        return "2026-99-99"


class SerializerTests(unittest.TestCase):
    def test_status_and_quality_goldens_pass_application_validation(self):
        for status in (CostStatus.COMPLETE, CostStatus.PARTIAL, CostStatus.UNPRICED):
            with self.subTest(status=status.value):
                actual = serialize_cost_estimates(result_for(status))
                expected = json.loads(
                    (FIXTURES / f"{status.value}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(actual, expected)
                self.assertEqual(set(actual["monthly_usage"]), {"2026-08"})
                self.assertEqual(set(actual["yearly_usage"]), {"2026"})
                self.assertTrue(schema_accepts(actual))
                validate_cost_estimates(actual)

    def test_all_four_scope_maps_are_mandatory_at_root_and_agent(self):
        scope_names = (
            "hourly_usage",
            "daily_usage",
            "monthly_usage",
            "yearly_usage",
        )
        for scope_name in scope_names:
            with self.subTest(scope=scope_name, owner="root"):
                payload = dict(serialize_cost_estimates(result_for(CostStatus.COMPLETE)))
                payload.pop(scope_name)
                self.assertFalse(schema_accepts(payload))
                with self.assertRaises(ContractViolation):
                    validate_cost_estimates(payload)

            with self.subTest(scope=scope_name, owner="agent"):
                payload = dict(serialize_cost_estimates(result_for(CostStatus.COMPLETE)))
                payload["agents"] = dict(payload["agents"])
                agent = dict(payload["agents"]["claude"])
                agent.pop(scope_name)
                payload["agents"]["claude"] = agent
                self.assertFalse(schema_accepts(payload))
                with self.assertRaises(ContractViolation):
                    validate_cost_estimates(payload)

    def test_custom_provenance_is_emitted_from_the_typed_application_result(self):
        payload = serialize_cost_estimates(result_for(CostStatus.COMPLETE))
        self.assertEqual(payload["pricing_pack"]["kind"], "custom")
        self.assertEqual(payload["pricing_pack"]["id"], "fixture.serializer.v1")

    def test_enabled_empty_engine_result_serializes_as_complete_zero_cost(self):
        pack = load_pricing_pack(PricingLoadOptions()).pack
        result = price_quantities((), (), pack)
        payload = serialize_cost_estimates(result)

        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["total_usd"], "0.000000")
        self.assertEqual(payload["priced_subtotal_usd"], "0.000000")
        self.assertEqual(payload["components"], [])
        self.assertEqual(payload["unpriced_components"], [])
        self.assertEqual(payload["agents"], {})
        self.assertTrue(schema_accepts(payload))
        validate_cost_estimates(payload)

    def test_disabled_legacy_payload_is_the_same_mapping(self):
        legacy = {
            "timezone": "UTC",
            "unknown_future_field": {"keep": ["this", "unchanged"]},
        }
        self.assertIs(attach_cost_estimates(legacy, None), legacy)

    def test_enabled_payload_adds_only_the_versioned_cost_section(self):
        legacy = {"timezone": "UTC", "unknown_future_field": {"keep": True}}
        payload = attach_cost_estimates(legacy, result_for(CostStatus.COMPLETE))
        self.assertEqual(
            legacy, {"timezone": "UTC", "unknown_future_field": {"keep": True}}
        )
        self.assertEqual(
            set(payload), {"timezone", "unknown_future_field", "cost_estimates"}
        )
        self.assertEqual(payload["cost_estimates"]["schema_version"], 1)

    def test_disabled_result_is_rejected_because_disabled_means_no_section(self):
        disabled = CostResult(
            valuation_as_of=DAY,
            provenance=PricingProvenance(
                pack_id="fixture.serializer.v1",
                kind=PricingPackKind.BUILTIN,
                sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            ),
            rollup=rollup(
                CostStatus.DISABLED, MeasurementQuality.ESTIMATED, "0.000000"
            ),
            components=(),
            unpriced_quantities=(),
            diagnostics=(),
            agents=(),
        )
        with self.assertRaisesRegex(ValueError, "omitted"):
            serialize_cost_estimates(disabled)

    def test_hostile_typed_fields_are_rejected_before_invariant_validation(self):
        cases = (
            (
                "reserved-pack-id",
                lambda result: object.__setattr__(
                    result.provenance, "pack_id", "constructor"
                ),
            ),
            (
                "bad-sha",
                lambda result: object.__setattr__(result.provenance, "sha256", "ABC"),
            ),
            (
                "unowned-pack-kind",
                lambda result: object.__setattr__(result.provenance, "kind", "custom"),
            ),
            (
                "invalid-date",
                lambda result: object.__setattr__(result, "valuation_as_of", BadDate()),
            ),
            (
                "reserved-agent",
                lambda result: object.__setattr__(
                    result.agents[0], "agent", "__proto__"
                ),
            ),
            (
                "empty-display-label",
                lambda result: object.__setattr__(result.agents[0], "display_name", ""),
            ),
            (
                "reserved-source-id",
                lambda result: object.__setattr__(
                    result.components[0], "source_record_ids", ("constructor",)
                ),
            ),
            (
                "oversized-diagnostic",
                lambda result: object.__setattr__(
                    result.diagnostics[0], "detail", "x" * 2049
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                result = result_for(CostStatus.PARTIAL)
                mutate(result)
                with self.assertRaises((TypeError, ValueError)):
                    serialize_cost_estimates(result)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext

from costs.contracts import (
    BillableQuantity,
    CostStatus,
    Diagnostic,
    DiagnosticReason,
    MeasurementQuality,
    Meter,
    Provider,
    RateKey,
    RateUnit,
    Unit,
    UnpricedQuantity,
)
from costs.engine import price_quantities
from costs.pricing import (
    PricingPack,
    PricingPackKind,
    PricingProvenance,
    PricingSource,
    RateCard,
)

VALUATION_DATE = date(2026, 8, 28)
DAY_ONE = datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)
DAY_TWO = datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc)


class ExactCostEngineTests(unittest.TestCase):
    def quantity(self, **changes) -> BillableQuantity:
        values = {
            "source_record_id": "record-1",
            "agent": "codex",
            "provider": Provider.OPENAI,
            "model": "exact-model",
            "channel": "api",
            "variant": "standard",
            "service_tier": "default",
            "context_band": "standard",
            "occurred_at": DAY_ONE,
            "meter": Meter.INPUT_UNCACHED,
            "quantity": 1,
            "unit": Unit.TOKENS,
            "measurement_quality": MeasurementQuality.PROVIDER_REPORTED,
            "semantics_version": "openai.v1",
            "assumptions": (),
        }
        values.update(changes)
        return BillableQuantity(**values)

    def unpriced(self, **changes) -> UnpricedQuantity:
        values = {
            "source_record_id": "unpriced-1",
            "agent": "codex",
            "provider": Provider.OPENAI,
            "model": "exact-model",
            "channel": "api",
            "variant": "standard",
            "service_tier": "default",
            "context_band": "standard",
            "occurred_at": DAY_ONE,
            "meter": Meter.OUTPUT,
            "quantity": 3,
            "unit": Unit.TOKENS,
            "measurement_quality": MeasurementQuality.PROVIDER_REPORTED,
            "semantics_version": "openai.v1",
            "reason": DiagnosticReason.UNSUPPORTED_METER,
            "assumptions": (),
        }
        values.update(changes)
        return UnpricedQuantity(**values)

    def key(self, **changes) -> RateKey:
        values = {
            "provider": Provider.OPENAI,
            "model": "exact-model",
            "channel": "api",
            "variant": "standard",
            "service_tier": "default",
            "context_band": "standard",
            "meter": Meter.INPUT_UNCACHED,
            "valuation_date": VALUATION_DATE,
            "measurement_quality": MeasurementQuality.PROVIDER_REPORTED,
        }
        values.update(changes)
        return RateKey(**values)

    def rate(self, price: str = "1", **key_changes) -> RateCard:
        return RateCard(
            key=self.key(**key_changes),
            unit=RateUnit.MILLION_TOKENS,
            price=Decimal(price),
            source=PricingSource("test", "https://example.invalid", VALUATION_DATE),
        )

    def pack(self, *rates: RateCard) -> PricingPack:
        return PricingPack(
            display_name="TEST ONLY",
            valuation_as_of=VALUATION_DATE,
            rates=tuple(rates),
            provenance=PricingProvenance("test-pack", PricingPackKind.CUSTOM, "0" * 64),
        )

    def test_aggregates_by_exact_key_agent_and_day_in_canonical_order(self):
        first = self.quantity(
            source_record_id="z-record", quantity=2, assumptions=("z",)
        )
        second = self.quantity(
            source_record_id="a-record", quantity=3, assumptions=("a",)
        )
        other_day = self.quantity(
            source_record_id="next-day", occurred_at=DAY_TWO, quantity=5
        )
        other_quality = self.quantity(
            source_record_id="derived",
            quantity=7,
            measurement_quality=MeasurementQuality.DERIVED,
        )
        rates = (
            self.rate("2"),
            self.rate("4", measurement_quality=MeasurementQuality.DERIVED),
        )

        result = price_quantities(
            [other_quality, other_day, second, first], [], self.pack(*reversed(rates))
        )
        reordered = price_quantities(
            [first, second, other_day, other_quality], [], self.pack(*rates)
        )

        self.assertEqual(result, reordered)
        self.assertEqual(len(result.components), 3)
        aggregated = next(
            item
            for item in result.components
            if item.occurred_on == DAY_ONE.date()
            and item.rate_key.measurement_quality
            is MeasurementQuality.PROVIDER_REPORTED
        )
        self.assertEqual(aggregated.quantity, 5)
        self.assertEqual(aggregated.source_record_ids, ("a-record", "z-record"))
        self.assertEqual(aggregated.assumptions, ("a", "z"))
        self.assertEqual(result.rollup.quality, MeasurementQuality.MIXED)
        self.assertEqual(
            [item.occurred_on for item in result.agents[0].daily],
            [DAY_ONE.date(), DAY_TWO.date()],
        )
        self.assertEqual(result.agents[0].display_name, "codex")

    def test_full_precision_rollups_round_once_not_from_component_displays(self):
        quantities = [
            self.quantity(source_record_id="input", quantity=4),
            self.quantity(source_record_id="output", quantity=4, meter=Meter.OUTPUT),
        ]
        result = price_quantities(
            quantities,
            [],
            self.pack(self.rate("0.1"), self.rate("0.1", meter=Meter.OUTPUT)),
        )

        self.assertEqual(
            [item.exact_cost_usd for item in result.components],
            [Decimal("0.0000004"), Decimal("0.0000004")],
        )
        self.assertEqual(
            [item.cost_usd for item in result.components],
            [Decimal("0.000000"), Decimal("0.000000")],
        )
        self.assertEqual(result.rollup.priced_subtotal_usd, Decimal("0.000001"))
        self.assertEqual(result.rollup.total_usd, Decimal("0.000001"))
        self.assertEqual(
            result.agents[0].daily[0].rollup.priced_subtotal_usd,
            Decimal("0.000001"),
        )

    def test_component_half_up_boundary(self):
        result = price_quantities(
            [self.quantity(quantity=5)], [], self.pack(self.rate("0.1"))
        )
        self.assertEqual(result.components[0].exact_cost_usd, Decimal("0.0000005"))
        self.assertEqual(result.components[0].cost_usd, Decimal("0.000001"))

    def test_large_values_ignore_ambient_decimal_precision(self):
        quantity = 10**80 + 123456789
        price = Decimal("123456789012.345678901234")
        with localcontext() as context:
            context.prec = 3
            result = price_quantities(
                [self.quantity(quantity=quantity)],
                [],
                self.pack(self.rate(str(price))),
            )
        with localcontext() as context:
            context.prec = 180
            expected = Decimal(quantity) * price / Decimal(1_000_000)
            displayed = expected.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

        self.assertEqual(result.components[0].exact_cost_usd, expected)
        self.assertEqual(result.components[0].cost_usd, displayed)
        self.assertEqual(result.rollup.priced_subtotal_usd, displayed)

    def test_missing_and_ambiguous_rates_become_visible_unpriced_usage(self):
        priced = self.quantity(source_record_id="priced", quantity=10)
        missing = self.quantity(
            source_record_id="missing", quantity=20, meter=Meter.OUTPUT
        )
        ambiguous = self.quantity(
            source_record_id="ambiguous",
            quantity=30,
            context_band="long",
        )
        ambiguous_rate = self.rate("2", context_band="long")
        result = price_quantities(
            [ambiguous, missing, priced],
            [],
            self.pack(self.rate("1"), ambiguous_rate, ambiguous_rate),
        )

        self.assertEqual(result.rollup.status, CostStatus.PARTIAL)
        self.assertIsNone(result.rollup.total_usd)
        self.assertEqual(result.rollup.priced_subtotal_usd, Decimal("0.000010"))
        self.assertEqual(
            {item.source_record_id: item.reason for item in result.unpriced_quantities},
            {
                "ambiguous": DiagnosticReason.RATE_AMBIGUOUS,
                "missing": DiagnosticReason.RATE_NOT_FOUND,
            },
        )
        self.assertEqual(
            {(item.source_record_id, item.reason) for item in result.diagnostics},
            {
                ("ambiguous", DiagnosticReason.RATE_AMBIGUOUS),
                ("missing", DiagnosticReason.RATE_NOT_FOUND),
            },
        )

    def test_adapter_diagnostic_details_are_preserved_and_deduplicated(self):
        missing = self.quantity(
            source_record_id="missing", quantity=20, meter=Meter.OUTPUT
        )
        detailed = Diagnostic(
            reason=DiagnosticReason.RATE_NOT_FOUND,
            source_record_id="missing",
            meter=Meter.OUTPUT,
            detail="adapter retained the exact source context",
        )
        distinct_detail = Diagnostic(
            reason=DiagnosticReason.RATE_NOT_FOUND,
            source_record_id="missing",
            meter=Meter.OUTPUT,
            detail="a second distinct adapter observation",
        )
        result = price_quantities(
            [missing],
            [],
            self.pack(),
            diagnostics=[detailed, distinct_detail, detailed],
        )

        self.assertEqual(
            result.diagnostics,
            (distinct_detail, detailed),
        )
        self.assertNotIn(None, [item.detail for item in result.diagnostics])

    def test_diagnostic_limit_is_deterministic_and_reports_truncation(self):
        diagnostics = [
            Diagnostic(
                reason=DiagnosticReason.UNSUPPORTED_CHARGE,
                source_record_id=f"record-{index:05}",
                detail=f"detail-{index:05}",
            )
            for index in range(10_001)
        ]
        visible_unpriced = self.unpriced(source_record_id="still-visible", quantity=99)

        forward = price_quantities(
            [], [visible_unpriced], self.pack(), diagnostics=diagnostics
        )
        reverse = price_quantities(
            [],
            [visible_unpriced],
            self.pack(),
            diagnostics=list(reversed(diagnostics)),
        )

        self.assertEqual(forward, reverse)
        self.assertEqual(len(forward.diagnostics), 10_000)
        self.assertEqual(forward.diagnostics[0].source_record_id, "record-00000")
        self.assertEqual(forward.diagnostics[-1].source_record_id, "record-09999")
        truncation_assumption = (
            "diagnostics truncated: kept 10000 of 10001 deterministic entries; "
            "inspect unpriced components for complete unsupported-usage coverage"
        )
        self.assertEqual(
            forward.assumptions,
            (truncation_assumption,),
        )
        self.assertEqual(forward.unpriced_quantities, (visible_unpriced,))

    def test_valuation_date_is_part_of_exact_lookup_with_no_fallback(self):
        wrong_date_rate = self.rate("1")
        wrong_date_rate = RateCard(
            key=RateKey(
                **{
                    **{
                        name: getattr(wrong_date_rate.key, name)
                        for name in wrong_date_rate.key.__slots__
                    },
                    "valuation_date": date(2026, 8, 27),
                }
            ),
            unit=wrong_date_rate.unit,
            price=wrong_date_rate.price,
            source=wrong_date_rate.source,
        )
        result = price_quantities(
            [self.quantity(quantity=10)], [], self.pack(wrong_date_rate)
        )

        self.assertEqual(result.rollup.status, CostStatus.UNPRICED)
        self.assertEqual(result.rollup.priced_subtotal_usd, Decimal("0.000000"))
        self.assertIsNone(result.rollup.total_usd)
        self.assertEqual(
            result.unpriced_quantities[0].reason, DiagnosticReason.RATE_NOT_FOUND
        )

    def test_quality_is_independent_from_status_at_every_scope(self):
        priced = self.quantity(
            source_record_id="derived",
            agent="composer",
            measurement_quality=MeasurementQuality.DERIVED,
        )
        unpriced = self.unpriced(
            source_record_id="estimated",
            agent="composer",
            measurement_quality=MeasurementQuality.ESTIMATED,
        )
        other_agent = self.unpriced(
            source_record_id="provider-reported",
            agent="codex",
            occurred_at=DAY_TWO,
        )
        result = price_quantities(
            [priced],
            [other_agent, unpriced],
            self.pack(self.rate("1", measurement_quality=MeasurementQuality.DERIVED)),
        )

        self.assertEqual(result.rollup.status, CostStatus.PARTIAL)
        self.assertEqual(result.rollup.quality, MeasurementQuality.MIXED)
        by_agent = {item.agent: item for item in result.agents}
        self.assertEqual(by_agent["composer"].rollup.status, CostStatus.PARTIAL)
        self.assertEqual(by_agent["composer"].rollup.quality, MeasurementQuality.MIXED)
        self.assertEqual(by_agent["codex"].rollup.status, CostStatus.UNPRICED)
        self.assertEqual(
            by_agent["codex"].rollup.quality,
            MeasurementQuality.PROVIDER_REPORTED,
        )

    def test_empty_enabled_input_is_complete_zero_and_inputs_are_not_mutated(self):
        source = self.quantity(assumptions=("immutable",))
        quantities = [source]
        unpriced: list[UnpricedQuantity] = []
        price_quantities(quantities, unpriced, self.pack(self.rate()))
        self.assertEqual(quantities, [source])
        self.assertEqual(unpriced, [])

        empty = price_quantities([], [], self.pack(self.rate()))
        self.assertEqual(empty.rollup.status, CostStatus.COMPLETE)
        self.assertEqual(empty.rollup.quality, MeasurementQuality.PROVIDER_REPORTED)
        self.assertEqual(empty.rollup.total_usd, Decimal("0.000000"))
        self.assertEqual(empty.rollup.priced_subtotal_usd, Decimal("0.000000"))
        self.assertEqual(empty.components, ())
        self.assertEqual(empty.agents, ())


if __name__ == "__main__":
    unittest.main()

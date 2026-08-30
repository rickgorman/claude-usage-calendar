from __future__ import annotations

import copy
import random
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from costs.adapters import anthropic, cursor, openai, xai
from costs.contracts import (
    CostStatus,
    MeasurementQuality,
    Meter,
    Provider,
    RateKey,
    RateUnit,
)
from costs.engine import price_quantities
from costs.pricing import (
    PricingPack,
    PricingPackKind,
    PricingProvenance,
    PricingSource,
    RateCard,
)
from tests.costs.security.harness import (
    FIXTURES,
    SECURITY_FIXTURES,
    billable,
    load_json,
    raw_record,
    unpriced,
)

EXPECTED_MISSING = "EXPECTED_MISSING_IMPLEMENTATION"
OCCURRED_AT = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
VALUATION_DATE = date(2026, 8, 1)


def property_pack(*, price: str = "2", include_rate: bool = True) -> PricingPack:
    key = RateKey(
        Provider.OPENAI,
        "property-model-v1",
        "api",
        "standard",
        "default",
        "standard",
        Meter.INPUT_UNCACHED,
        VALUATION_DATE,
        MeasurementQuality.PROVIDER_REPORTED,
    )
    rates = (
        (
            RateCard(
                key,
                RateUnit.MILLION_TOKENS,
                Decimal(price),
                PricingSource(
                    "Property-test synthetic rate",
                    "https://example.invalid/property-rate",
                    VALUATION_DATE,
                ),
            ),
        )
        if include_rate
        else ()
    )
    return PricingPack(
        "Property-test pack",
        VALUATION_DATE,
        rates,
        PricingProvenance("property.pack.v1", PricingPackKind.BUILTIN, "1" * 64),
    )


def price_or_skip(
    test: unittest.TestCase, quantities, unpriced_quantities=(), *, pack=None
):
    try:
        return price_quantities(
            quantities,
            unpriced_quantities,
            pack if pack is not None else property_pack(),
        )
    except NotImplementedError as error:
        test.skipTest(f"{EXPECTED_MISSING}: cost engine: {error}")


def _key_tuple(key):
    return tuple(
        getattr(key, name)
        for name in (
            "provider",
            "model",
            "channel",
            "variant",
            "service_tier",
            "context_band",
            "meter",
            "valuation_date",
            "measurement_quality",
        )
    )


def logical_result(result):
    """Canonicalize unordered result collections without weakening value checks."""

    components = sorted(
        (
            tuple(sorted(item.source_record_ids)),
            item.agent,
            _key_tuple(item.rate_key),
            item.occurred_on,
            item.quantity,
            item.unit,
            item.price,
            item.exact_cost_usd,
            item.cost_usd,
            tuple(sorted(item.assumptions)),
        )
        for item in result.components
    )
    unpriced_items = sorted(repr(item) for item in result.unpriced_quantities)
    diagnostics = sorted(repr(item) for item in result.diagnostics)
    agents = sorted(repr(item) for item in result.agents)
    return result.rollup, components, unpriced_items, diagnostics, agents


def frozen_snapshot(value):
    if isinstance(value, dict) or hasattr(value, "items"):
        return tuple(
            sorted((key, frozen_snapshot(item)) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(frozen_snapshot(item) for item in value)
    return value


class CostEnginePropertyTests(unittest.TestCase):
    def test_cost_is_monotonic_for_nonnegative_quantity(self):
        rng = random.Random(8259)
        for index in range(32):
            low = rng.randint(1, 1_000_000)
            high = low + rng.randint(0, 1_000_000)
            with self.subTest(index=index, low=low, high=high):
                lower = price_or_skip(
                    self, [billable(f"low-{index}", low, occurred_at=OCCURRED_AT)]
                )
                higher = price_or_skip(
                    self, [billable(f"high-{index}", high, occurred_at=OCCURRED_AT)]
                )
                self.assertLessEqual(
                    lower.rollup.priced_subtotal_usd,
                    higher.rollup.priced_subtotal_usd,
                )

    def test_order_independence_for_same_multiset(self):
        values = [
            billable(f"order-{index}", quantity, occurred_at=OCCURRED_AT)
            for index, quantity in enumerate((17, 1_000_003, 9, 31, 500_000, 2))
        ]
        shuffled = list(values)
        random.Random(20260828).shuffle(shuffled)
        forward = price_or_skip(self, values)
        reverse = price_or_skip(self, shuffled)
        self.assertEqual(logical_result(forward), logical_result(reverse))

    def test_disjoint_dataset_costs_are_additive(self):
        left = [billable("left", 1_000_000, occurred_at=OCCURRED_AT)]
        right = [billable("right", 2_000_000, occurred_at=OCCURRED_AT)]
        left_result = price_or_skip(self, left)
        right_result = price_or_skip(self, right)
        union_result = price_or_skip(self, left + right)
        self.assertEqual(
            union_result.rollup.priced_subtotal_usd,
            left_result.rollup.priced_subtotal_usd
            + right_result.rollup.priced_subtotal_usd,
        )

    def test_complete_has_no_unpriced_and_partial_has_null_total(self):
        priced = billable("priced", 1_000_000, occurred_at=OCCURRED_AT)
        complete = price_or_skip(self, [priced])
        self.assertEqual(complete.rollup.status, CostStatus.COMPLETE)
        self.assertEqual(complete.unpriced_quantities, ())
        self.assertIsNotNone(complete.rollup.total_usd)
        self.assertEqual(complete.rollup.total_usd, complete.rollup.priced_subtotal_usd)

        unknown = unpriced("visible-unknown", 7, occurred_at=OCCURRED_AT)
        partial = price_or_skip(self, [priced], [unknown])
        self.assertEqual(partial.rollup.status, CostStatus.PARTIAL)
        self.assertTrue(partial.components)
        self.assertTrue(partial.unpriced_quantities)
        self.assertIsNone(partial.rollup.total_usd)

    def test_raw_billable_inputs_are_not_mutated(self):
        quantities = tuple(
            billable(f"immutable-{index}", index + 1, occurred_at=OCCURRED_AT)
            for index in range(8)
        )
        before = copy.deepcopy(quantities)
        price_or_skip(self, quantities)
        self.assertEqual(quantities, before)

    def test_diagnostics_and_components_are_bounded_by_input_records(self):
        quantities = tuple(
            billable(
                f"missing-{index}",
                index + 1,
                model=f"missing-model-{index}",
                occurred_at=OCCURRED_AT,
            )
            for index in range(128)
        )
        result = price_or_skip(self, quantities, pack=property_pack(include_rate=False))
        self.assertLessEqual(len(result.components), len(quantities))
        self.assertLessEqual(len(result.unpriced_quantities), len(quantities))
        self.assertLessEqual(len(result.diagnostics), len(quantities))
        self.assertLessEqual(len(result.diagnostics), 10_000)

    def test_token_quantity_is_never_used_as_an_allocation_loop_bound(self):
        original_range = range

        def guarded_range(*args):
            if any(isinstance(value, int) and abs(value) > 10_000 for value in args):
                raise AssertionError("token quantity was expanded through range()")
            return original_range(*args)

        quantity = billable("allocation-bound", 10**12, occurred_at=OCCURRED_AT)
        with patch("builtins.range", guarded_range):
            result = price_or_skip(self, [quantity])
        self.assertLessEqual(len(result.components), 1)
        self.assertLessEqual(len(result.diagnostics), 1)


class AdapterImmutabilityAndMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend = load_json(FIXTURES / "synthetic_backend_v1.json")
        cls.raw_fixture_records = backend["raw_usage_records"]
        cls.mutations = load_json(SECURITY_FIXTURES / "adapter_mutations_v1.json")[
            "cases"
        ]
        cls.adapters = {
            "anthropic": anthropic.normalize,
            "openai": openai.normalize,
            "xai": xai.normalize,
            "cursor": cursor.normalize,
        }

    def normalize_or_skip(self, adapter_name, record):
        try:
            return self.adapters[adapter_name](record)
        except NotImplementedError as error:
            self.skipTest(
                f"{EXPECTED_MISSING}: {adapter_name} semantic adapter: {error}"
            )

    def mutation(self, name):
        return next(case for case in self.mutations if case["name"] == name)

    def test_source_adapters_never_mutate_raw_records(self):
        for payload in self.raw_fixture_records:
            adapter_name = payload["provider"]
            if adapter_name not in self.adapters:
                continue
            with self.subTest(adapter=adapter_name, record=payload["source_record_id"]):
                record = raw_record(payload)
                before = frozen_snapshot(record.source)
                self.normalize_or_skip(adapter_name, record)
                self.assertEqual(frozen_snapshot(record.source), before)

    def test_mutation_anthropic_input_is_not_cache_subtracted(self):
        case = self.mutation("anthropic_input_is_not_cache_subtracted")
        result = self.normalize_or_skip("anthropic", raw_record(case["record"]))
        meters = {
            meter: sum(
                item.quantity for item in result.quantities if item.meter is meter
            )
            for meter in Meter
        }
        self.assertEqual(meters[Meter.INPUT_UNCACHED], 40)
        self.assertEqual(meters[Meter.INPUT_CACHE_READ], 20)

    def test_mutation_openai_inclusive_input_subtracts_cache(self):
        case = self.mutation("openai_inclusive_input_subtracts_cache")
        result = self.normalize_or_skip("openai", raw_record(case["record"]))
        meters = {
            meter: sum(
                item.quantity for item in result.quantities if item.meter is meter
            )
            for meter in Meter
        }
        self.assertEqual(meters[Meter.INPUT_UNCACHED], 70)
        self.assertEqual(meters[Meter.INPUT_CACHE_READ], 30)

    def test_mutation_openai_reasoning_is_not_double_counted(self):
        case = self.mutation("openai_reasoning_is_not_double_counted")
        result = self.normalize_or_skip("openai", raw_record(case["record"]))
        output = sum(
            item.quantity for item in result.quantities if item.meter is Meter.OUTPUT
        )
        self.assertEqual(output, 100)

    def test_mutation_openai_negative_remainder_is_never_clamped(self):
        case = self.mutation("openai_negative_remainder_is_not_clamped")
        result = self.normalize_or_skip("openai", raw_record(case["record"]))
        self.assertEqual(result.quantities, ())
        actual = {
            (
                item.meter.value if item.meter else None,
                item.quantity,
                item.reason.value,
            )
            for item in result.unpriced_quantities
        }
        self.assertEqual(
            actual,
            {
                (None, 10, "SEMANTICS_INCONSISTENT"),
                ("input.cache_read", 11, "SEMANTICS_INCONSISTENT"),
                ("output", 2, "SEMANTICS_INCONSISTENT"),
            },
        )


if __name__ == "__main__":
    unittest.main()

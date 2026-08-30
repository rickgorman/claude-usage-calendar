import json
import unittest
from datetime import datetime
from pathlib import Path

from costs.adapters.openai import normalize
from costs.contracts import MeasurementQuality, RawUsageRecord, Unit

FIXTURE = (
    Path(__file__).parents[2]
    / "costs"
    / "fixtures"
    / "openai"
    / "semantic_cases_v1.json"
)


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def make_record(values):
    return RawUsageRecord(
        **{
            **values,
            "occurred_at": datetime.fromisoformat(values["occurred_at"]),
            "measurement_quality": MeasurementQuality(values["measurement_quality"]),
            "assumptions": tuple(values["assumptions"]),
        }
    )


class OpenAIAdapterFixtureTests(unittest.TestCase):
    def test_semantic_fixture_cases(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                result = normalize(make_record(case["record"]))
                self.assertEqual(
                    [(item.meter.value, item.quantity) for item in result.quantities],
                    [tuple(item) for item in case["billable"]],
                )
                self.assertEqual(
                    [
                        (
                            item.meter.value if item.meter is not None else None,
                            item.quantity,
                            item.reason.value,
                        )
                        for item in result.unpriced_quantities
                    ],
                    [tuple(item) for item in case["unpriced"]],
                )
                self.assertEqual(
                    [item.reason.value for item in result.diagnostics],
                    case["diagnostics"],
                )
                self.assertTrue(
                    all(item.unit is Unit.TOKENS for item in result.quantities)
                )
                self.assertTrue(
                    all(item.unit is Unit.TOKENS for item in result.unpriced_quantities)
                )

    def test_exact_dimensions_and_record_metadata_are_preserved(self):
        record = make_record(load_cases()[0]["record"])
        result = normalize(record)

        self.assertEqual(len(result.quantities), 3)
        self.assertEqual(len(result.unpriced_quantities), 1)
        for quantity in (*result.quantities, *result.unpriced_quantities):
            self.assertEqual(quantity.source_record_id, record.source_record_id)
            self.assertEqual(quantity.agent, record.agent)
            self.assertEqual(quantity.model, record.model)
            self.assertEqual(quantity.channel, record.channel)
            self.assertEqual(quantity.variant, record.variant)
            self.assertEqual(quantity.service_tier, record.service_tier)
            self.assertEqual(quantity.context_band, record.context_band)
            self.assertEqual(quantity.occurred_at, record.occurred_at)
            self.assertEqual(quantity.measurement_quality, record.measurement_quality)
            self.assertEqual(quantity.semantics_version, record.semantics_version)
            self.assertEqual(quantity.assumptions, record.assumptions)

    def test_negative_remainder_never_leaks_a_billable_quantity(self):
        case = next(
            case
            for case in load_cases()
            if case["name"] == "negative-inclusive-remainder-invalidates-whole-record"
        )
        result = normalize(make_record(case["record"]))

        self.assertEqual(result.quantities, ())
        self.assertEqual(
            sorted(item.quantity for item in result.unpriced_quantities),
            [2, 10, 11],
        )

    def test_reasoning_output_is_included_exactly_once(self):
        case = next(
            case
            for case in load_cases()
            if case["name"] == "reasoning-equal-to-output-is-not-double-counted"
        )
        result = normalize(make_record(case["record"]))

        self.assertEqual(sum(item.quantity for item in result.quantities), 37)
        self.assertEqual(
            [
                item.quantity
                for item in result.quantities
                if item.meter.value == "output"
            ],
            [12],
        )

    def test_missing_required_native_counters_are_malformed(self):
        cases = {
            case["name"]: case
            for case in load_cases()
            if case["name"].startswith("missing-required-")
        }

        self.assertEqual(
            set(cases),
            {
                "missing-required-input-counter-is-malformed",
                "missing-required-output-counter-is-malformed",
            },
        )
        for name, case in cases.items():
            with self.subTest(case=name):
                result = normalize(make_record(case["record"]))
                self.assertEqual(result.quantities, ())
                self.assertTrue(result.unpriced_quantities)
                self.assertEqual(
                    {item.reason.value for item in result.unpriced_quantities},
                    {"MALFORMED_SOURCE_RECORD"},
                )
                self.assertEqual(
                    [item.reason.value for item in result.diagnostics],
                    ["MALFORMED_SOURCE_RECORD"],
                )


if __name__ == "__main__":
    unittest.main()

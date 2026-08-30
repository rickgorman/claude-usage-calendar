import json
import unittest
from datetime import datetime
from pathlib import Path

from costs.adapters.cursor import normalize
from costs.contracts import (
    DiagnosticReason,
    MeasurementQuality,
    Meter,
    RawUsageRecord,
    Unit,
)

FIXTURE = (
    Path(__file__).parents[2]
    / "costs"
    / "fixtures"
    / "cursor"
    / "adapter_records_v1.json"
)


def records():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {
        item["source_record_id"]: RawUsageRecord(
            **{
                **item,
                "occurred_at": datetime.fromisoformat(item["occurred_at"]),
                "measurement_quality": MeasurementQuality(item["measurement_quality"]),
            }
        )
        for item in payload["records"]
    }


class CursorAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = records()

    def test_reported_standard_first_party_preserves_disjoint_meters_and_fee(self):
        result = normalize(self.records["cloud-standard-explicit-first-party"])

        self.assertEqual(
            [(item.meter, item.quantity) for item in result.quantities],
            [
                (Meter.INPUT_UNCACHED, 100),
                (Meter.INPUT_CACHE_READ, 20),
                (Meter.OUTPUT, 30),
            ],
        )
        self.assertTrue(
            all(
                item.measurement_quality is MeasurementQuality.PROVIDER_REPORTED
                and item.variant == "standard"
                and item.channel == "cursor-first-party"
                for item in result.quantities
            )
        )
        (cache_write,) = result.unpriced_quantities
        self.assertEqual(
            (
                cache_write.meter,
                cache_write.quantity,
                cache_write.unit,
                cache_write.reason,
            ),
            (None, 7, Unit.TOKENS, DiagnosticReason.UNSUPPORTED_METER),
        )
        self.assertTrue(
            any(
                assumption.startswith("Cursor reports cache writes without a TTL")
                for assumption in cache_write.assumptions
            )
        )
        self.assertIn(
            "chargedCents is observed total-charge metadata, not a v1 platform-fee meter",
            result.quantities[0].assumptions,
        )

    def test_auto_uses_exact_routed_model_and_keeps_fast_third_party_dimensions(self):
        result = normalize(self.records["cloud-fast-auto-third-party"])

        self.assertFalse(result.unpriced_quantities)
        self.assertEqual({item.model for item in result.quantities}, {"gpt-5.3-codex"})
        self.assertTrue(
            all(
                item.variant == "fast"
                and item.channel == "cursor-third-party"
                and item.service_tier == "third-party"
                and item.measurement_quality is MeasurementQuality.DERIVED
                for item in result.quantities
            )
        )

    def test_subscription_and_local_transcript_preserve_distinct_channels_and_quality(
        self,
    ):
        subscription = normalize(self.records["subscription-derived-explicit"])
        transcript = normalize(self.records["local-transcript-estimated"])

        self.assertEqual(
            {item.channel for item in subscription.quantities}, {"subscription-derived"}
        )
        self.assertTrue(
            all(
                item.measurement_quality is MeasurementQuality.DERIVED
                for item in subscription.quantities
            )
        )
        self.assertEqual(
            {item.channel for item in transcript.quantities}, {"local-transcript"}
        )
        self.assertTrue(
            all(
                item.measurement_quality is MeasurementQuality.ESTIMATED
                for item in transcript.quantities
            )
        )
        self.assertTrue(
            all(
                "local transcript token reconstruction; not provider-reported"
                in item.assumptions
                for item in transcript.quantities
            )
        )
        self.assertEqual(
            [
                (item.meter, item.quantity, item.reason)
                for item in transcript.unpriced_quantities
            ],
            [(None, 4, DiagnosticReason.UNSUPPORTED_METER)],
        )

    def test_auto_without_an_exact_route_remains_unpriced_including_cache(self):
        result = normalize(self.records["auto-without-routed-model"])

        self.assertFalse(result.quantities)
        self.assertEqual(
            [
                (item.meter, item.quantity, item.reason)
                for item in result.unpriced_quantities
            ],
            [
                (Meter.INPUT_UNCACHED, 18, DiagnosticReason.UNKNOWN_ROUTED_MODEL),
                (Meter.INPUT_CACHE_READ, 3, DiagnosticReason.UNKNOWN_ROUTED_MODEL),
                (Meter.OUTPUT, 6, DiagnosticReason.UNKNOWN_ROUTED_MODEL),
            ],
        )
        self.assertEqual(
            [item.reason for item in result.diagnostics],
            [DiagnosticReason.UNKNOWN_ROUTED_MODEL],
        )

    def test_malformed_counters_and_visible_tool_charge_are_unpriced(self):
        result = normalize(self.records["malformed-counter"])

        self.assertFalse(result.quantities)
        self.assertEqual(
            [
                (item.meter, item.quantity, item.unit, item.reason)
                for item in result.unpriced_quantities
            ],
            [
                (
                    Meter.INPUT_UNCACHED,
                    10,
                    Unit.TOKENS,
                    DiagnosticReason.MALFORMED_SOURCE_RECORD,
                ),
                (None, 1, Unit.COUNT, DiagnosticReason.MALFORMED_SOURCE_RECORD),
                (None, 1, Unit.COUNT, DiagnosticReason.MALFORMED_SOURCE_RECORD),
                (None, 2, Unit.REQUESTS, DiagnosticReason.UNSUPPORTED_CHARGE),
            ],
        )
        self.assertEqual(
            [item.reason for item in result.diagnostics],
            [
                DiagnosticReason.MALFORMED_SOURCE_RECORD,
                DiagnosticReason.INVALID_QUANTITY,
                DiagnosticReason.INVALID_QUANTITY,
            ],
        )

    def test_missing_required_input_or_output_unprices_every_visible_counter(self):
        cases = {
            "missing-input": [(Meter.INPUT_CACHE_READ, 4), (Meter.OUTPUT, 12)],
            "missing-output": [(Meter.INPUT_UNCACHED, 12), (Meter.INPUT_CACHE_READ, 4)],
        }

        for record_id, expected in cases.items():
            with self.subTest(record_id=record_id):
                result = normalize(self.records[record_id])

                self.assertFalse(result.quantities)
                self.assertEqual(
                    [
                        (item.meter, item.quantity, item.reason)
                        for item in result.unpriced_quantities[:-1]
                    ],
                    [
                        (meter, quantity, DiagnosticReason.MALFORMED_SOURCE_RECORD)
                        for meter, quantity in expected
                    ],
                )
                marker = result.unpriced_quantities[-1]
                self.assertEqual(
                    (marker.meter, marker.quantity, marker.unit, marker.reason),
                    (None, 1, Unit.COUNT, DiagnosticReason.MALFORMED_SOURCE_RECORD),
                )

    def test_outer_auto_requires_a_route_and_native_positive_fee_is_visible(self):
        result = normalize(self.records["auto-outer-model"])

        self.assertEqual(
            {item.model for item in result.quantities}, {"claude-sonnet-4"}
        )
        self.assertFalse(
            any(item.model.casefold() == "auto" for item in result.quantities)
        )
        self.assertEqual(
            [
                (item.meter, item.quantity, item.unit, item.reason)
                for item in result.unpriced_quantities
            ],
            [(None, 1, Unit.COUNT, DiagnosticReason.UNSUPPORTED_CHARGE)],
        )
        self.assertEqual(
            [item.reason for item in result.diagnostics],
            [DiagnosticReason.UNSUPPORTED_CHARGE],
        )

    def test_explicit_model_conflicting_with_routed_model_is_unpriced(self):
        result = normalize(self.records["explicit-routed-conflict"])

        self.assertFalse(result.quantities)
        self.assertEqual(
            [
                (item.model, item.meter, item.reason)
                for item in result.unpriced_quantities
            ],
            [
                (None, Meter.INPUT_UNCACHED, DiagnosticReason.SEMANTICS_INCONSISTENT),
                (None, Meter.OUTPUT, DiagnosticReason.SEMANTICS_INCONSISTENT),
            ],
        )

    def test_auto_is_not_an_exact_routed_model(self):
        result = normalize(self.records["auto-routed-to-auto"])

        self.assertFalse(result.quantities)
        self.assertEqual({item.model for item in result.unpriced_quantities}, {None})
        self.assertEqual(
            {item.reason for item in result.unpriced_quantities},
            {DiagnosticReason.UNKNOWN_ROUTED_MODEL},
        )

    def test_local_transcript_roles_allow_a_single_estimated_component(self):
        expected = {
            "local-transcript-input-role": [(Meter.INPUT_UNCACHED, 8)],
            "local-transcript-output-role": [(Meter.OUTPUT, 6)],
        }

        for record_id, quantities in expected.items():
            with self.subTest(record_id=record_id):
                result = normalize(self.records[record_id])

                self.assertEqual(
                    [(item.meter, item.quantity) for item in result.quantities],
                    quantities,
                )
                self.assertFalse(result.unpriced_quantities)

    def test_team_usage_is_native_and_charged_cents_or_zero_cache_write_do_not_block_it(
        self,
    ):
        result = normalize(self.records["team-usage-charged-cents-zero-cache-write"])

        self.assertEqual(
            [(item.meter, item.quantity) for item in result.quantities],
            [
                (Meter.INPUT_UNCACHED, 14),
                (Meter.INPUT_CACHE_READ, 2),
                (Meter.OUTPUT, 7),
            ],
        )
        self.assertFalse(result.unpriced_quantities)
        self.assertIn(
            "chargedCents is observed total-charge metadata, not a v1 platform-fee meter",
            result.quantities[0].assumptions,
        )

    def test_wrong_native_shape_preserves_visible_counters_as_unpriced(self):
        result = normalize(self.records["team-usage-wrong-run-shape"])

        self.assertFalse(result.quantities)
        self.assertEqual(
            [
                (item.meter, item.quantity, item.reason)
                for item in result.unpriced_quantities[:-1]
            ],
            [
                (Meter.INPUT_UNCACHED, 14, DiagnosticReason.MALFORMED_SOURCE_RECORD),
                (Meter.OUTPUT, 7, DiagnosticReason.MALFORMED_SOURCE_RECORD),
            ],
        )
        marker = result.unpriced_quantities[-1]
        self.assertEqual(
            (marker.meter, marker.quantity, marker.unit, marker.reason),
            (None, 1, Unit.COUNT, DiagnosticReason.MALFORMED_SOURCE_RECORD),
        )

    def test_zero_flattened_cache_write_does_not_create_an_unsupported_component(self):
        result = normalize(self.records["flattened-zero-cache-write"])

        self.assertEqual(
            [(item.meter, item.quantity) for item in result.quantities],
            [(Meter.INPUT_UNCACHED, 14), (Meter.OUTPUT, 7)],
        )
        self.assertFalse(result.unpriced_quantities)


if __name__ == "__main__":
    unittest.main()

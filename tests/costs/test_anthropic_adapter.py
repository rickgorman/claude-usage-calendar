import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from costs.adapters.anthropic import normalize
from costs.contracts import (
    MeasurementQuality,
    RawUsageRecord,
)

FIXTURE = (
    Path(__file__).parents[2] / "costs" / "fixtures" / "anthropic" / "usage-v1.json"
)


class AnthropicAdapterTests(unittest.TestCase):
    def make_record(self, source, **changes):
        values = {
            "source_record_id": "anthropic-record",
            "agent": "claude",
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
            "channel": "api",
            "variant": "standard",
            "service_tier": "default",
            "context_band": "standard",
            "occurred_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
            "measurement_quality": MeasurementQuality.PROVIDER_REPORTED,
            "semantics_version": "anthropic.v1",
            "source": source,
        }
        values.update(changes)
        return RawUsageRecord(**values)

    def test_frozen_usage_fixtures(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for example in fixture["records"]:
            with self.subTest(example=example["name"]):
                record = self.make_record(
                    example["source"], **example.get("record", {})
                )
                result = normalize(record)
                self.assertEqual(
                    [(item.meter.value, item.quantity) for item in result.quantities],
                    [tuple(item) for item in example["expected_billable"]],
                )
                self.assertEqual(
                    [
                        (
                            item.meter.value if item.meter else None,
                            item.quantity,
                            item.reason.value,
                        )
                        for item in result.unpriced_quantities
                    ],
                    [tuple(item) for item in example["expected_unpriced"]],
                )
                self.assertEqual(
                    [item.reason.value for item in result.diagnostics],
                    example["expected_diagnostics"],
                )
                for quantity in (*result.quantities, *result.unpriced_quantities):
                    self.assertEqual(quantity.occurred_at, record.occurred_at)
                    self.assertEqual(
                        quantity.measurement_quality, record.measurement_quality
                    )
                    self.assertEqual(
                        quantity.semantics_version, record.semantics_version
                    )
                    self.assertEqual(quantity.assumptions, record.assumptions)

    def test_raw_record_remains_immutable(self):
        record = self.make_record(
            {"input_tokens": 10, "output_tokens": 2},
            assumptions=("frozen fixture assumption",),
        )
        original_source = record.source
        result = normalize(record)
        self.assertIs(record.source, original_source)
        self.assertEqual(record.source["input_tokens"], 10)
        self.assertEqual(
            [item.assumptions for item in result.quantities],
            [("frozen fixture assumption",), ("frozen fixture assumption",)],
        )


if __name__ == "__main__":
    unittest.main()

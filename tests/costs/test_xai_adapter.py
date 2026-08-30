import json
import unittest
from datetime import datetime
from pathlib import Path

from costs.adapters.xai import MODEL_LONG_CONTEXT_THRESHOLDS, normalize
from costs.contracts import (
    DiagnosticReason,
    MeasurementQuality,
    Provider,
    RawUsageRecord,
)

FIXTURE = (
    Path(__file__).parents[2] / "costs" / "fixtures" / "xai" / "semantic_cases_v1.json"
)


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def raw_record(payload):
    payload = dict(payload)
    payload["provider"] = (
        Provider(payload["provider"]) if payload["provider"] is not None else None
    )
    payload["measurement_quality"] = MeasurementQuality(payload["measurement_quality"])
    payload["occurred_at"] = datetime.fromisoformat(payload["occurred_at"])
    return RawUsageRecord(**payload)


class XaiAdapterTests(unittest.TestCase):
    def grok_cli_record(self, **overrides):
        values = {
            "source_record_id": "grok:cli-prompt:grok-4.6-build",
            "agent": "grok",
            "provider": Provider.XAI,
            "model": "grok-4.6-build",
            "channel": "subscription",
            "variant": "standard",
            "service_tier": "default",
            "context_band": "standard",
            "occurred_at": datetime.fromisoformat("2026-08-29T12:00:00+00:00"),
            "measurement_quality": MeasurementQuality.DERIVED,
            "semantics_version": "xai.grok-cli.api-equivalent.v1",
            "assumptions": ("standard-context API-equivalent estimate",),
            "source": {
                "inputTokens": 100,
                "cachedReadTokens": 20,
                "cacheCreationTokens": 0,
                "outputTokens": 5,
            },
        }
        values.update(overrides)
        return RawUsageRecord(**values)

    def test_grok_cli_api_equivalent_derives_uncached_and_fixed_dimensions(self):
        result = normalize(self.grok_cli_record())

        self.assertEqual(result.unpriced_quantities, ())
        self.assertEqual(
            {item.meter.value: item.quantity for item in result.quantities},
            {"input.uncached": 80, "input.cache_read": 20, "output": 5},
        )
        self.assertEqual({item.context_band for item in result.quantities}, {"standard"})
        self.assertEqual(
            {item.measurement_quality for item in result.quantities},
            {MeasurementQuality.DERIVED},
        )

    def test_grok_cli_cache_creation_stays_visible_and_unpriced(self):
        result = normalize(
            self.grok_cli_record(
                source={
                    "inputTokens": 100,
                    "cachedReadTokens": 20,
                    "cacheCreationTokens": 10,
                    "outputTokens": 5,
                }
            )
        )

        self.assertEqual(
            {item.meter.value: item.quantity for item in result.quantities},
            {"input.uncached": 70, "input.cache_read": 20, "output": 5},
        )
        self.assertEqual(len(result.unpriced_quantities), 1)
        self.assertEqual(result.unpriced_quantities[0].quantity, 10)
        self.assertEqual(
            result.unpriced_quantities[0].reason,
            DiagnosticReason.UNSUPPORTED_METER,
        )

    def test_grok_cli_api_equivalent_requires_an_exact_model(self):
        result = normalize(self.grok_cli_record(model=None))

        self.assertEqual(result.quantities, ())
        self.assertEqual(
            {item.reason for item in result.unpriced_quantities},
            {DiagnosticReason.MISSING_MODEL},
        )

    def test_fixture_cases_preserve_request_dimensions_and_meters(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                result = normalize(raw_record(case["record"]))
                expected = case["expected"]
                if "reason" in expected:
                    self.assertEqual(result.quantities, ())
                    self.assertEqual(
                        result.diagnostics[0].reason.value, expected["reason"]
                    )
                    if "reasons" not in expected:
                        self.assertTrue(
                            all(
                                quantity.reason.value == expected["reason"]
                                for quantity in result.unpriced_quantities
                            )
                        )
                    quantities = result.unpriced_quantities
                else:
                    self.assertEqual(result.unpriced_quantities, ())
                    self.assertEqual(result.diagnostics, ())
                    quantities = result.quantities
                    self.assertTrue(
                        all(
                            quantity.context_band == expected["context_band"]
                            for quantity in quantities
                        )
                    )

                if "meters" in expected:
                    self.assertEqual(
                        [
                            (
                                item.meter.value if item.meter is not None else None,
                                item.quantity,
                            )
                            for item in quantities
                        ],
                        [tuple(item) for item in expected["meters"]],
                    )
                if "reasons" in expected:
                    self.assertEqual(
                        {item.reason.value for item in quantities},
                        set(expected["reasons"]),
                    )
                if "units" in expected:
                    self.assertEqual(
                        [item.unit.value for item in quantities], expected["units"]
                    )

    def test_cached_and_uncached_quantities_share_the_pre_split_band(self):
        standard, long_context = [
            raw_record(case["record"])
            for case in load_cases()
            if case["name"] in {"chat_completions_cached", "responses_cached"}
        ]
        standard_result = normalize(standard)
        long_result = normalize(long_context)

        self.assertEqual(
            {item.context_band for item in standard_result.quantities}, {"standard"}
        )
        self.assertEqual(
            {item.context_band for item in long_result.quantities}, {"long"}
        )
        self.assertEqual(long_result.quantities[0].model, "grok-test-2")
        self.assertEqual(long_result.quantities[0].service_tier, "priority")

    def test_threshold_selects_long_context_at_and_above_the_exact_model_value(self):
        cases = {
            case["name"]: raw_record(case["record"])
            for case in load_cases()
            if case["name"]
            in {"threshold_minus_one", "threshold", "threshold_plus_one"}
        }

        self.assertEqual(
            {
                item.context_band
                for item in normalize(cases["threshold_minus_one"]).quantities
            },
            {"standard"},
        )
        self.assertEqual(
            {item.context_band for item in normalize(cases["threshold"]).quantities},
            {"long"},
        )
        self.assertEqual(
            {
                item.context_band
                for item in normalize(cases["threshold_plus_one"]).quantities
            },
            {"long"},
        )
        self.assertEqual(MODEL_LONG_CONTEXT_THRESHOLDS["grok-4.6"], 200_000)
        with self.assertRaises(TypeError):
            MODEL_LONG_CONTEXT_THRESHOLDS["grok-4.6"] = 1

    def test_negative_uncached_remainder_does_not_select_a_band(self):
        record = raw_record(
            next(
                case["record"]
                for case in load_cases()
                if case["name"] == "negative_cached_remainder"
            )
        )
        result = normalize(record)

        self.assertEqual(result.quantities, ())
        self.assertEqual(
            result.diagnostics[0].reason, DiagnosticReason.SEMANTICS_INCONSISTENT
        )
        self.assertTrue(
            all(item.context_band is None for item in result.unpriced_quantities)
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from argparse import ArgumentParser
from contextlib import redirect_stderr
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from costs.cli import (
    CostCliConfig,
    CostCliError,
    configure_cost_cli,
    resolve_cost_cli,
)
from costs.contracts import MeasurementQuality, Meter, Provider, RateUnit
from costs.pricing import (
    BUILTIN_PRICING_PACK_PATH,
    MAX_JSON_DEPTH,
    MAX_PRICING_PACK_BYTES,
    MAX_RATES,
    PricingLoadOptions,
    PricingLoadResult,
    PricingPackError,
    PricingPackKind,
    load_pricing_pack,
)

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "costs" / "fixtures" / "pricing"
VALID = FIXTURES / "valid-minimal-v1.json"


def valid_payload() -> dict[str, object]:
    return json.loads(VALID.read_text(encoding="utf-8"))


class PricingLoaderTests(unittest.TestCase):
    def load_payload(self, payload: object) -> PricingLoadResult:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pricing.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_pricing_pack(PricingLoadOptions(path=path))

    def assert_rejected(self, payload: object) -> None:
        with self.assertRaises(PricingPackError):
            self.load_payload(payload)

    def test_valid_pack_has_exact_typed_immutable_result_and_exact_byte_hash(self):
        raw = VALID.read_bytes()
        result = load_pricing_pack(PricingLoadOptions(path=VALID))

        self.assertIsInstance(result, PricingLoadResult)
        self.assertEqual(result.pack.schema_version, 1)
        self.assertEqual(result.pack.currency, "USD")
        self.assertEqual(result.pack.valuation_as_of, date(2026, 8, 28))
        self.assertIsInstance(result.pack.rates, tuple)
        self.assertEqual(len(result.pack.rates), 1)
        rate = result.pack.rates[0]
        self.assertEqual(rate.key.provider, Provider.OPENAI)
        self.assertEqual(rate.key.meter, Meter.INPUT_UNCACHED)
        self.assertEqual(
            rate.key.measurement_quality, MeasurementQuality.PROVIDER_REPORTED
        )
        self.assertEqual(rate.unit, RateUnit.MILLION_TOKENS)
        self.assertEqual(rate.price, Decimal("1.234567890123"))
        self.assertNotIsInstance(rate.price, float)
        self.assertEqual(result.pack.provenance.kind, PricingPackKind.CUSTOM)
        self.assertEqual(result.pack.provenance.sha256, hashlib.sha256(raw).hexdigest())
        self.assertFalse(hasattr(result.pack.provenance, "path"))
        with self.assertRaises(FrozenInstanceError):
            result.pack.display_name = "changed"  # type: ignore[misc]

    def test_builtin_selection_assigns_kind_in_application(self):
        with patch("costs.pricing.BUILTIN_PRICING_PACK_PATH", VALID):
            result = load_pricing_pack(PricingLoadOptions())
        self.assertEqual(result.pack.provenance.kind, PricingPackKind.BUILTIN)

    def test_locked_builtin_pack_location_loads_without_special_handling(self):
        self.assertEqual(
            BUILTIN_PRICING_PACK_PATH,
            ROOT / "costs" / "pricing_packs" / "builtin-2026-08-29.json",
        )
        result = load_pricing_pack(PricingLoadOptions())
        self.assertEqual(result.pack.provenance.kind, PricingPackKind.BUILTIN)
        self.assertGreater(len(result.pack.rates), 0)

    def test_loader_reads_selected_pack_exactly_once(self):
        original_open = Path.open
        reads = 0

        def tracking_open(path: Path, *args, **kwargs):
            nonlocal reads
            if path == VALID:
                reads += 1
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", autospec=True, side_effect=tracking_open):
            load_pricing_pack(PricingLoadOptions(path=VALID))
        self.assertEqual(reads, 1)

    def test_custom_pack_replaces_builtin_even_when_builtin_is_enabled(self):
        with patch(
            "costs.pricing.BUILTIN_PRICING_PACK_PATH",
            Path("/path/that/must/not/be/read"),
        ):
            result = load_pricing_pack(PricingLoadOptions(path=VALID, use_builtin=True))
        self.assertEqual(result.pack.provenance.kind, PricingPackKind.CUSTOM)

    def test_disabled_loading_and_unreadable_inputs_fail_closed(self):
        with self.assertRaises(PricingPackError):
            load_pricing_pack(PricingLoadOptions(use_builtin=False))
        with self.assertRaises(PricingPackError):
            load_pricing_pack(PricingLoadOptions(path=Path("missing-pack.json")))

    def test_duplicate_names_and_prototype_keys_are_rejected_from_raw_fixtures(self):
        for name in (
            "invalid-duplicate-name-v1.json",
            "invalid-prototype-key-v1.json",
        ):
            with self.subTest(name=name), self.assertRaises(PricingPackError):
                load_pricing_pack(PricingLoadOptions(path=FIXTURES / name))

    def test_unknown_fields_version_currency_provider_unit_and_quality_are_rejected(
        self,
    ):
        mutations = []
        unknown_top = valid_payload()
        unknown_top["expression"] = "price * usage"
        mutations.append(unknown_top)
        for field, value in (("schema_version", 2), ("currency", "EUR")):
            payload = valid_payload()
            payload[field] = value
            mutations.append(payload)
        for field, value in (
            ("provider", "future-provider"),
            ("unit", "token"),
            ("measurement_quality", "mixed"),
            ("measurement_quality", []),
        ):
            payload = valid_payload()
            payload["rates"][0][field] = value  # type: ignore[index]
            mutations.append(payload)
        unknown_rate = valid_payload()
        unknown_rate["rates"][0]["fallback"] = True  # type: ignore[index]
        mutations.append(unknown_rate)
        unknown_source = valid_payload()
        unknown_source["rates"][0]["source"]["notes"] = "ignored?"  # type: ignore[index]
        mutations.append(unknown_source)
        for payload in mutations:
            self.assert_rejected(payload)

    def test_numeric_negative_exponent_and_excess_precision_prices_are_rejected(self):
        invalid_prices: tuple[object, ...] = (
            1,
            1.5,
            -1,
            "-1",
            "+1",
            "01",
            ".1",
            "1.",
            "1e3",
            "1.0000000000000",
            "9" * 65,
        )
        for price in invalid_prices:
            with self.subTest(price=price):
                payload = valid_payload()
                payload["rates"][0]["price"] = price  # type: ignore[index]
                self.assert_rejected(payload)

    def test_pattern_empty_oversized_and_prototype_identifiers_are_rejected(self):
        invalid_identifiers = (
            "",
            "*",
            "model?",
            "[model]",
            "(model|other)",
            "^model$",
            "model\\d+",
            "__proto__",
            "prototype",
            "constructor",
            "x" * 257,
        )
        for identifier in invalid_identifiers:
            with self.subTest(identifier=identifier):
                payload = valid_payload()
                payload["rates"][0]["model"] = identifier  # type: ignore[index]
                self.assert_rejected(payload)

    def test_duplicate_rate_keys_are_rejected_without_fallback_or_merge(self):
        payload = valid_payload()
        payload["rates"].append(copy.deepcopy(payload["rates"][0]))  # type: ignore[union-attr,index]
        self.assert_rejected(payload)

    def test_all_rate_dates_must_equal_the_single_pack_snapshot_date(self):
        accepted = load_pricing_pack(
            PricingLoadOptions(path=FIXTURES / "valid-snapshot-date-v1.json")
        ).pack
        self.assertEqual(accepted.valuation_as_of, date(2026, 8, 28))
        self.assertEqual(
            {rate.key.valuation_date for rate in accepted.rates},
            {accepted.valuation_as_of},
        )

        for name in (
            "invalid-stale-valuation-date-v1.json",
            "invalid-mixed-valuation-dates-v1.json",
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(PricingPackError, "valuation_date must equal"),
            ):
                load_pricing_pack(PricingLoadOptions(path=FIXTURES / name))

    def test_invalid_dates_labels_urls_and_nested_shapes_are_rejected(self):
        mutations = []
        for path, value in (
            (("valuation_as_of",), "2026-8-28"),
            (("display_name",), ""),
            (("rates", 0, "valuation_date"), "2026-02-30"),
            (("rates", 0, "source", "checked_at"), "yesterday"),
            (("rates", 0, "source", "url"), "relative/path"),
            (("rates", 0, "source"), []),
        ):
            payload = valid_payload()
            target = payload
            for part in path[:-1]:
                target = target[part]  # type: ignore[index,assignment]
            target[path[-1]] = value  # type: ignore[index]
            mutations.append(payload)
        for payload in mutations:
            self.assert_rejected(payload)

    def test_non_utf8_oversized_deep_and_excess_cardinality_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cases = {
                "non-utf8.json": b"\xff\xfe",
                "oversized.json": b" " * (MAX_PRICING_PACK_BYTES + 1),
                "deep.json": (
                    "[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1)
                ).encode(),
                "many-rates.json": json.dumps(
                    {
                        "schema_version": 1,
                        "pack_id": "many",
                        "display_name": "Many",
                        "currency": "USD",
                        "valuation_as_of": "2026-08-28",
                        "rates": [{}] * (MAX_RATES + 1),
                    }
                ).encode(),
            }
            for name, raw in cases.items():
                with self.subTest(name=name):
                    path = base / name
                    path.write_bytes(raw)
                    with self.assertRaises(PricingPackError):
                        load_pricing_pack(PricingLoadOptions(path=path))


class CostCliTests(unittest.TestCase):
    def parse(self, *arguments: str) -> CostCliConfig:
        parser = ArgumentParser()
        configure_cost_cli(parser)
        return resolve_cost_cli(parser.parse_args(arguments))

    def test_default_and_explicit_disable_are_frozen_disabled_configs(self):
        for arguments in ((), ("--no-costs",)):
            config = self.parse(*arguments)
            self.assertEqual(config, CostCliConfig())
            with self.assertRaises(FrozenInstanceError):
                config.enabled = True  # type: ignore[misc]

    def test_each_positive_control_enables_costs_and_resolves_types(self):
        self.assertTrue(self.parse("--costs").enabled)
        pricing = self.parse("--pricing", "custom.json")
        self.assertTrue(pricing.enabled)
        self.assertEqual(pricing.pricing_path, Path("custom.json"))
        complete = self.parse("--require-complete-pricing")
        self.assertTrue(complete.enabled)
        self.assertTrue(complete.require_complete_pricing)
        explain = self.parse("--explain-costs")
        self.assertTrue(explain.enabled)
        self.assertTrue(explain.explain_costs)

    def test_costs_can_be_combined_with_custom_and_behavior_flags(self):
        config = self.parse(
            "--costs",
            "--pricing",
            "custom.json",
            "--require-complete-pricing",
            "--explain-costs",
        )
        self.assertEqual(
            config,
            CostCliConfig(
                enabled=True,
                pricing_path=Path("custom.json"),
                require_complete_pricing=True,
                explain_costs=True,
            ),
        )

    def test_no_costs_conflicts_with_every_implying_flag_regardless_of_order(self):
        cases = (
            ("--no-costs", "--pricing", "custom.json"),
            ("--pricing", "custom.json", "--no-costs"),
            ("--no-costs", "--require-complete-pricing"),
            ("--explain-costs", "--no-costs"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(CostCliError):
                self.parse(*arguments)

    def test_costs_and_no_costs_are_argparse_mutually_exclusive(self):
        parser = ArgumentParser()
        configure_cost_cli(parser)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(("--costs", "--no-costs"))


if __name__ == "__main__":
    unittest.main()

import copy
import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import typing
import unittest
from argparse import ArgumentParser
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from costs import (
    COSTS_ENABLED_BY_DEFAULT,
    TOKENS_PER_MILLION,
    BillableQuantity,
    ContractViolation,
    CostStatus,
    Diagnostic,
    DiagnosticReason,
    MeasurementQuality,
    Meter,
    NormalizationResult,
    Provider,
    RateKey,
    RateUnit,
    RawUsageRecord,
    Unit,
    UnpricedQuantity,
    validate_cost_estimates,
)
from costs.registry import ADAPTER_SLOT_NAMES, ADAPTER_SLOTS

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "costs" / "schemas"
FIXTURES = ROOT / "costs" / "fixtures"
SCRIPT = ROOT / "claude-usage-calendar.py"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def schema_accepts(schema_path, payload):
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
            fixture_path = Path(directory) / "fixture.json"
            fixture_path.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(
                [*prefix, "--schemafile", str(schema_path), str(fixture_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        return result.returncode == 0
    schema = load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    return not list(Draft202012Validator(schema).iter_errors(payload))


class FrozenContractTests(unittest.TestCase):
    def make_raw(self, **changes):
        values = {
            "source_record_id": "record-1",
            "agent": "codex",
            "provider": Provider.OPENAI,
            "model": "exact-model",
            "channel": "api",
            "variant": "standard",
            "service_tier": "default",
            "context_band": "standard",
            "occurred_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "measurement_quality": MeasurementQuality.PROVIDER_REPORTED,
            "semantics_version": "openai.v1",
            "source": {"usage": {"input_tokens": 10}, "parts": [1, 2]},
        }
        values.update(changes)
        return RawUsageRecord(**values)

    def test_costs_default_off_and_frozen_enums(self):
        self.assertFalse(COSTS_ENABLED_BY_DEFAULT)
        self.assertEqual(
            [item.value for item in Meter],
            [
                "input.uncached",
                "input.cache_read",
                "input.cache_write_5m",
                "input.cache_write_1h",
                "output",
            ],
        )
        self.assertEqual([item.value for item in Unit], ["tokens", "requests", "count"])
        self.assertEqual(RateUnit.MILLION_TOKENS.value, "million_tokens")
        self.assertEqual(TOKENS_PER_MILLION, 1_000_000)
        self.assertEqual(
            [item.value for item in CostStatus],
            ["complete", "partial", "unpriced", "disabled"],
        )
        self.assertEqual(
            [item.value for item in MeasurementQuality],
            ["provider_reported", "derived", "estimated", "mixed"],
        )
        self.assertIn(
            "SEMANTICS_INCONSISTENT",
            {item.value for item in DiagnosticReason},
        )

    def test_rate_key_fields_are_exact_and_complete(self):
        self.assertEqual(
            [field.name for field in fields(RateKey)],
            [
                "provider",
                "model",
                "channel",
                "variant",
                "service_tier",
                "context_band",
                "meter",
                "valuation_date",
                "measurement_quality",
            ],
        )
        key = RateKey(
            Provider.OPENAI,
            "exact-model",
            "api",
            "standard",
            "default",
            "standard",
            Meter.INPUT_UNCACHED,
            date(2026, 8, 1),
            MeasurementQuality.PROVIDER_REPORTED,
        )
        self.assertIsInstance(hash(key), int)
        with self.assertRaises(ValueError):
            RateKey(
                Provider.OPENAI,
                "",
                "api",
                "standard",
                "default",
                "standard",
                Meter.INPUT_UNCACHED,
                date(2026, 8, 1),
                MeasurementQuality.PROVIDER_REPORTED,
            )

    def test_raw_record_is_deeply_immutable_and_lossless(self):
        record = self.make_raw()
        self.assertEqual(record.source["usage"]["input_tokens"], 10)
        self.assertEqual(record.source["parts"], (1, 2))
        with self.assertRaises(TypeError):
            record.source["new"] = "value"
        with self.assertRaises(TypeError):
            record.source["usage"]["input_tokens"] = 11
        with self.assertRaises(FrozenInstanceError):
            record.model = "different"
        with self.assertRaises(ValueError):
            self.make_raw(measurement_quality=MeasurementQuality.MIXED)
        with self.assertRaises(ValueError):
            self.make_raw(occurred_at=datetime(2026, 8, 1, tzinfo=None))  # noqa: DTZ001
        unknown = self.make_raw(provider="future_provider")
        self.assertEqual(unknown.provider, "future_provider")
        for reserved in ("__proto__", "prototype", "constructor"):
            with self.assertRaises(ValueError):
                self.make_raw(source={reserved: 1})
            with self.assertRaises(ValueError):
                self.make_raw(agent=reserved)

    def test_billable_quantity_rejects_negative_noninteger_and_mixed(self):
        common = {
            "source_record_id": "record-1",
            "agent": "codex",
            "provider": Provider.OPENAI,
            "model": "exact-model",
            "channel": "api",
            "variant": "standard",
            "service_tier": "default",
            "context_band": "standard",
            "occurred_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "meter": Meter.OUTPUT,
            "unit": Unit.TOKENS,
            "measurement_quality": MeasurementQuality.DERIVED,
            "semantics_version": "openai.v1",
        }
        self.assertEqual(BillableQuantity(quantity=0, **common).quantity, 0)
        with self.assertRaises(ValueError):
            BillableQuantity(quantity=-1, **common)
        with self.assertRaises(TypeError):
            BillableQuantity(quantity=True, **common)
        with self.assertRaises(ValueError):
            BillableQuantity(
                quantity=1,
                **{**common, "measurement_quality": MeasurementQuality.MIXED},
            )
        with self.assertRaises(TypeError):
            BillableQuantity(quantity=1, **{**common, "provider": "future_provider"})
        with self.assertRaises(ValueError):
            BillableQuantity(quantity=1, **{**common, "unit": Unit.REQUESTS})

    def test_unpriced_quantity_preserves_unknowns_and_diagnostic_units(self):
        quantity = UnpricedQuantity(
            source_record_id="future-record",
            agent="mystery",
            provider="future_provider",
            model="future-model",
            channel=None,
            variant=None,
            service_tier=None,
            context_band=None,
            occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            meter=None,
            quantity=1,
            unit=Unit.REQUESTS,
            measurement_quality=MeasurementQuality.PROVIDER_REPORTED,
            semantics_version="future.v1",
            reason=DiagnosticReason.UNKNOWN_PROVIDER,
        )
        self.assertEqual(quantity.provider, "future_provider")
        self.assertIsNone(quantity.meter)
        self.assertEqual(quantity.unit, Unit.REQUESTS)
        result = NormalizationResult(unpriced_quantities=(quantity,))
        self.assertEqual(result.unpriced_quantities, (quantity,))
        with self.assertRaises(ValueError):
            UnpricedQuantity(
                **{
                    **{
                        field.name: getattr(quantity, field.name)
                        for field in fields(quantity)
                    },
                    "quantity": -1,
                }
            )

    def test_registry_centrally_registers_only_the_four_named_adapters(self):
        self.assertEqual(ADAPTER_SLOT_NAMES, ("anthropic", "openai", "xai", "cursor"))
        self.assertEqual(set(ADAPTER_SLOTS), set(ADAPTER_SLOT_NAMES))
        self.assertTrue(all(callable(adapter) for adapter in ADAPTER_SLOTS.values()))

    def test_adapter_boundaries_expose_stable_normalizer_callables(self):
        from costs.adapters import anthropic, cursor, openai, xai

        for adapter in (anthropic, openai, xai, cursor):
            self.assertTrue(callable(adapter.normalize))

    def test_disjoint_workstream_modules_expose_stable_callables(self):
        from costs import cli, dashboard, engine, pricing, serializer

        for function in (
            pricing.load_pricing_pack,
            cli.configure_cost_cli,
            engine.price_quantities,
            dashboard.render_cost_dashboard,
            serializer.serialize_cost_estimates,
        ):
            self.assertTrue(callable(function))

    def test_scaffold_signatures_and_result_types_are_frozen(self):
        from costs import cli, dashboard, engine, pricing, serializer

        resolved_json_object = typing.get_type_hints(
            serializer.serialize_cost_estimates
        )["return"]
        self.assertIs(typing.get_origin(resolved_json_object), MappingABC)
        self.assertEqual(
            serializer.serialize_cost_estimates.__annotations__["return"], "JsonObject"
        )
        self.assertEqual(
            dashboard.render_cost_dashboard.__annotations__["cost_estimates"],
            "JsonObject",
        )
        signatures = {
            pricing.load_pricing_pack: {
                "options": pricing.PricingLoadOptions,
                "return": pricing.PricingLoadResult,
            },
            engine.price_quantities: {
                "quantities": SequenceABC[BillableQuantity],
                "unpriced_quantities": SequenceABC[UnpricedQuantity],
                "pricing_pack": pricing.PricingPack,
                "diagnostics": SequenceABC[Diagnostic],
                "return": engine.CostResult,
            },
            serializer.serialize_cost_estimates: {
                "result": engine.CostResult,
                "return": resolved_json_object,
            },
            dashboard.render_cost_dashboard: {
                "result": engine.CostResult,
                "cost_estimates": resolved_json_object,
                "return": dashboard.DashboardOutput,
            },
            cli.configure_cost_cli: {
                "parser": ArgumentParser,
                "return": type(None),
            },
        }
        for function, expected_hints in signatures.items():
            with self.subTest(function=function.__name__):
                signature = inspect.signature(function)
                hints = typing.get_type_hints(function)
                self.assertEqual(
                    list(signature.parameters),
                    [name for name in expected_hints if name != "return"],
                )
                self.assertEqual(hints, expected_hints)

        frozen_types = (
            cli.CostCliConfig,
            pricing.PricingSource,
            pricing.RateCard,
            pricing.PricingProvenance,
            pricing.PricingPack,
            pricing.PricingLoadOptions,
            pricing.PricingLoadResult,
            engine.CostRollup,
            engine.PricedComponent,
            engine.DailyCostRollup,
            engine.AgentCostRollup,
            engine.CostResult,
            dashboard.SafeHtmlFragment,
            dashboard.DashboardOutput,
        )
        for contract_type in frozen_types:
            with self.subTest(contract_type=contract_type.__name__):
                self.assertTrue(contract_type.__dataclass_params__.frozen)
                self.assertTrue(hasattr(contract_type, "__slots__"))
        for module_name in (
            "costs.cli",
            "costs.pricing",
            "costs.engine",
            "costs.serializer",
            "costs.dashboard",
        ):
            with self.subTest(import_module=module_name):
                subprocess.run(
                    [sys.executable, "-c", f"import {module_name}"],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )


class FrozenArtifactTests(unittest.TestCase):
    def test_schema_documents_are_strict_and_versioned(self):
        pricing = load_json(SCHEMAS / "pricing-pack-v1.schema.json")
        costs = load_json(SCHEMAS / "cost-estimates-v1.schema.json")
        self.assertFalse(pricing["additionalProperties"])
        self.assertFalse(costs["additionalProperties"])
        self.assertEqual(pricing["properties"]["schema_version"]["const"], 1)
        self.assertEqual(costs["properties"]["schema_version"]["const"], 1)
        self.assertEqual(pricing["properties"]["currency"]["const"], "USD")
        self.assertEqual(costs["properties"]["currency"]["const"], "USD")
        self.assertEqual(
            set(costs["$defs"]["reason"]["enum"]),
            {reason.value for reason in DiagnosticReason},
        )
        self.assertEqual(
            costs["$defs"]["money"]["pattern"],
            "^(0|[1-9][0-9]*)\\.[0-9]{6}$",
        )
        rate = pricing["$defs"]["rate"]
        self.assertEqual(rate["properties"]["unit"]["const"], "million_tokens")
        self.assertIn("price", rate["required"])
        self.assertNotIn("unit_price_usd", rate["properties"])
        self.assertIn("cost_array_invariants", costs["$defs"])
        self.assertIn("occurred_on", costs["$defs"]["dimensions"]["required"])

    def test_synthetic_fixtures_freeze_semantic_edge_cases(self):
        backend = load_json(FIXTURES / "synthetic_backend_v1.json")
        expected = {
            item["source_record_id"]: item for item in backend["expected_normalization"]
        }
        self.assertIsNone(expected["anthropic-unknown-ttl"]["unpriced"][0][0])
        self.assertTrue(expected["openai-negative-remainder"]["whole_record_unpriced"])
        self.assertEqual(expected["openai-negative-remainder"]["billable"], [])
        self.assertEqual(
            expected["xai-standard-boundary"]["selected_context_band"], "standard"
        )
        self.assertEqual(expected["xai-long-boundary"]["selected_context_band"], "long")
        self.assertEqual(backend["xai_long_context_threshold_tokens"], 128000)

        ui = load_json(FIXTURES / "synthetic_ui_v1.json")
        self.assertEqual(ui["status"], "partial")
        self.assertIsNone(ui["total_usd"])
        self.assertRegex(ui["priced_subtotal_usd"], r"^\d+\.\d{6}$")
        unknown_ttl = next(
            item
            for item in ui["unpriced_components"]
            if item["reason"] == "UNKNOWN_CACHE_TTL"
        )
        self.assertEqual(unknown_ttl["reason"], "UNKNOWN_CACHE_TTL")
        self.assertIsNone(unknown_ttl["meter"])
        self.assertIn("</script>", json.dumps(ui))

        pack = load_json(FIXTURES / "synthetic_pricing_pack_v1.json")
        self.assertIn("TEST ONLY", pack["display_name"])
        self.assertEqual(
            pack["rates"][0]["source"]["url"].split("/")[2], "example.invalid"
        )
        self.assertTrue(all(rate["unit"] == "million_tokens" for rate in pack["rates"]))

    def test_json_schemas_validate_frozen_fixtures_without_optional_skip(self):
        self.assertTrue(
            schema_accepts(
                SCHEMAS / "pricing-pack-v1.schema.json",
                load_json(FIXTURES / "synthetic_pricing_pack_v1.json"),
            )
        )
        reserved_pack = load_json(FIXTURES / "synthetic_pricing_pack_v1.json")
        reserved_pack["pack_id"] = "constructor"
        self.assertFalse(
            schema_accepts(SCHEMAS / "pricing-pack-v1.schema.json", reserved_pack)
        )
        self.assertTrue(
            schema_accepts(
                SCHEMAS / "cost-estimates-v1.schema.json",
                load_json(FIXTURES / "synthetic_ui_v1.json"),
            )
        )

    def test_fixture_chain_rates_costs_normalization_and_coverage_reconcile(self):
        backend = load_json(FIXTURES / "synthetic_backend_v1.json")
        pack = load_json(FIXTURES / "synthetic_pricing_pack_v1.json")
        ui = load_json(FIXTURES / "synthetic_ui_v1.json")
        expected = {
            item["source_record_id"]: item for item in backend["expected_normalization"]
        }

        rate_fields = (
            "provider",
            "model",
            "channel",
            "variant",
            "service_tier",
            "context_band",
            "meter",
            "measurement_quality",
        )
        rates = {
            (*tuple(rate[field] for field in rate_fields), rate["valuation_date"]): rate
            for rate in pack["rates"]
        }
        self.assertEqual(len(rates), len(pack["rates"]))

        exact_calculated = Decimal(0)
        for component in ui["components"]:
            key = (
                *tuple(component[field] for field in rate_fields),
                ui["valuation_as_of"],
            )
            self.assertIn(key, rates)
            rate = rates[key]
            self.assertEqual(component["price"], rate["price"])
            self.assertEqual(component["rate_unit"], rate["unit"])
            exact = (
                Decimal(component["quantity"])
                * Decimal(component["price"])
                / Decimal(TOKENS_PER_MILLION)
            )
            rounded = exact.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            self.assertEqual(rounded, Decimal(component["cost_usd"]))
            exact_calculated += exact
        self.assertEqual(
            exact_calculated.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            Decimal(ui["priced_subtotal_usd"]),
        )

        expected_billable = set()
        expected_unpriced = set()
        for record in backend["expected_normalization"]:
            source_id = record["source_record_id"]
            expected_billable.update(
                (source_id, meter, quantity) for meter, quantity in record["billable"]
            )
            expected_unpriced.update(
                (source_id, meter, quantity, unit, reason)
                for meter, quantity, unit, reason in record["unpriced"]
            )
        actual_billable = {
            (source_id, item["meter"], item["quantity"])
            for item in ui["components"]
            for source_id in item["source_record_ids"]
        }
        actual_unpriced = {
            (source_id, item["meter"], item["quantity"], item["unit"], item["reason"])
            for item in ui["unpriced_components"]
            for source_id in item["source_record_ids"]
        }
        self.assertEqual(actual_billable, expected_billable)
        self.assertEqual(actual_unpriced, expected_unpriced)

        raw_ids = {item["source_record_id"] for item in backend["raw_usage_records"]}
        covered_ids = {
            source_id
            for item in [*ui["components"], *ui["unpriced_components"]]
            for source_id in item["source_record_ids"]
        }
        self.assertEqual(covered_ids, raw_ids)
        raw = {
            item["source_record_id"]: item["source"]
            for item in backend["raw_usage_records"]
        }
        self.assertEqual(
            sorted(value[1] for value in expected["anthropic-unknown-ttl"]["billable"]),
            sorted(
                [
                    raw["anthropic-unknown-ttl"]["input_tokens"],
                    raw["anthropic-unknown-ttl"]["cache_read_input_tokens"],
                    raw["anthropic-unknown-ttl"]["output_tokens"],
                ]
            ),
        )
        self.assertEqual(
            sorted(
                value[1] for value in expected["openai-negative-remainder"]["unpriced"]
            ),
            sorted(
                [
                    raw["openai-negative-remainder"]["input_tokens"],
                    raw["openai-negative-remainder"]["cached_input_tokens"],
                    raw["openai-negative-remainder"]["output_tokens"],
                ]
            ),
        )
        openai_priced = [
            item
            for item in ui["components"]
            if "openai-negative-remainder" in item["source_record_ids"]
        ]
        self.assertEqual(openai_priced, [])
        cursor_qualities = {
            item["measurement_quality"]
            for item in [*ui["components"], *ui["unpriced_components"]]
            if item["agent"] == "composer"
        }
        self.assertEqual(
            cursor_qualities, {"provider_reported", "derived", "estimated"}
        )
        validate_cost_estimates(ui)

    def test_full_precision_subtotals_round_once_per_root_agent_and_day(self):
        base = load_json(FIXTURES / "synthetic_ui_v1.json")
        components = [copy.deepcopy(base["components"][0]) for _ in range(2)]
        for index, component in enumerate(components):
            component["source_record_ids"] = [f"submicro-{index}"]
            component["meter"] = "input.uncached" if index == 0 else "output"
            component["quantity"] = 4
            component["price"] = "0.1"
            component["cost_usd"] = "0.000000"
        rollup = {
            "status": "complete",
            "quality": "provider_reported",
            "total_usd": "0.000001",
            "priced_subtotal_usd": "0.000001",
        }
        scopes = {
            "hourly_usage": {"2026-08-01": {"12": dict(rollup)}},
            "daily_usage": {"2026-08-01": dict(rollup)},
            "monthly_usage": {"2026-08": dict(rollup)},
            "yearly_usage": {"2026": dict(rollup)},
        }
        payload = {
            **base,
            **rollup,
            **scopes,
            "agents": {
                "claude": {
                    "display_name": "Claude",
                    **rollup,
                    **scopes,
                }
            },
            "components": components,
            "unpriced_components": [],
            "diagnostics": [],
        }
        self.assertEqual(
            sum(Decimal(item["cost_usd"]) for item in components), Decimal(0)
        )
        self.assertTrue(
            schema_accepts(SCHEMAS / "cost-estimates-v1.schema.json", payload)
        )
        validate_cost_estimates(payload)

        root_zero = copy.deepcopy(payload)
        root_zero["total_usd"] = "0.000000"
        root_zero["priced_subtotal_usd"] = "0.000000"
        with self.assertRaises(ContractViolation):
            validate_cost_estimates(root_zero)

        agent_zero = copy.deepcopy(payload)
        agent_zero["agents"]["claude"]["total_usd"] = "0.000000"
        agent_zero["agents"]["claude"]["priced_subtotal_usd"] = "0.000000"
        with self.assertRaises(ContractViolation):
            validate_cost_estimates(agent_zero)

        day_zero = copy.deepcopy(payload)
        day = day_zero["agents"]["claude"]["daily_usage"]["2026-08-01"]
        day["total_usd"] = "0.000000"
        day["priced_subtotal_usd"] = "0.000000"
        with self.assertRaises(ContractViolation):
            validate_cost_estimates(day_zero)

    def test_negative_fixtures_fail_schema_or_application_invariants(self):
        base = load_json(FIXTURES / "synthetic_ui_v1.json")
        cases = load_json(FIXTURES / "invalid_cost_estimates_v1.json")["cases"]
        for case in cases:
            with self.subTest(case=case["name"]):
                payload = copy.deepcopy(base)
                if "rename_key" in case:
                    old, new = case["rename_key"]
                    payload[case["path"]][new] = payload[case["path"]].pop(old)
                else:
                    path = case["path"]
                    if isinstance(path, str):
                        payload[path] = case["value"]
                    else:
                        target = payload
                        for part in path[:-1]:
                            target = target[part]
                        target[path[-1]] = case["value"]
                if case["rejected_by"] == "schema":
                    self.assertFalse(
                        schema_accepts(
                            SCHEMAS / "cost-estimates-v1.schema.json", payload
                        )
                    )
                else:
                    self.assertTrue(
                        schema_accepts(
                            SCHEMAS / "cost-estimates-v1.schema.json", payload
                        )
                    )
                    with self.assertRaises(ContractViolation):
                        validate_cost_estimates(payload)

    def test_legacy_json_golden_has_no_cost_section(self):
        spec = importlib.util.spec_from_file_location("legacy_usage_calendar", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        golden = load_json(FIXTURES / "legacy_json_v1.json")
        actual = module.build_usage_data(
            golden["daily_usage"],
            golden["hourly_usage"],
            golden["unique_messages"],
            golden["timezone"],
            golden["agents"],
        )
        self.assertEqual(actual, golden)
        self.assertNotIn("cost_estimates", actual)

    def test_cli_no_costs_is_byte_identical_and_enabled_controls_are_live(self):
        with tempfile.TemporaryDirectory() as directory:
            base = [
                sys.executable,
                str(SCRIPT),
                "--json",
                "-q",
                "--no-cache",
                "--utc",
                "--search-path",
                directory,
            ]
            default = subprocess.run(base, check=True, capture_output=True, text=True)
            explicit = subprocess.run(
                [*base, "--no-costs"], check=True, capture_output=True, text=True
            )
            self.assertEqual(default.stdout, explicit.stdout)
            self.assertNotIn("cost_estimates", default.stdout)
            enabled = subprocess.run(
                [*base, "--costs"], check=True, capture_output=True, text=True
            )
            self.assertIn('"cost_estimates"', enabled.stdout)
            explained = subprocess.run(
                [*base, "--explain-costs"],
                check=True,
                capture_output=True,
                text=True,
            )
            json.loads(explained.stdout)
            self.assertIn("cost explanation", explained.stderr)
            missing_pack = subprocess.run(
                [*base, "--pricing", str(Path(directory) / "pack.json")],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(missing_pack.returncode, 2)


if __name__ == "__main__":
    unittest.main()

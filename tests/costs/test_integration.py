"""End-to-end feature-gated cost integration coverage."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "claude-usage-calendar.py"
SCHEMA = ROOT / "costs" / "schemas" / "cost-estimates-v1.schema.json"
CUSTOM_PACK = ROOT / "costs" / "fixtures" / "synthetic_pricing_pack_v1.json"
NATIVE_CODEX_FIXTURE = (
    ROOT / "costs" / "fixtures" / "integration" / "codex-native-two-models.jsonl"
)

SPEC = importlib.util.spec_from_file_location("usage_calendar_integration", SCRIPT)
usage_calendar = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(usage_calendar)


def _schema_accepts(payload: object, directory: Path) -> bool:
    executable = shutil.which("check-jsonschema")
    if executable:
        prefix = [executable]
    elif shutil.which("uvx"):
        prefix = ["uvx", "--from", "check-jsonschema", "check-jsonschema"]
    else:
        raise AssertionError("check-jsonschema CLI is required")
    payload_path = directory / "cost-estimates.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    checked = subprocess.run(
        [*prefix, "--schemafile", str(SCHEMA), str(payload_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    return checked.returncode == 0


def _write_claude_session(root: Path, *, cache_creation: int = 0, model: str = "synthetic-anthropic-v1") -> Path:
    path = (
        root
        / ".claude"
        / "projects"
        / "demo"
        / "11111111-1111-1111-1111-111111111111.jsonl"
    )
    path.parent.mkdir(parents=True)
    usage = {
        "input_tokens": 40,
        "cache_read_input_tokens": 20,
        "output_tokens": 5,
    }
    if cache_creation:
        usage["cache_creation_input_tokens"] = cache_creation
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-01T12:34:00Z",
                "billing_channel": "api",
                "message": {
                    "id": "message-1",
                    "model": model,
                    "usage": usage,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class CostIntegrationTests(unittest.TestCase):
    def run_cli(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--utc",
                "--search-path",
                str(root),
                "-q",
                "--no-cache",
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_disabled_default_and_explicit_flag_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_claude_session(root)
            default = self.run_cli(root, "--json")
            explicit = self.run_cli(root, "--json", "--no-costs")
            self.assertEqual(default.returncode, 0)
            self.assertEqual(default.stdout, explicit.stdout)
            self.assertNotIn("cost_estimates", default.stdout)

    def test_custom_pack_prices_raw_api_record_and_emits_all_backend_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = _write_claude_session(root)
            result = self.run_cli(
                root,
                "--json",
                "--pricing",
                str(CUSTOM_PACK),
                "--require-complete-pricing",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            estimates = payload["cost_estimates"]
            self.assertEqual(estimates["status"], "complete")
            self.assertEqual(estimates["pricing_pack"]["kind"], "custom")
            self.assertEqual(set(estimates["hourly_usage"]["2026-08-01"]), {"12"})
            self.assertEqual(set(estimates["daily_usage"]), {"2026-08-01"})
            self.assertEqual(set(estimates["monthly_usage"]), {"2026-08"})
            self.assertEqual(set(estimates["yearly_usage"]), {"2026"})
            self.assertEqual(
                estimates["agents"]["claude"]["monthly_usage"]["2026-08"],
                estimates["monthly_usage"]["2026-08"],
            )
            self.assertTrue(_schema_accepts(estimates, root))

            raw = usage_calendar.extract_cost_raw_records(
                {**{key: [] for key in usage_calendar.AGENT_NAMES}, "claude": [str(session)]},
                timezone.utc,
            )[0]
            self.assertEqual(raw.channel, "api")
            self.assertEqual(raw.model, "synthetic-anthropic-v1")
            self.assertEqual(raw.source["input_tokens"], 40)

            from costs.registry import ADAPTER_SLOTS

            normalized = ADAPTER_SLOTS["anthropic"](raw)
            duplicate = list(normalized.quantities) * 2
            compacted_billable, compacted_unpriced = usage_calendar._compact_normalized_costs(
                duplicate, [], limit=3
            )
            self.assertEqual(compacted_unpriced, [])
            self.assertEqual(
                sum(item.quantity for item in compacted_billable),
                2 * sum(item.quantity for item in normalized.quantities),
            )
            self.assertTrue(
                all(
                    item.source_record_id.startswith("cost-compact:billable:")
                    for item in compacted_billable
                )
            )

    def test_partial_unpriced_require_complete_and_explain_are_honest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_claude_session(root, cache_creation=10)
            partial = self.run_cli(root, "--json", "--pricing", str(CUSTOM_PACK))
            self.assertEqual(partial.returncode, 0, partial.stderr)
            self.assertEqual(json.loads(partial.stdout)["cost_estimates"]["status"], "partial")

            required = self.run_cli(
                root,
                "--json",
                "--pricing",
                str(CUSTOM_PACK),
                "--require-complete-pricing",
            )
            self.assertEqual(required.returncode, 2)
            self.assertEqual(required.stdout, "")
            self.assertIn("status=partial", required.stderr)

            explained = self.run_cli(
                root, "--json", "--pricing", str(CUSTOM_PACK), "--explain-costs"
            )
            json.loads(explained.stdout)
            self.assertIn("UNKNOWN_CACHE_TTL", explained.stderr)
            self.assertIn("Published-rate-equivalent cost explanation", explained.stderr)

            unpriced = self.run_cli(root, "--json", "--costs")
            self.assertEqual(
                json.loads(unpriced.stdout)["cost_estimates"]["status"], "unpriced"
            )

    def test_cli_sources_preserve_provider_semantics_without_api_guessing(self):
        from costs.registry import ADAPTER_SLOTS

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / "rollout-session.jsonl"
            codex.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "type": "event_msg",
                            "timestamp": "2026-08-01T13:00:00Z",
                            "payload": {"type": "session_meta", "model": "gpt-5.3-codex"},
                        },
                        {
                            "type": "event_msg",
                            "timestamp": "2026-08-01T13:01:00Z",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 100,
                                        "cached_input_tokens": 40,
                                        "output_tokens": 5,
                                    }
                                },
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            codex_record = usage_calendar._extract_codex_cost_records(
                str(codex), timezone.utc
            )[0]
            self.assertEqual(codex_record.model, "gpt-5.3-codex")
            self.assertEqual(codex_record.channel, "subscription")

            grok = root / "updates.jsonl"
            grok.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-08-01T14:00:00Z",
                        "params": {
                            "sessionId": "session",
                            "update": {
                                "prompt_id": "prompt",
                                "model": "grok-4.6",
                                "usage": {
                                    "inputTokens": 30,
                                    "cachedReadTokens": 10,
                                    "cacheCreationTokens": 2,
                                    "outputTokens": 4,
                                },
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            grok_record = usage_calendar._extract_grok_cost_records(
                str(grok), timezone.utc
            )[0]
            self.assertEqual(
                grok_record.semantics_version,
                "xai.grok-cli.api-equivalent.v1",
            )
            self.assertEqual(grok_record.channel, "subscription")
            self.assertEqual(grok_record.context_band, "standard")
            self.assertEqual(grok_record.measurement_quality.value, "derived")
            grok_normalized = ADAPTER_SLOTS["xai"](grok_record)
            self.assertEqual(
                {item.meter.value: item.quantity for item in grok_normalized.quantities},
                {"input.uncached": 18, "input.cache_read": 10, "output": 4},
            )
            self.assertEqual(len(grok_normalized.unpriced_quantities), 1)

            composer = root / "composer.jsonl"
            composer.write_text(
                json.dumps(
                    {
                        "role": "assistant",
                        "timestamp": "2026-08-01T15:00:00Z",
                        "message": {"content": "estimated transcript output"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            composer_record = usage_calendar._extract_composer_cost_records(
                str(composer), timezone.utc
            )[0]
            self.assertEqual(composer_record.measurement_quality.value, "estimated")
            self.assertEqual(composer_record.model, "composer-2.5")
            self.assertEqual(composer_record.variant, "fast")
            self.assertEqual(composer_record.service_tier, "default")
            composer_normalized = ADAPTER_SLOTS["cursor"](composer_record)
            self.assertEqual(len(composer_normalized.quantities), 1)
            self.assertEqual(composer_normalized.unpriced_quantities, ())

    def test_invalid_metadata_never_turns_visible_tokens_into_complete_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_claude_session(root, model="m" * 257)
            entry = json.loads(path.read_text(encoding="utf-8"))
            entry["message"]["variant"] = "__proto__"
            entry["message"]["usage"]["service_tier"] = {"malformed": True}
            path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
            result = self.run_cli(root, "--json", "--pricing", str(CUSTOM_PACK))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["totals"]["input_tokens"], 40)
            estimates = payload["cost_estimates"]
            self.assertEqual(estimates["status"], "unpriced")
            self.assertNotEqual(estimates["unpriced_components"], [])
            self.assertIn(
                "invalid model metadata omitted", estimates["assumptions"]
            )
            self.assertIn(
                "invalid variant metadata omitted", estimates["assumptions"]
            )

    def test_grok_model_usage_splits_exact_models_and_composer_roles(self):
        from costs.registry import ADAPTER_SLOTS

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grok = root / "updates.jsonl"
            grok.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-08-29T14:00:00Z",
                        "params": {
                            "sessionId": "mixed-session",
                            "update": {
                                "prompt_id": "mixed-prompt",
                                "usage": {
                                    "modelUsage": {
                                        "grok-4.5-build": {
                                            "inputTokens": 30,
                                            "cachedReadTokens": 10,
                                            "cacheCreationTokens": 0,
                                            "outputTokens": 4,
                                        },
                                        "grok-4.6-build": {
                                            "inputTokens": 50,
                                            "cachedReadTokens": 5,
                                            "cacheCreationTokens": 0,
                                            "outputTokens": 6,
                                        },
                                    }
                                },
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            grok_records = usage_calendar._extract_grok_cost_records(
                str(grok), timezone.utc
            )
            self.assertEqual(
                {item.model for item in grok_records},
                {"grok-4.5-build", "grok-4.6-build"},
            )
            self.assertEqual(len({item.source_record_id for item in grok_records}), 2)
            self.assertTrue(
                all(
                    item.semantics_version == "xai.grok-cli.api-equivalent.v1"
                    and item.channel == "subscription"
                    and item.measurement_quality.value == "derived"
                    for item in grok_records
                )
            )
            normalized = [ADAPTER_SLOTS["xai"](item) for item in grok_records]
            self.assertTrue(all(item.quantities for item in normalized))
            self.assertTrue(all(not item.unpriced_quantities for item in normalized))

            composer = root / "composer.jsonl"
            composer.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "role": "user",
                            "timestamp": "2026-08-29T15:00:00Z",
                            "message": {"content": "estimated transcript input"},
                        },
                        {
                            "role": "assistant",
                            "timestamp": "2026-08-29T15:01:00Z",
                            "message": {"content": "estimated transcript output"},
                        },
                        {
                            "role": "assistant",
                            "timestamp": "2026-08-29T15:02:00Z",
                            "variant": "custom-fast",
                            "model": "historical-composer-model",
                            "message": {"content": "exact override"},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            composer_records = usage_calendar._extract_composer_cost_records(
                str(composer), timezone.utc
            )
            self.assertEqual(
                [(item.model, item.variant) for item in composer_records],
                [
                    ("composer-2.5", "fast"),
                    ("composer-2.5", "fast"),
                    ("historical-composer-model", "custom-fast"),
                ],
            )
            role_meters = [
                {quantity.meter.value for quantity in ADAPTER_SLOTS["cursor"](item).quantities}
                for item in composer_records[:2]
            ]
            self.assertEqual(role_meters, [{"input.uncached"}, {"output"}])

    def test_claude_cross_file_snapshots_merge_maxima_and_regression_unprices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index, (timestamp, input_tokens, nested_write) in enumerate(
                (
                    ("2026-08-01T12:00:00Z", 100, 50),
                    ("2026-08-01T12:01:00Z", 10, 5),
                ),
                1,
            ):
                path = (
                    root
                    / ".claude"
                    / "projects"
                    / f"demo-{index}"
                    / f"{index:08d}-1111-1111-1111-111111111111.jsonl"
                )
                path.parent.mkdir(parents=True)
                path.write_text(
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": timestamp,
                            "billing_channel": "api",
                            "message": {
                                "id": "shared-message",
                                "model": "synthetic-anthropic-v1",
                                "usage": {
                                    "input_tokens": input_tokens,
                                    "output_tokens": 5,
                                    "cache_read_input_tokens": 0,
                                    "cache_creation": {
                                        "ephemeral_5m_input_tokens": nested_write,
                                        "ephemeral_1h_input_tokens": 0,
                                    },
                                },
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                paths.append(str(path))

            files = {key: [] for key in usage_calendar.AGENT_NAMES}
            files["claude"] = paths
            raw = usage_calendar.extract_cost_raw_records(files, timezone.utc)
            self.assertEqual(len(raw), 1)
            self.assertEqual(raw[0].source_record_id, "claude:shared-message")
            self.assertEqual(raw[0].source["input_tokens"], 100)
            self.assertEqual(
                raw[0].source["cache_creation"]["ephemeral_5m_input_tokens"],
                50,
            )
            self.assertEqual(raw[0].semantics_version, "anthropic.v1")
            self.assertEqual(raw[0].measurement_quality.value, "derived")
            self.assertIn(usage_calendar.STREAMING_MAXIMA_ASSUMPTION, raw[0].assumptions)

            result = self.run_cli(root, "--json", "--pricing", str(CUSTOM_PACK))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["totals"]["input_tokens"], 100)
            self.assertNotEqual(payload["cost_estimates"]["status"], "complete")
            self.assertEqual(
                {
                    item["reason"]
                    for item in payload["cost_estimates"]["unpriced_components"]
                },
                {"RATE_NOT_FOUND"},
            )
            self.assertGreaterEqual(
                sum(
                    item["quantity"]
                    for item in payload["cost_estimates"]["unpriced_components"]
                    if item["meter"] in {None, "input.uncached"}
                ),
                100,
            )

    def test_grok_snapshot_maxima_are_derived_but_dimension_change_is_unpriced(self):
        from costs.pricing import PricingLoadOptions, load_pricing_pack

        pack = load_pricing_pack(PricingLoadOptions()).pack
        for scenario, second_model, second_input in (
            ("counter-regression", "grok-4.6", 10),
            ("dimension-change", "grok-other", 100),
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = []
                for index, (model, input_tokens) in enumerate(
                    (("grok-4.6", 100), (second_model, second_input)), 1
                ):
                    path = root / f"snapshot-{index}.jsonl"
                    path.write_text(
                        json.dumps(
                            {
                                "timestamp": f"2026-08-01T12:0{index}:00Z",
                                "params": {
                                    "sessionId": "shared-session",
                                    "update": {
                                        "prompt_id": "shared-prompt",
                                        "model": model,
                                        "usage": {
                                            "inputTokens": input_tokens,
                                            "cachedReadTokens": 20,
                                            "cacheCreationTokens": 0,
                                            "outputTokens": 5,
                                        },
                                    },
                                },
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    paths.append(str(path))

                files = {key: [] for key in usage_calendar.AGENT_NAMES}
                files["grok"] = paths
                raw = usage_calendar.extract_cost_raw_records(files, timezone.utc)
                self.assertEqual(len(raw), 1)
                self.assertEqual(
                    raw[0].semantics_version,
                    "xai.grok-cli.api-equivalent.v1",
                )
                self.assertEqual(raw[0].source["inputTokens"], 100)
                expected_assumption = (
                    usage_calendar.STREAMING_MAXIMA_ASSUMPTION
                    if scenario == "counter-regression"
                    else usage_calendar.MERGE_INCONSISTENCY_ASSUMPTION
                )
                self.assertIn(expected_assumption, raw[0].assumptions)

                result = usage_calendar.calculate_cost_result(raw, pack)
                self.assertFalse(result.components)
                self.assertNotEqual(result.rollup.status.value, "complete")
                expected_reason = (
                    "RATE_NOT_FOUND"
                    if scenario == "counter-regression"
                    else "SEMANTICS_INCONSISTENT"
                )
                self.assertEqual(
                    {item.reason.value for item in result.unpriced_quantities},
                    {expected_reason},
                )
                self.assertGreaterEqual(
                    sum(
                        item.quantity
                        for item in result.unpriced_quantities
                        if item.meter is None
                        or item.meter.value in {"input.uncached", "input.cache_read"}
                    ),
                    100,
                )
                self.assertIn(expected_reason, {item.reason.value for item in result.diagnostics})

    def test_native_codex_turn_context_binds_each_usage_to_current_model(self):
        records = usage_calendar._extract_codex_cost_records(
            str(NATIVE_CODEX_FIXTURE), timezone.utc
        )
        self.assertEqual([item.model for item in records], ["model-a", "model-b"])
        self.assertEqual(
            [item.source_record_id for item in records],
            ["codex:native-session:1", "codex:native-session:2"],
        )
        self.assertTrue(all(item.channel == "subscription" for item in records))

    def test_codex_total_only_regression_is_unpriced_then_resets_baseline(self):
        from costs.pricing import PricingLoadOptions, load_pricing_pack

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-total-only.jsonl"
            entries = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "total-only-session",
                        "model": "gpt-5.3-codex",
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.3-codex"},
                },
            ]
            for ordinal, hour, counters in (
                (1, 12, (100, 10, 0, 0)),
                (2, 13, (90, 20, 0, 0)),
                (3, 14, (95, 25, 0, 0)),
            ):
                input_tokens, output_tokens, cached_tokens, cache_write = counters
                entries.append(
                    {
                        "type": "event_msg",
                        "ordinal": ordinal,
                        "timestamp": f"2026-08-01T{hour:02}:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "billing_channel": "api",
                                "total_token_usage": {
                                    "input_tokens": input_tokens,
                                    "output_tokens": output_tokens,
                                    "cached_input_tokens": cached_tokens,
                                    "cache_write_input_tokens": cache_write,
                                },
                            },
                        },
                    }
                )
            path.write_text(
                "".join(json.dumps(entry) + "\n" for entry in entries),
                encoding="utf-8",
            )

            records = usage_calendar._extract_codex_cost_records(
                str(path), timezone.utc
            )
            self.assertEqual(len(records), 3)
            self.assertEqual(
                [(item.source["input_tokens"], item.source["output_tokens"]) for item in records],
                [(100, 10), (90, 20), (5, 5)],
            )
            self.assertTrue(
                all(item.semantics_version == "openai.inclusive-input.v1" for item in records)
            )
            self.assertTrue(all(item.model == "gpt-5.3-codex" for item in records))
            self.assertTrue(all(item.channel == "api" for item in records))
            self.assertIn(
                usage_calendar.CODEX_CUMULATIVE_REGRESSION_ASSUMPTION,
                records[1].assumptions,
            )
            self.assertNotIn(
                usage_calendar.CODEX_CUMULATIVE_REGRESSION_ASSUMPTION,
                records[2].assumptions,
            )

            result = usage_calendar.calculate_cost_result(
                records, load_pricing_pack(PricingLoadOptions()).pack
            )
            regressed_id = "codex:total-only-session:2"
            self.assertFalse(
                any(regressed_id in item.source_record_ids for item in result.components)
            )
            regressed = [
                item
                for item in result.unpriced_quantities
                if item.source_record_id == regressed_id
            ]
            self.assertTrue(regressed)
            self.assertEqual(
                {item.reason.value for item in regressed},
                {"SEMANTICS_INCONSISTENT"},
            )
            self.assertEqual(
                sum(item.quantity for item in regressed if item.meter.value == "output"),
                20,
            )
            reset_delta_id = "codex:total-only-session:3"
            self.assertTrue(
                any(reset_delta_id in item.source_record_ids for item in result.components)
            )
            reset_components = [
                item
                for item in result.components
                if reset_delta_id in item.source_record_ids
            ]
            self.assertEqual(
                {
                    item.rate_key.meter.value: item.quantity
                    for item in reset_components
                    if item.quantity
                },
                {"input.uncached": 5, "output": 5},
            )
            diagnostic = next(
                item
                for item in result.diagnostics
                if item.source_record_id == regressed_id
                and item.reason.value == "SEMANTICS_INCONSISTENT"
            )
            self.assertIn("reset baseline", diagnostic.detail)

    def test_codex_total_only_cache_regression_forces_whole_snapshot_unpriced(self):
        from costs.pricing import PricingLoadOptions, load_pricing_pack

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-cache-regression.jsonl"
            entries = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "cache-regression-session",
                        "model": "gpt-5.3-codex",
                    },
                }
            ]
            for ordinal, counters in (
                (1, (100, 10, 50, 0)),
                (2, (110, 20, 40, 0)),
            ):
                input_tokens, output_tokens, cached_tokens, cache_write = counters
                entries.append(
                    {
                        "type": "event_msg",
                        "ordinal": ordinal,
                        "timestamp": f"2026-08-01T{11 + ordinal:02}:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "billing_channel": "api",
                                "total_token_usage": {
                                    "input_tokens": input_tokens,
                                    "output_tokens": output_tokens,
                                    "cached_input_tokens": cached_tokens,
                                    "cache_write_input_tokens": cache_write,
                                },
                            },
                        },
                    }
                )
            path.write_text(
                "".join(json.dumps(entry) + "\n" for entry in entries),
                encoding="utf-8",
            )
            records = usage_calendar._extract_codex_cost_records(
                str(path), timezone.utc
            )
            self.assertEqual(records[1].source["cached_input_tokens"], 40)
            self.assertIn(
                usage_calendar.CODEX_CUMULATIVE_REGRESSION_ASSUMPTION,
                records[1].assumptions,
            )
            result = usage_calendar.calculate_cost_result(
                records, load_pricing_pack(PricingLoadOptions()).pack
            )
            regressed_id = "codex:cache-regression-session:2"
            self.assertFalse(
                any(regressed_id in item.source_record_ids for item in result.components)
            )
            regressed = [
                item
                for item in result.unpriced_quantities
                if item.source_record_id == regressed_id
            ]
            self.assertTrue(regressed)
            self.assertEqual(
                {item.reason.value for item in regressed},
                {"SEMANTICS_INCONSISTENT"},
            )
            self.assertEqual(
                sum(
                    item.quantity
                    for item in regressed
                    if item.meter.value == "input.cache_read"
                ),
                40,
            )

    def test_overflow_compaction_guarantees_limits_and_preserves_quantity(self):
        from costs.contracts import (
            BillableQuantity,
            MeasurementQuality,
            Meter,
            Provider,
            Unit,
            validate_cost_estimates,
        )
        from costs.engine import price_quantities
        from costs.pricing import PricingLoadOptions, load_pricing_pack
        from costs.serializer import serialize_cost_estimates

        occurred_at = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        quantities = [
            BillableQuantity(
                source_record_id=f"record-{index}",
                agent="codex",
                provider=Provider.ANTHROPIC,
                model="synthetic-anthropic-v1",
                channel="api",
                variant="standard",
                service_tier="default",
                context_band="standard",
                occurred_at=occurred_at.replace(hour=12 + (index % 2)),
                meter=Meter.INPUT_UNCACHED,
                quantity=10**12 + index,
                unit=Unit.TOKENS,
                measurement_quality=MeasurementQuality.PROVIDER_REPORTED,
                semantics_version="anthropic.v1",
                assumptions=(f"unique-assumption-{index}",),
            )
            for index in range(101)
        ]
        compacted_billable, compacted_unpriced = (
            usage_calendar._compact_normalized_costs(quantities, [], limit=100)
        )
        self.assertEqual(len(compacted_billable), 2)
        self.assertEqual(compacted_unpriced, [])
        self.assertEqual(
            sum(item.quantity for item in compacted_billable),
            sum(item.quantity for item in quantities),
        )
        self.assertEqual(
            {
                assumption
                for item in compacted_billable
                for assumption in item.assumptions
            },
            {
                "usage records compacted by exact rate dimensions and hour for bounded serialization"
            },
        )
        self.assertEqual(
            {
                (item.occurred_at.date().isoformat(), item.occurred_at.hour)
                for item in compacted_billable
            },
            {("2026-08-01", 12), ("2026-08-01", 13)},
        )
        self.assertEqual(
            {
                (
                    item.provider,
                    item.model,
                    item.channel,
                    item.variant,
                    item.service_tier,
                    item.context_band,
                    item.meter,
                    item.measurement_quality,
                    item.semantics_version,
                )
                for item in compacted_billable
            },
            {
                (
                    Provider.ANTHROPIC,
                    "synthetic-anthropic-v1",
                    "api",
                    "standard",
                    "default",
                    "standard",
                    Meter.INPUT_UNCACHED,
                    MeasurementQuality.PROVIDER_REPORTED,
                    "anthropic.v1",
                )
            },
        )
        pack = load_pricing_pack(
            PricingLoadOptions(path=CUSTOM_PACK, use_builtin=False)
        ).pack
        payload = serialize_cost_estimates(
            price_quantities(compacted_billable, compacted_unpriced, pack)
        )
        validate_cost_estimates(payload)
        self.assertLessEqual(
            len(payload["components"]) + len(payload["unpriced_components"]), 100
        )
        self.assertLessEqual(len(payload["diagnostics"]), 10_000)
        self.assertLessEqual(len(payload["assumptions"]), 1_000)

    def test_costs_off_imports_do_not_load_heavy_cost_modules(self):
        code = f"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('usage_calendar_lazy', {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import costs.cli
heavy = ['costs.pricing', 'costs.engine', 'costs.dashboard', 'costs.serializer']
print(json.dumps([name for name in heavy if name in sys.modules]))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout), [])

    @unittest.skipUnless(shutil.which("node"), "Node is required for script smoke")
    def test_enabled_html_script_is_parseable_and_xss_payload_is_script_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = "probe</script><script>alert('cost-probe')</script>\u2028"
            _write_claude_session(root, model=model)
            output = root / "report.html"
            result = self.run_cli(
                root,
                "--costs",
                "--no-open",
                "--output",
                str(output),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            html = output.read_text(encoding="utf-8")
            self.assertNotIn(model, html)
            self.assertNotIn("<script>alert('cost-probe')</script>", html)
            self.assertIn("\\u003c/script\\u003e", html)
            self.assertIn('class="cost-dashboard"', html)
            scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
            self.assertEqual(len(scripts), 1)
            script_path = root / "dashboard.js"
            script_path.write_text(scripts[0], encoding="utf-8")
            checked = subprocess.run(
                ["node", "--check", str(script_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            formatter_start = scripts[0].index("function formatBackendUsd")
            formatter_end = scripts[0].index(
                "function formatBackendCost", formatter_start
            )
            formatter_probe = subprocess.run(
                [
                    "node",
                    "-e",
                    scripts[0][formatter_start:formatter_end]
                    + "console.log(JSON.stringify(["
                    + "formatBackendUsd('0.000000'),"
                    + "formatBackendUsd('12.345678'),"
                    + "formatBackendUsd('999.999999')]))",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(formatter_probe.returncode, 0, formatter_probe.stderr)
            self.assertEqual(
                json.loads(formatter_probe.stdout),
                ["$0.00", "$12.35", "$1000.00"],
            )


if __name__ == "__main__":
    unittest.main()

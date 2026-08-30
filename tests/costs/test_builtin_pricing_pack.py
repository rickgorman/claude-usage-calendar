import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parents[2]
PACK_PATH = ROOT / "costs" / "pricing_packs" / "builtin-2026-08-29.json"
SOURCES_PATH = ROOT / "costs" / "pricing_packs" / "sources-2026-08-29.json"
SCHEMA_PATH = ROOT / "costs" / "schemas" / "pricing-pack-v1.schema.json"


class BuiltinPricingPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = json.loads(PACK_PATH.read_text(encoding="utf-8"))
        cls.sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))

    def test_pack_matches_strict_schema(self):
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
                pack_path = Path(directory) / "builtin-pricing-pack.json"
                pack_path.write_text(json.dumps(self.pack), encoding="utf-8")
                result = subprocess.run(
                    [
                        *prefix,
                        "--schemafile",
                        str(SCHEMA_PATH),
                        str(pack_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(
                result.returncode,
                0,
                msg=result.stdout + result.stderr,
            )
            return
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        errors = list(Draft202012Validator(schema).iter_errors(self.pack))
        self.assertEqual(errors, [])

    def test_exact_keys_are_unique_and_fully_dimensioned(self):
        fields = (
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
        keys = [tuple(rate[field] for field in fields) for rate in self.pack["rates"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), 210)
        for key in keys:
            self.assertTrue(all(key))
            self.assertFalse(any(char in value for value in key for char in "*?[{"))

    def test_prices_are_decimal_million_token_strings_and_sources_are_current(self):
        as_of = self.pack["valuation_as_of"]
        self.assertEqual(as_of, "2026-08-29")
        date.fromisoformat(as_of)
        source_urls = {source["url"] for source in self.sources["sources"]}
        for rate in self.pack["rates"]:
            self.assertEqual(rate["unit"], "million_tokens")
            self.assertRegex(rate["price"], r"^(0|[1-9][0-9]*)(\.[0-9]{1,12})?$")
            self.assertGreaterEqual(Decimal(rate["price"]), Decimal(0))
            self.assertEqual(rate["valuation_date"], as_of)
            self.assertEqual(rate["source"]["checked_at"], as_of)
            self.assertIn(rate["source"]["url"], source_urls)
            self.assertTrue(rate["source"]["url"].startswith("https://"))

    def test_reviewed_scope_and_exclusions_are_explicit(self):
        models = {rate["model"] for rate in self.pack["rates"]}
        self.assertEqual(
            models,
            {
                "claude-fable-5",
                "claude-opus-5",
                "claude-sonnet-5",
                "claude-haiku-4-5-20251001",
                "claude-opus-4-8",
                "claude-opus-4-6",
                "claude-sonnet-4-6",
                "gpt-5.3-codex",
                "gpt-5.4",
                "gpt-5.5",
                "gpt-5.6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "grok-4.6",
                "grok-4.5-build",
                "grok-4.6-build",
                "composer-2.5",
            },
        )
        self.assertEqual(
            {
                rate["channel"]
                for rate in self.pack["rates"]
                if rate["model"] == "gpt-5.3-codex"
            },
            {"api", "subscription"},
        )
        self.assertIn("cursor", {rate["provider"] for rate in self.pack["rates"]})
        self.assertEqual(
            {
                (rate["channel"], rate["measurement_quality"])
                for rate in self.pack["rates"]
                if rate["provider"] == "openai"
                and rate["model"] == "gpt-5.3-codex"
                and rate["channel"] == "api"
            },
            {("api", "provider_reported"), ("api", "derived")},
        )
        self.assertNotIn(
            "gpt-5.6",
            {rate["model"] for rate in self.pack["rates"]},
        )
        self.assertNotIn(
            "codex-auto-review",
            {rate["model"] for rate in self.pack["rates"]},
        )
        self.assertTrue(self.sources["excluded_coverage"])
        self.assertEqual(self.sources["pack_id"], self.pack["pack_id"])


if __name__ == "__main__":
    unittest.main()

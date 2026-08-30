from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from costs.dashboard import render_cost_dashboard
from costs.pricing import (
    PricingLoadOptions,
    PricingPackKind,
    load_pricing_pack,
)
from tests.costs.security.harness import (
    FIXTURES,
    SCHEMAS,
    SECURITY_FIXTURES,
    DuplicateJsonName,
    cost_result_from_payload,
    inject_dashboard_surface,
    load_json,
    materialize_pack_case,
    probe_html,
    strict_json_bytes,
)

EXPECTED_MISSING = "EXPECTED_MISSING_IMPLEMENTATION"


def schema_rejected_names(cases):
    """Return rejected case names using one in-process or one CLI validation pass."""

    try:
        from jsonschema import Draft202012Validator, FormatChecker
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
            paths = []
            for name, payload in cases:
                path = Path(directory) / f"{name}.json"
                path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                paths.append(path)
            completed = subprocess.run(
                [
                    *prefix,
                    "--schemafile",
                    str(SCHEMAS / "pricing-pack-v1.schema.json"),
                    "--output-format",
                    "json",
                    *map(str, paths),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AssertionError(completed.stderr or completed.stdout) from error
        failures = [*report.get("errors", []), *report.get("parse_errors", [])]
        return {Path(item["filename"]).stem for item in failures}

    schema = load_json(SCHEMAS / "pricing-pack-v1.schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return {name for name, payload in cases if list(validator.iter_errors(payload))}


class PricingCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = load_json(FIXTURES / "synthetic_pricing_pack_v1.json")
        cls.manifest = load_json(SECURITY_FIXTURES / "pricing_adversarial_v1.json")

    def test_duplicate_member_corpus_is_rejected_before_mapping_construction(self):
        for name in ("duplicate_names_v1.json", "duplicate_rate_names_v1.json"):
            with self.subTest(name=name), self.assertRaises(DuplicateJsonName):
                strict_json_bytes((SECURITY_FIXTURES / name).read_bytes())

    def test_non_utf8_corpus_is_rejected_before_json_parsing(self):
        raw = bytes.fromhex(
            (SECURITY_FIXTURES / "non_utf8_v1.hex").read_text(encoding="ascii").strip()
        )
        with self.assertRaises(UnicodeDecodeError):
            strict_json_bytes(raw)

    def test_schema_rejects_every_schema_level_adversarial_case(self):
        cases = [
            (case["name"], materialize_pack_case(self.base, case))
            for case in self.manifest["cases"]
            if case["schema_rejects"]
        ]
        rejected = schema_rejected_names(cases)
        self.assertEqual(rejected, {name for name, _ in cases})

    def test_loader_rejects_raw_duplicate_names_and_non_utf8(self):
        corpus = [
            (path.name, path.read_bytes())
            for path in (
                SECURITY_FIXTURES / "duplicate_names_v1.json",
                SECURITY_FIXTURES / "duplicate_rate_names_v1.json",
            )
        ]
        corpus.append(
            (
                "non_utf8_v1.json",
                bytes.fromhex(
                    (SECURITY_FIXTURES / "non_utf8_v1.hex")
                    .read_text(encoding="ascii")
                    .strip()
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, raw in corpus:
                path = Path(directory) / name
                path.write_bytes(raw)
                try:
                    load_pricing_pack(PricingLoadOptions(path=path, use_builtin=False))
                except NotImplementedError as error:
                    self.skipTest(f"{EXPECTED_MISSING}: pricing loader: {error}")
                except (OSError, UnicodeError, ValueError):
                    continue
                self.fail(f"pricing loader accepted {name}")

    def test_loader_rejects_bounded_adversarial_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            for case in self.manifest["cases"]:
                with self.subTest(case=case["name"]):
                    payload = materialize_pack_case(self.base, case)
                    raw = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                    path = Path(directory) / f"{case['name']}.json"
                    path.write_bytes(raw)
                    try:
                        load_pricing_pack(
                            PricingLoadOptions(path=path, use_builtin=False)
                        )
                    except NotImplementedError as error:
                        self.skipTest(f"{EXPECTED_MISSING}: pricing loader: {error}")
                    except (OSError, UnicodeError, ValueError):
                        continue
                    self.fail(f"pricing loader accepted {case['name']}")

    def test_custom_pack_provenance_is_application_owned_and_exact_byte_hashed(self):
        raw = (FIXTURES / "synthetic_pricing_pack_v1.json").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator-pack.json"
            path.write_bytes(raw)
            try:
                loaded = load_pricing_pack(
                    PricingLoadOptions(path=path, use_builtin=False)
                )
            except NotImplementedError as error:
                self.skipTest(f"{EXPECTED_MISSING}: pricing loader: {error}")
        self.assertEqual(loaded.pack.provenance.kind, PricingPackKind.CUSTOM)
        self.assertEqual(loaded.pack.provenance.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(loaded.pack.provenance.pack_id, self.base["pack_id"])
        self.assertNotIn(str(path), repr(loaded.pack.provenance))


class DashboardSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = load_json(FIXTURES / "synthetic_ui_v1.json")
        cls.corpus = load_json(SECURITY_FIXTURES / "xss_payloads_v1.json")

    def render_or_skip(self, payload):
        try:
            output = render_cost_dashboard(cost_result_from_payload(payload), payload)
        except NotImplementedError as error:
            self.skipTest(f"{EXPECTED_MISSING}: dashboard renderer: {error}")
        return output.fragment.html

    def test_all_untrusted_surfaces_resist_all_html_script_vectors(self):
        baseline_html = self.render_or_skip(copy.deepcopy(self.base))
        baseline = probe_html(baseline_html)
        self.assertFalse(baseline.event_attributes)
        self.assertFalse(baseline.remote_attributes)

        for surface in self.corpus["surfaces"]:
            for attack in self.corpus["payloads"]:
                with self.subTest(surface=surface, attack=attack["name"]):
                    payload = copy.deepcopy(self.base)
                    inject_dashboard_surface(payload, surface, attack["value"])
                    html = self.render_or_skip(payload)
                    probe = probe_html(html)
                    self.assertEqual(probe.tag_shapes, baseline.tag_shapes)
                    self.assertFalse(probe.event_attributes)
                    self.assertFalse(probe.remote_attributes)
                    self.assertIn(attack["value"], probe.text)
                    self.assertNotIn("\u2028", html)
                    self.assertNotIn("\u2029", html)

    def test_renderer_uses_no_executable_or_remote_data_sinks(self):
        html = self.render_or_skip(copy.deepcopy(self.base))
        forbidden = {
            "innerHTML assignment": r"\.innerHTML\s*=",
            "outerHTML assignment": r"\.outerHTML\s*=",
            "adjacent HTML": r"insertAdjacentHTML\s*\(",
            "document.write": r"document\.write\s*\(",
            "eval": r"\beval\s*\(",
            "Function constructor": r"\bnew\s+Function\s*\(",
            "fetch": r"\bfetch\s*\(",
            "XMLHttpRequest": r"\bXMLHttpRequest\b",
            "WebSocket": r"\bWebSocket\s*\(",
        }
        for name, pattern in forbidden.items():
            with self.subTest(sink=name):
                self.assertIsNone(re.search(pattern, html, flags=re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()

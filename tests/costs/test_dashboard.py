"""Display and security tests for the cost dashboard boundary."""

from __future__ import annotations

import copy
import json
import typing
import unittest
from html.parser import HTMLParser
from pathlib import Path

from costs.contracts import validate_cost_estimates
from costs.dashboard import (
    DASHBOARD_ABSENCE_REQUIREMENT,
    REQUIRED_SERVED_DASHBOARD_HEADERS,
    render_cost_dashboard,
)
from costs.engine import CostResult

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "costs" / "fixtures" / "synthetic_ui_v1.json"


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def disabled_payload() -> dict[str, object]:
    """A schema/application-valid cost payload used only to render disabled copy."""

    payload = load_fixture()
    payload.update(
        {
            "status": "disabled",
            "total_usd": None,
            "priced_subtotal_usd": "0.000000",
            "hourly_usage": {},
            "daily_usage": {},
            "monthly_usage": {},
            "yearly_usage": {},
            "agents": {},
            "components": [],
            "unpriced_components": [],
            "diagnostics": [],
        }
    )
    return payload


class _HtmlElements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, attrs))


class DashboardTests(unittest.TestCase):
    def render(self, payload: dict[str, object]) -> str:
        return render_cost_dashboard(
            typing.cast(CostResult, None), payload
        ).fragment.html

    def test_frozen_fixture_renders_deterministically_with_explainability(self):
        payload = load_fixture()
        html = self.render(payload)

        self.assertEqual(html, self.render(copy.deepcopy(payload)))
        for text in (
            "Cost estimate",
            "Partial estimate",
            "Priced subtotal $0.55 USD",
            "Mixed measurement quality",
            "Provider-reported measurement",
            "Derived measurement",
            "Estimated measurement",
            "2026-08-01",
            "fixture.synthetic.v1",
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "Published-rate-equivalent estimate, not an invoice.",
            "Explain estimate coverage",
            "UNKNOWN_CACHE_TTL",
            "SEMANTICS_INCONSISTENT",
            "Static local artifact",
            "This fragment cannot enforce HTTP response headers",
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertIn('role="status" aria-live="polite"', html)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<img", html.lower())
        self.assertNotIn("<svg", html.lower())

    def test_complete_partial_and_unpriced_states_have_accessible_copy(self):
        expected_statuses = {
            "complete": ("Complete estimate", "Total $12.35 USD"),
            "partial": ("Partial estimate", "Priced subtotal $12.35 USD"),
            "unpriced": ("Unpriced usage", "No priced amount No priced amount"),
        }
        for status, expected in expected_statuses.items():
            payload = load_fixture()
            payload["status"] = status
            payload["total_usd"] = "12.345678" if status == "complete" else None
            payload["priced_subtotal_usd"] = "12.345678"
            html = self.render(payload)
            with self.subTest(status=status):
                self.assertIn(expected[0], html)
                self.assertIn(expected[1], html)

        for quality, expected in {
            "provider_reported": "Provider-reported measurement",
            "derived": "Derived measurement",
            "estimated": "Estimated measurement",
            "mixed": "Mixed measurement quality",
        }.items():
            payload = load_fixture()
            payload["quality"] = quality
            with self.subTest(quality=quality):
                self.assertIn(expected, self.render(payload))

    def test_disabled_state_uses_a_valid_zero_coverage_payload(self):
        payload = disabled_payload()
        validate_cost_estimates(payload)

        html = self.render(payload)
        self.assertIn("Cost estimation disabled", html)
        self.assertIn("Disabled Disabled", html)
        self.assertNotIn("UNKNOWN_CACHE_TTL", html)

    def test_served_dashboard_header_policy_is_immutable_and_nonpermissive(self):
        expected = {
            "Content-Security-Policy": (
                "default-src 'none'; base-uri 'none'; connect-src 'none'; "
                "form-action 'none'; frame-ancestors 'none'; img-src 'none'; "
                "object-src 'none'; script-src 'none'; style-src 'none'"
            ),
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
        self.assertEqual(dict(REQUIRED_SERVED_DASHBOARD_HEADERS), expected)
        self.assertNotIn(
            "access-control-allow-origin", {key.lower() for key in expected}
        )
        with self.assertRaises(TypeError):
            REQUIRED_SERVED_DASHBOARD_HEADERS["X-Frame-Options"] = "SAMEORIGIN"  # type: ignore[index]

    def test_absent_cost_estimates_is_an_integration_omission_contract(self):
        self.assertIsNone(
            None, "Integration supplies no payload when costs are disabled."
        )
        self.assertEqual(
            DASHBOARD_ABSENCE_REQUIREMENT,
            "When cost_estimates is absent, Integration must not call render_cost_dashboard "
            "or embed a dashboard fragment.",
        )

    def test_untrusted_fixture_strings_cannot_become_markup_or_handlers(self):
        payload = load_fixture()
        attack = (
            '"</script><svg/onload=alert(1)><img src=x onerror=alert(2)>\u2028\u2029'
        )
        payload["disclaimer"] = attack
        payload["pricing_pack"]["id"] = attack  # type: ignore[index]
        payload["agents"]["composer"]["display_name"] = attack  # type: ignore[index]
        payload["components"][0]["model"] = attack  # type: ignore[index]
        payload["diagnostics"][0]["detail"] = attack  # type: ignore[index]

        html = self.render(payload)
        lower = html.lower()
        self.assertNotIn("</script>", lower)
        self.assertNotIn("<script", lower)
        self.assertNotIn("<svg", lower)
        self.assertNotIn("<img", lower)
        self.assertIn("&lt;/script&gt;&lt;svg/onload=alert(1)&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", html)
        self.assertIn("&#x2028;&#x2029;", html)

        parser = _HtmlElements()
        parser.feed(html)
        forbidden_tags = {"script", "style", "iframe", "object", "embed", "link", "img"}
        forbidden_attributes = {"action", "formaction", "href", "src", "srcset"}
        for tag, attributes in parser.elements:
            with self.subTest(tag=tag):
                self.assertNotIn(tag, forbidden_tags)
            for name, _ in attributes:
                with self.subTest(tag=tag, attribute=name):
                    self.assertFalse(name.startswith("on"))
                    self.assertNotIn(name, forbidden_attributes)


if __name__ == "__main__":
    unittest.main()

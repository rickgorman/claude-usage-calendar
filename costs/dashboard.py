"""Display-only HTML for already-validated cost-estimate JSON.

This module deliberately consumes the serializer's published values rather than the
engine's components.  It never selects a rate, derives a status, or calculates a
total.  The resulting fragment contains no scripts, resource URLs, event handlers,
or user-controlled attributes; all JSON-derived text is escaped before it becomes
HTML.  The current product writes a local static artifact: it runs no server and
makes no network requests.  A fragment cannot set HTTP response headers, so a future
integration that serves the artifact must apply ``REQUIRED_SERVED_DASHBOARD_HEADERS``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from types import MappingProxyType

from .contracts import FrozenJson, JsonObject
from .engine import CostResult


@dataclass(frozen=True, slots=True)
class SafeHtmlFragment:
    html: str


@dataclass(frozen=True, slots=True)
class DashboardOutput:
    fragment: SafeHtmlFragment


# These are a serving-layer contract, not a claim that an HTML fragment can enforce
# headers by itself.  In particular, frame-ancestors is effective only in a response
# header, and the intentionally absent Access-Control-Allow-Origin avoids permissive
# cross-origin access to locally generated session data.
REQUIRED_SERVED_DASHBOARD_HEADERS: Mapping[str, str] = MappingProxyType(
    {
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
)

# The absence of serialized cost_estimates means costs are disabled.  Integration
# owns the main-document assertion: it must omit this fragment rather than invoke
# this renderer with a made-up disabled payload.
DASHBOARD_ABSENCE_REQUIREMENT = (
    "When cost_estimates is absent, Integration must not call render_cost_dashboard "
    "or embed a dashboard fragment."
)


_STATUS_COPY = {
    "complete": ("Complete estimate", "Total"),
    "partial": ("Partial estimate", "Priced subtotal"),
    "unpriced": ("Unpriced usage", "No priced amount"),
    "disabled": ("Cost estimation disabled", "Disabled"),
}
_QUALITY_COPY = {
    "provider_reported": "Provider-reported measurement",
    "derived": "Derived measurement",
    "estimated": "Estimated measurement",
    "mixed": "Mixed measurement quality",
}


def _string(value: object, default: str = "Not provided") -> str:
    """Return schema text without trusting it as markup or an attribute value."""

    return value if isinstance(value, str) else default


def _escaped(value: object, default: str = "Not provided") -> str:
    """Serialize untrusted text for an HTML text node, including script separators."""

    # U+2028/U+2029 are harmless in a text node, but encoding them also keeps this
    # serializer safe if a host later transports the fragment through JavaScript.
    return (
        escape(_string(value, default), quote=True)
        .replace("\u2028", "&#x2028;")
        .replace("\u2029", "&#x2029;")
    )


def _mapping(value: object) -> Mapping[str, FrozenJson]:
    return value if isinstance(value, Mapping) else {}


def _objects(value: object) -> tuple[Mapping[str, FrozenJson], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _text_list(value: object) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return "Not provided"
    return ", ".join(_string(item) for item in value) or "None"


def _format_usd(value: object) -> str:
    """Format one backend six-place money string without deriving a new amount."""

    amount = _string(value, "")
    if len(amount) < 4 or amount.count(".") != 1:
        return "Unavailable"
    whole, fraction = amount.split(".", 1)
    if (
        not whole.isascii()
        or not whole.isdigit()
        or len(fraction) != 6
        or not fraction.isdigit()
    ):
        return "Unavailable"

    # This is presentation rounding only.  It operates on the backend's already
    # calculated decimal string and never sums, multiplies, or otherwise values data.
    cents = whole + fraction[:2]
    if fraction[2] >= "5":
        digits = list(cents)
        index = len(digits) - 1
        while index >= 0 and digits[index] == "9":
            digits[index] = "0"
            index -= 1
        if index < 0:
            digits.insert(0, "1")
        else:
            digits[index] = str(int(digits[index]) + 1)
        cents = "".join(digits)
    if len(cents) < 3:
        cents = cents.zfill(3)
    return f"${cents[:-2]}.{cents[-2:]} USD"


def _status_copy(status: object) -> tuple[str, str]:
    return _STATUS_COPY.get(
        _string(status, ""), ("Unavailable cost state", "Unavailable")
    )


def _quality_copy(quality: object) -> str:
    return _QUALITY_COPY.get(_string(quality, ""), "Unavailable measurement quality")


def _rollup_amount(rollup: Mapping[str, FrozenJson]) -> str:
    """Choose, but never infer, the backend amount appropriate to its status."""

    status = _string(rollup.get("status"), "")
    if status == "complete":
        return _format_usd(rollup.get("total_usd"))
    if status == "partial":
        return _format_usd(rollup.get("priced_subtotal_usd"))
    if status == "unpriced":
        return "No priced amount"
    if status == "disabled":
        return "Disabled"
    return "Unavailable"


def _definition_list(items: Sequence[tuple[str, object]]) -> str:
    return (
        "<dl>"
        + "".join(
            f"<dt>{escape(label, quote=True)}</dt><dd>{_escaped(value)}</dd>"
            for label, value in items
        )
        + "</dl>"
    )


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[object]], caption: str
) -> str:
    head = "".join(
        f'<th scope="col">{escape(header, quote=True)}</th>' for header in headers
    )
    body = "".join(
        "<tr>" + "".join(f"<td>{_escaped(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        "<table><caption>"
        + escape(caption, quote=True)
        + "</caption><thead><tr>"
        + head
        + "</tr></thead><tbody>"
        + body
        + "</tbody></table>"
    )


def _component_sort_key(component: Mapping[str, FrozenJson]) -> tuple[str, ...]:
    return tuple(
        _string(component.get(name), "")
        for name in ("agent", "occurred_on", "provider", "model", "meter", "reason")
    )


def render_cost_dashboard(
    result: CostResult, cost_estimates: JsonObject
) -> DashboardOutput:
    """Render a deterministic, display-only fragment from validated backend JSON.

    ``result`` is retained by the frozen boundary signature.  The dashboard reads no
    values from it: using only the validated serializer payload keeps presentation
    independent from pricing logic and prevents client-side recalculation.
    """

    del result
    payload = _mapping(cost_estimates)
    status_title, amount_label = _status_copy(payload.get("status"))
    root_amount = _rollup_amount(payload)

    agent_rows = []
    agents = _mapping(payload.get("agents"))
    for agent, raw_rollup in sorted(
        agents.items(), key=lambda item: _string(item[0], "")
    ):
        rollup = _mapping(raw_rollup)
        agent_status, agent_label = _status_copy(rollup.get("status"))
        agent_rows.append(
            (
                _string(rollup.get("display_name"), _string(agent)),
                agent_status,
                _quality_copy(rollup.get("quality")),
                agent_label,
                _rollup_amount(rollup),
            )
        )

    priced = sorted(_objects(payload.get("components")), key=_component_sort_key)
    unpriced = sorted(
        _objects(payload.get("unpriced_components")), key=_component_sort_key
    )
    diagnostics = sorted(
        _objects(payload.get("diagnostics")),
        key=lambda item: (
            _string(item.get("reason"), ""),
            _string(item.get("source_record_id"), ""),
        ),
    )

    priced_rows = [
        (
            _text_list(item.get("source_record_ids")),
            _string(item.get("occurred_on")),
            _string(item.get("agent")),
            _string(item.get("provider")),
            _string(item.get("model")),
            _string(item.get("meter")),
            _quality_copy(item.get("measurement_quality")),
            _string(item.get("price")),
            _format_usd(item.get("cost_usd")),
            _text_list(item.get("assumptions")),
        )
        for item in priced
    ]
    unpriced_rows = [
        (
            _text_list(item.get("source_record_ids")),
            _string(item.get("occurred_on")),
            _string(item.get("agent")),
            _string(item.get("provider")),
            _string(item.get("meter"), "No meter"),
            _quality_copy(item.get("measurement_quality")),
            _string(item.get("reason")),
            _text_list(item.get("assumptions")),
        )
        for item in unpriced
    ]
    diagnostic_rows = [
        (
            _string(item.get("reason")),
            _string(item.get("source_record_id")),
            _string(item.get("meter"), "No meter"),
            _string(item.get("detail"), "No additional detail"),
        )
        for item in diagnostics
    ]

    pricing_pack = _mapping(payload.get("pricing_pack"))
    provenance = _definition_list(
        (
            ("Valuation date", payload.get("valuation_as_of")),
            ("Pricing pack", pricing_pack.get("id")),
            ("Pack kind", pricing_pack.get("kind")),
            ("Pack SHA-256", pricing_pack.get("sha256")),
            ("Valuation basis", payload.get("basis")),
            ("Currency", payload.get("currency")),
        )
    )
    assumptions = _text_list(payload.get("assumptions"))

    html = (
        '<section class="cost-dashboard" aria-labelledby="cost-dashboard-title">'
        '<h2 id="cost-dashboard-title">Cost estimate</h2>'
        f'<p role="status" aria-live="polite"><strong>{escape(status_title, quote=True)}</strong>: '
        f"{escape(amount_label, quote=True)} {escape(root_amount, quote=True)}. "
        f"{escape(_quality_copy(payload.get('quality')), quote=True)}.</p>"
        "<p>Amounts are published-rate-equivalent snapshot estimates, not invoices. "
        "The dashboard only displays backend-produced values; it does not select rates or calculate totals.</p>"
        f"<p>{_escaped(payload.get('disclaimer'), 'No disclaimer provided')}</p>"
        "<h3>Pricing provenance</h3>"
        + provenance
        + "<h3>Cost status by agent</h3>"
        + _table(
            ("Agent", "Status", "Measurement quality", "Amount type", "Amount"),
            agent_rows,
            "Backend cost rollups by agent",
        )
        + "<details><summary>Explain estimate coverage</summary>"
        + "<p>Priced components are included only in backend-provided amounts. "
        "Unpriced components remain visible and prevent a partial estimate from becoming a total.</p>"
        + "<h3>Priced components</h3>"
        + _table(
            (
                "Source records",
                "Date",
                "Agent",
                "Provider",
                "Model",
                "Meter",
                "Measurement quality",
                "Backend rate per million tokens",
                "Backend cost",
                "Assumptions",
            ),
            priced_rows,
            "Backend-priced components",
        )
        + "<h3>Unpriced components</h3>"
        + _table(
            (
                "Source records",
                "Date",
                "Agent",
                "Provider",
                "Meter",
                "Measurement quality",
                "Reason",
                "Assumptions",
            ),
            unpriced_rows,
            "Visible usage without a price",
        )
        + "<h3>Diagnostics</h3>"
        + _table(
            ("Reason", "Source record", "Meter", "Detail"),
            diagnostic_rows,
            "Backend diagnostic reasons",
        )
        + "<h3>Backend assumptions</h3><p>"
        + _escaped(assumptions)
        + "</p></details>"
        + "<p>Static local artifact: it makes no third-party requests and starts no data server. "
        "This fragment cannot enforce HTTP response headers; any future serving integration must apply "
        "the required anti-framing header policy.</p>" + "</section>"
    )
    return DashboardOutput(SafeHtmlFragment(html))


__all__ = [
    "DASHBOARD_ABSENCE_REQUIREMENT",
    "REQUIRED_SERVED_DASHBOARD_HEADERS",
    "DashboardOutput",
    "SafeHtmlFragment",
    "render_cost_dashboard",
]

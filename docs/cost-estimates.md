# Cost estimates: operator contract

Cost estimates are optional. They revalue observable token counters with the
selected published-rate snapshot; they do not reconstruct historical spend.
They are not invoices and do not include provider discounts, credits, taxes,
contracts, or other billing adjustments.

## CLI controls

The executable help is authoritative and currently exposes:

```text
--costs
--no-costs
--pricing PATH
--require-complete-pricing
--explain-costs
```

Costs are disabled by default. `--costs` loads the built-in pack;
`--pricing PATH` loads one strict custom replacement pack and implies costs;
the two completeness/explanation flags also imply costs. `--no-costs` is an
explicit no-op and conflicts with every option that implies costs. With costs
off, `cost_estimates` is absent from JSON and default output is byte-identical
to `--no-costs`.

`--require-complete-pricing` exits 2 and emits no JSON stdout if any visible
usage cannot be priced. `--explain-costs` emits the bounded serialized cost
payload (including diagnostics) to stderr; machine JSON remains on stdout.

## Valuation and exact rate selection

The selected pack’s `valuation_as_of` is the estimate’s valuation date. Each
rate is USD per one million tokens as a decimal string. The engine groups
integer quantities by the complete key below, multiplies with Python
`Decimal`, and sums exact values before one half-up rounding to six decimal
places per scope. Floats and JavaScript `Number` are not monetary truth.

```text
provider, exact model, channel, variant, service tier, context band,
meter, valuation date, measurement quality
```

There are no wildcard, prefix, regex, provider-default, neighboring-model, or
date fallbacks. A missing exact key produces `RATE_NOT_FOUND`; ambiguity
produces `RATE_AMBIGUOUS`. Components preserve source record IDs and date/hour.
Histories over the bounded component format may be compacted only after
source-specific normalization; the output assumptions disclose compaction and
the integer quantity remains exact.

## Provider and source assumptions

These are semantic adapter rules, not inferred prices:

- Anthropic `input_tokens` is uncached input. Cache reads and writes are
  separate. Detailed 5-minute and 1-hour cache writes retain separate meters.
  An aggregate cache-creation counter without a known TTL remains unpriced as
  `UNKNOWN_CACHE_TTL`; no TTL is guessed.
- OpenAI/Codex input counters are inclusive. Uncached input is total input less
  cached input less cache-write input. A negative remainder is
  `SEMANTICS_INCONSISTENT`; the record is wholly unpriced and is not clamped.
  Reasoning output already included in output is not added a second time.
- xAI/Grok classifies the request-level total prompt, including cached tokens,
  before splitting uncached/cache-read quantities. The exact-model threshold
  mapping uses `grok-4.6`: totals at or above 200,000 are `long`, lower totals
  are `standard`. Daily aggregates cannot reconstruct this band. CLI aggregate
  records without a request total remain unpriced.
- Cursor preserves exact model/routed model, standard/fast variant,
  Auto/explicit selection, billing channel, and visible platform/tool-fee
  information. Generic cache writes without a TTL and unsupported fees remain
  visible and unpriced. No channel, model, fee, or quality is guessed.

The v1 token meters are `input.uncached`, `input.cache_read`,
`input.cache_write_5m`, `input.cache_write_1h`, and `output`. Diagnostic-only
unpriced quantities may also use `requests` or `count` for visible non-token
charges.

Composer/Cursor quality is record-level. Native cloud/team usage can be
`provider_reported`; local exported transcript roles use the reconstruction
assumption “roughly four characters per token” and are `estimated`. A rollup is
`mixed` when its records combine quality values. The legacy Composer
`estimated` field is only a compatibility file-level summary.

## JSON v1 shape

The cost object validates against
`costs/schemas/cost-estimates-v1.schema.json`, then against application
invariants. Its root includes:

```text
schema_version: 1
basis: "published_rate_equivalent_snapshot"
currency: "USD"
valuation_as_of: "YYYY-MM-DD"
pricing_pack: {id, kind: "builtin"|"custom", sha256}
status, quality, total_usd, priced_subtotal_usd
hourly_usage, daily_usage, monthly_usage, yearly_usage
agents, components, unpriced_components, diagnostics, assumptions, disclaimer
```

All money values are six-decimal strings such as `"0.000001"`; `total_usd`
may be `null`. Rollups have the same status/quality/amount fields. Components
include source IDs, occurred date/hour, agent, all dimensions, quality,
quantity/unit, assumptions, and (when priced) `rate_unit`, `price`, and
`cost_usd`. Unpriced entries additionally carry a bounded `reason`.

| Status | Contract |
| --- | --- |
| `complete` | No unpriced components; `total_usd` equals the backend priced subtotal. |
| `partial` | At least one priced and one unpriced component; `total_usd` is `null`, and the subtotal is a known lower bound. |
| `unpriced` | Visible unpriced components and no priced components; total is `null`, subtotal is `"0.000000"`. |
| `disabled` | No pack/no cost path; internal dashboard state only and omitted from machine JSON. |

Quality is independent of status: `provider_reported`, `derived`, and
`estimated` are record values; `mixed` is rollup-only. Scope maps cover exactly
the component dates/hours and agent keys. Diagnostics and arrays have bounded
wire limits. Identifiers reject `__proto__`, `prototype`, and `constructor`.

## Dashboard/security boundary

The generated HTML is a static local artifact. It embeds report data, starts no
server, and makes no third-party requests. Anyone who receives the file can
read its session data. The cost fragment is deterministic, contains no script,
resource URL, event-handler attribute, or executable/remote sink, and escapes
untrusted labels and values. The surrounding calendar itself contains inline
style/script and legacy client rendering; do not overstate the fragment’s
guarantee as a whole-document `textContent` guarantee.

The current file writer does not serve HTTP. If a future integration serves
the artifact, it must be separately reviewed, loopback/authenticated by
default, and apply the immutable contract in
`costs.dashboard.REQUIRED_SERVED_DASHBOARD_HEADERS`:

```text
Content-Security-Policy: default-src 'none'; base-uri 'none'; connect-src 'none'; form-action 'none'; frame-ancestors 'none'; img-src 'none'; object-src 'none'; script-src 'none'; style-src 'none'
X-Frame-Options: DENY
Cache-Control: no-store
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
```

The map deliberately has no permissive `Access-Control-Allow-Origin` entry.

## Live validation record (2026-08-28)

Commands were run read-only against temporary synthetic sessions plus the
default local search path. No private paths or historical dollar amounts are
recorded here.

```bash
./claude-usage-calendar.py --help
./claude-usage-calendar.py --json -q
./claude-usage-calendar.py --costs --json -q
python3 -m unittest -v
python3 -m unittest tests.costs.test_builtin_pricing_pack -v
python3 -m unittest tests.costs.test_integration.CostIntegrationTests.test_enabled_html_script_is_parseable_and_xss_payload_is_script_safe -v
python3 -m unittest tests.costs.test_dashboard tests.costs.test_security_contract -v
```

Observed results:

- Help lists all five cost controls and their implications exactly as above.
- Default local JSON completed with 60 data days and all four agent keys; it
  had no `cost_estimates` section. Cost-enabled local JSON completed with the
  built-in pack and reported `status: "unpriced"`, `quality: "mixed"`, 0
  priced components, 6,314 unpriced components, and 60 covered days; no
  historical amount is claimed.
- In a temporary API session, default and `--no-costs` JSON were byte-identical.
  A synthetic custom pack produced `status: "complete"`, `kind: "custom"`, a
  64-character SHA-256, and hourly/daily/monthly/yearly scopes. HTML generation
  succeeded in both modes; costs-off had `const costData = null` and no cost
  dashboard container, while costs-on embedded the validated cost dashboard.
- The built-in pricing-pack tests passed (4 tests), the dashboard/security
  checks passed (14 tests), the dedicated Node smoke passed, and the full suite
  passed: 123 tests, 0 failures.

The JSON schemas are strict Draft 2020-12 documents and are validated by the
tests using the repository’s available schema validator path. The built-in
pack contains 26 unique exact cards and its source manifest is checked by the
pack tests.

# ADR 0001: Published-rate-equivalent snapshot valuation contract

- Status: Accepted
- Date: 2026-08-28
- Scope: Cost-estimation contract gate (PR0)

## Decision

Cost estimates are an optional, parallel path beside the existing token reports. The
legacy parser, deduplication, `TokenAggregator` behavior, token fields, and costs-off
JSON are immutable inputs to this work. Cost valuation never begins with provider,
agent, hourly, or daily aggregates.

The only permitted flow is:

```text
raw source record
  -> source-specific semantic adapter
  -> BillableQuantity or UnpricedQuantity
  -> bucket by the complete RateKey
  -> strict exact-key rate lookup
  -> Python Decimal multiplication and summation
  -> backend status and measurement-quality rollups
  -> display-only JSON/UI
```

`RawUsageRecord`, `BillableQuantity`, `UnpricedQuantity`, and `RateKey` are frozen in
`costs/contracts.py`. A `RateKey` consists, in order, of provider, exact model,
channel, variant, service tier, context band, meter, valuation date, and record-level
measurement quality. Empty dimensions, wildcard/prefix/regex matching, and
provider-wide defaults are not exact keys. Raw unknown provider strings are preserved
within the bounded record contract and become `UNKNOWN_PROVIDER` unpriced quantities;
only the four known `Provider` values can form `BillableQuantity` or `RateKey` values.

Version 1 is USD-only. Quantities are nonnegative integers. Every priced rate has
`unit: "million_tokens"` and a decimal-string `price`. After quantities have been
aggregated once by complete `RateKey`, the engine formula is exactly
`integer_quantity * Decimal(price) / 1_000_000`, applied once per bucket. Pack prices
and output money are decimal strings; floats and JavaScript `Number` are never a
source of monetary truth. Each bucket retains its exact, unquantized Decimal cost and
its source date/hour. At root and per-agent hourly, daily, monthly, yearly, and total
scopes, exact bucket costs are summed first and the subtotal is rounded half-up to six
decimal places exactly once. Rounded component display strings are validated
independently and are never summed to obtain a subtotal. The UI only formats a backend
amount and never recomputes it. Histories exceeding the bounded wire-component limit
may be compacted only after source-specific normalization, by complete dimensions and
local date/hour; integer quantities remain exact and emitted assumptions disclose the
compaction count.

Valuation is a published-rate-equivalent snapshot as of the selected pack's
`valuation_as_of` date. It is a reproducible revaluation of observable usage, not a
claim about historical spend, discounts, credits, taxes, contracts, or an invoice.
Custom packs replace the built-in pack; they are not merged with it. Provenance kind
(`builtin` or `custom`) is assigned by the application, and SHA-256 covers the exact
pack bytes.

## Status, quality, and incomplete knowledge

Status and measurement quality are independent. Status is `complete`, `partial`,
`unpriced`, or `disabled`; quality is `provider_reported`, `derived`, `estimated`, or
`mixed`. `mixed` is a rollup only: every raw record, billable quantity, and unpriced
quantity has exactly one of the other three qualities. Cursor/Composer quality is
therefore record-level, not a product-wide constant.

For `complete`, `total_usd` and `priced_subtotal_usd` are the same backend result. For
`partial`, `total_usd` is null and `priced_subtotal_usd` is the known lower bound. For
`unpriced`, `total_usd` is null and the subtotal is `0.000000`. For `disabled`, no
pricing pack is loaded and the cost section is absent from legacy machine JSON.
Visible unsupported charges force partial or unpriced status; they are never silently
discarded.

`complete` has no unpriced components. `partial` has at least one priced and one
unpriced component. `unpriced` has visible unpriced usage and no priced components.
`disabled` has neither. Schema validation is followed by the mandatory application
invariant validator in `costs/contracts.py`; serialization may proceed only if totals,
component sums, statuses, qualities, agent/day rollups, and bounded diagnostics all
reconcile.

Diagnostics use only the bounded `DiagnosticReason` enum. Human-readable details,
assumptions, and all provider/model/agent/project/session/pack labels remain untrusted
text. Dynamic mapping identifiers reject `__proto__`, `prototype`, and `constructor`.

## Frozen provider semantics

- Anthropic `input_tokens` is uncached input and cache fields are not subtracted.
  Detailed 5-minute and 1-hour cache writes retain distinct meters. Aggregate cache
  creation with unknown TTL stays unpriced as `UNKNOWN_CACHE_TTL` unless the record
  carries an explicit, frozen assumption; a TTL is never guessed.
- OpenAI ordinary input under inclusive semantics is total input minus cached input
  minus cache-write input. A negative remainder is `SEMANTICS_INCONSISTENT`; it is
  never clamped. The entire record is unpriced—no quantities from it become
  `BillableQuantity`; every visible counter is preserved as an `UnpricedQuantity`.
  Reasoning already included in output is not added again.
- xAI context-band classification requires the request-level total prompt including
  cached tokens. Classification happens before cache and uncached quantities are
  separated or aggregated. Thresholds come from an immutable exact-model mapping,
  never request-provided metadata or a provider-wide default. For `grok-4.6`, the
  reviewed threshold is 200,000 tokens: totals at or above it are `long`, and lower
  totals are `standard`. A daily aggregate cannot reconstruct the band.
- Cursor preserves exact model/version, standard/fast variant, Auto/explicit
  selection, billing channel, and visible platform-fee information. Unknown routing
or unsupported fees remain visible and unpriced rather than being guessed.

The only v1 token meters are `input.uncached`, `input.cache_read`,
`input.cache_write_5m`, `input.cache_write_1h`, and `output`, all in integer tokens.
An ambiguous source charge may have no meter until semantics are known.
`UnpricedQuantity` additionally permits diagnostic-only `requests` or `count` units
for visible non-token charges. Those units never appear in v1 pricing cards.

## Static dashboard and server security boundary

The generated dashboard is a static local artifact. It performs no third-party
requests and introduces no data server. If a future server is added, it must be a
separately reviewed boundary: loopback-only by default, no unauthenticated remote
session data, restrictive content/security headers, and explicit anti-framing.

Every cost payload string and label is untrusted. Cost-data serialization escapes
HTML-sensitive characters before embedding JSON in the document script and
encodes Unicode line separators. The dedicated cost fragment has no scripts,
resource URLs, event-handler attributes, or executable/remote data sinks; its
security tests assert that hostile values remain text. The surrounding calendar
is an existing self-contained document with inline style/script and legacy
`innerHTML` rendering, so this fragment guarantee must not be generalized to a
claim that the whole document uses `textContent` exclusively. The frontend may
select and display backend-provided totals, statuses, diagnostics, and
already-rounded strings. It may not subtract cache, select rates, multiply,
re-sum components, infer completeness, or turn a priced subtotal into a total.

Bundling data into a static file improves portability and keeps session data local,
but it also means anyone with the file can read that data. The artifact must make no
confidentiality claim; users control its storage and distribution.

## Workstream file ownership

PR0 owns `costs/contracts.py`, schema and frozen integration fixtures, this ADR,
adapter module boundaries, empty registry slots, and inert CLI stubs. After PR0:

| Workstream | Exclusive implementation ownership |
| --- | --- |
| A | `costs/pricing.py`, `costs/cli.py`, loader validation/tests, pricing provenance; never the main script |
| B1 | `costs/adapters/anthropic.py` and Anthropic adapter fixtures/tests |
| B2 | `costs/adapters/openai.py` and OpenAI/Codex adapter fixtures/tests |
| B3 | `costs/adapters/xai.py` and xAI/Grok adapter fixtures/tests |
| B4 | `costs/adapters/cursor.py` and Cursor/Composer adapter fixtures/tests |
| C | `costs/engine.py`, exact-key Decimal engine, engine-only fixtures/tests |
| D | `costs/serializer.py`, production `cost_estimates` serializer, schema goldens, compatibility tests |
| E | `costs/dashboard.py`, display-only dashboard renderer and UI fixtures |
| F | adversarial corpora, security/property/mutation tests |
| G | reviewed data-only built-in pack, source manifest, operator pricing docs |
| Integration | main-script/argparse wiring, raw-record plumbing, and central registry population |

Provider branches do not edit the registry. Shared contract or schema changes require
integration-owner coordination rather than being made opportunistically in a branch.

The frozen cross-workstream interfaces are:

| Boundary | Input → output |
| --- | --- |
| A CLI | `ArgumentParser` → `None`; immutable `CostCliConfig` carries resolved values |
| A pricing | `PricingLoadOptions` → `PricingLoadResult`, containing immutable `PricingPack`, `RateCard`, and `PricingProvenance` values |
| C engine | `BillableQuantity` sequence + `UnpricedQuantity` sequence + adapter `Diagnostic` sequence + `PricingPack` → immutable `CostResult` |
| D serializer | `CostResult` → JSON-shaped `JsonObject`; emitted mappings must pass `validate_cost_estimates` |
| E dashboard | `CostResult` + validated `JsonObject` → immutable `DashboardOutput` containing `SafeHtmlFragment` |

The implementation keeps these boundaries separate: the CLI resolves an
immutable config, pricing loads one validated pack, adapters normalize source
semantics, the engine prices exact keys, the serializer validates the v1 wire
payload, and the dashboard renders only that validated payload.

## CLI controls and non-goals

`--costs` enables the built-in reviewed snapshot. `--pricing PATH` selects one
strict custom replacement pack and implies costs; it is not merged with the
built-in pack. `--require-complete-pricing` and `--explain-costs` also imply
costs. `--no-costs` is an explicit no-op and conflicts with every option that
implies costs. Costs remain disabled by default, including for `--json -q`.
When costs are disabled, the machine JSON has no `cost_estimates` member and is
byte-compatible with the legacy report. `--require-complete-pricing` fails with
exit code 2 and no JSON stdout when visible usage is partial or unpriced;
`--explain-costs` writes the bounded explanation to stderr.

The implemented v1 scope is intentionally limited. It does not fetch rates at
runtime, scrape provider pages, hot-reload packs, infer executable pricing
rules, or represent discounts, credits, taxes, contract terms, historical
spend, or invoice semantics. A generated dashboard is a static local artifact;
it starts no data server and makes no third-party requests. The immutable
`REQUIRED_SERVED_DASHBOARD_HEADERS` map is a future serving-layer contract, not
something the current file writer applies. Any future remote serving boundary
requires separate review, authentication/loopback controls, and the required
anti-framing/no-store/security headers.

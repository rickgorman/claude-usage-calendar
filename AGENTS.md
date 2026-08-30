# AGENTS.md

AI agent token usage analyzer. Parses Claude, Codex, Grok, and Composer session
files, normalizes their usage formats, deduplicates streaming records, and
aggregates token usage by date/hour. It writes legacy JSON or a static HTML
calendar. Cost estimation is an optional published-rate-equivalent snapshot
valuation and must never be described as historical spend.

## Operator quick reference

```bash
./claude-usage-calendar.py --json -q
./claude-usage-calendar.py --costs --json -q
./claude-usage-calendar.py --no-costs --json -q
./claude-usage-calendar.py --pricing PATH --json -q
./claude-usage-calendar.py --require-complete-pricing --json -q
./claude-usage-calendar.py --explain-costs --json -q
```

Costs are off by default. `--costs` enables the built-in snapshot;
`--pricing PATH`, `--require-complete-pricing`, and `--explain-costs` imply
costs. `--no-costs` is an explicit no-op and conflicts with all three implying
options. A strict custom pack replaces the built-in pack rather than merging
with it. `--require-complete-pricing` exits 2, with no JSON stdout, if visible
usage is not fully priced. `--explain-costs` writes bounded diagnostics and the
serialized cost payload to stderr while preserving machine JSON on stdout.

## Legacy JSON contract

With costs disabled, JSON contains `timezone`, `date_range`, `days_with_data`,
`unique_messages`, `totals`, `agents`, `daily_usage`, and `hourly_usage`.
`totals` and usage maps contain `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`, and (at totals) the
derived `total_tokens`. The tool reads recognized session layouts only and does
not modify agent data.

Composer’s legacy `agents.composer.estimated` is a file-level compatibility
summary. Cost quality is more precise: each record is `provider_reported`,
`derived`, or `estimated`; rollups can additionally be `mixed`. Native Cursor
Cloud Agent/team usage can therefore be provider-reported, while local
transcript text reconstruction is estimated at roughly four characters per
token.

## Cost JSON v1

Enabled output adds only a `cost_estimates` object to that legacy mapping. The
object is validated against `costs/schemas/cost-estimates-v1.schema.json` and
then against application invariants in `costs/contracts.py`. Required fields
are:

```text
schema_version, basis, currency, valuation_as_of, pricing_pack, status,
quality, total_usd, priced_subtotal_usd, hourly_usage, daily_usage,
monthly_usage, yearly_usage, agents, components, unpriced_components,
diagnostics, assumptions, disclaimer
```

`basis` is `published_rate_equivalent_snapshot` and `currency` is `USD`.
Money is a six-decimal, nonnegative decimal string (for example
`"1.250000"`), never a floating-point JSON number. Dates use ISO `YYYY-MM-DD`;
hour keys are canonical strings `"0"` through `"23"`; month and year keys are
`YYYY-MM` and `YYYY`. Each component preserves source IDs, date/hour, agent,
provider/model/channel/variant/service tier/context band, meter, quality,
quantity/unit, assumptions, and (when priced) rate unit, price, and cost.

Status and quality are independent:

| Status | Meaning | `total_usd` | `priced_subtotal_usd` |
| --- | --- | --- | --- |
| `complete` | No visible unpriced usage | amount | same backend amount |
| `partial` | Priced and unpriced usage coexist | `null` | known priced lower bound |
| `unpriced` | Visible unpriced usage and no priced components | `null` | `"0.000000"` |
| `disabled` | Internal no-pack state only | `null` | `"0.000000"` |

The disabled state is not serialized: when costs are off, `cost_estimates` is
absent and legacy JSON remains unchanged. Missing rates, unsupported charges,
unknown cache TTLs, malformed semantics, and incomplete dimensions remain in
`unpriced_components` with bounded enum reasons; they are not silently dropped.
Diagnostics are bounded and may be shown with `--explain-costs`.

## Pricing and valuation rules

The built-in pack is `costs/pricing_packs/builtin-2026-08-29.json`, a reviewed
snapshot valued as of 2026-08-29. Its exact cards and source citations are
documented in [docs/pricing.md](docs/pricing.md). A complete rate key is
provider, exact model, channel, variant, service tier, context band, meter,
valuation date, and record quality. There are no wildcard, prefix, regex, or
provider-default fallbacks. Rates are USD per million tokens as decimal text;
the engine multiplies integer quantities with `Decimal` and rounds each scope’s
exact subtotal half-up to six places once.

The estimate is a reproducible revaluation of observable counters at the pack’s
published-rate snapshot date. It is not an invoice and makes no claim about
historical spend, discounts, credits, taxes, contract terms, or provider
billing. Cache/model/channel assumptions and deliberate omissions are listed
in [docs/cost-estimates.md](docs/cost-estimates.md).

## Static dashboard boundary

The output HTML is a local static artifact containing embedded data. It starts
no server and makes no third-party requests; anyone given the file can read its
session data. The cost fragment is deterministic, has no executable/resource
sinks, and escapes untrusted displayed text. The surrounding calendar document
still contains its own inline style/script and legacy client rendering.

`costs.dashboard.REQUIRED_SERVED_DASHBOARD_HEADERS` is an immutable,
future-serving contract (CSP, anti-framing, no-store, no-referrer, and
`nosniff`); the current file writer does not serve HTTP or apply those headers.
Do not expose generated reports through an unauthenticated remote server.

## Validation

Run `python3 -m unittest` for the complete suite. The schemas are strict
Draft 2020-12 documents; the tests validate built-in and synthetic packs,
status/quality rollups, provider semantics, security corpus, custom SHA-256
provenance, and Node parsing of the generated cost script.

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds

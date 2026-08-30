# Built-in pricing snapshot and pack updates

`costs/pricing_packs/builtin-2026-08-29.json` is a reviewed, data-only USD
snapshot valued as of 2026-08-29. It is a published-rate-equivalent
revaluation, not an invoice or a claim about historical spend. The source
inventory is `costs/pricing_packs/sources-2026-08-29.json`; the manifest
records the exact URL, check date, and SHA-256 of every consulted capture.

## What is priced

The snapshot uses complete exact RateKeys (provider, model, channel, variant,
service tier, context band, meter, valuation date, and measurement quality).
All prices are nonnegative decimal strings in USD per one million tokens.

- Anthropic API cards cover the previously supported
  `claude-fable-5`, `claude-opus-5`, `claude-sonnet-5`,
  `claude-haiku-4-5-20251001`, and the exact `claude-opus-4-8` model.
  Historical `claude-opus-4-6` and `claude-sonnet-4-6` API cards are also
  included at their published standard rates. Subscription API-equivalent
  cards duplicate these seven models with the exact `service_tier=standard`
  dimension, including 5-minute and 1-hour cache-write meters. Both
  `provider_reported` and `derived` quality cards are present where a
  merge can produce either quality.
- OpenAI API `gpt-5.3-codex` remains available. Subscription cards cover
  exact `gpt-5.3-codex`, `gpt-5.6-sol`, `gpt-5.6-terra`,
  `gpt-5.6-luna`, `gpt-5.5`, and `gpt-5.4`, with uncached input,
  cache-read input, and output meters. Provider-reported and derived quality
  cards are intentionally separate RateKeys.
- xAI API `grok-4.6` remains available. Subscription-derived cards cover
  only exact `grok-4.5-build` and `grok-4.6-build`, mapped explicitly to
  the corresponding Grok API/OpenRouter rates in standard context.
- Cursor Composer 2.5 local transcripts have estimated cards for the exact
  `local-transcript/default/standard` dimensions and both explicitly
  documented variants: `fast` ($3/$15) and `standard` ($0.50/$2.50)
  input/output per million tokens. Fast is the documented default.

The pack contains 210 unique cards. Quality is part of the exact key, so a
provider-reported and derived card with the same other dimensions does not
overlap. The OpenRouter catalog was fetched and hashed for this review; no
runtime network request is made.

## Deliberately unpriced coverage

Unsupported usage remains visible as unpriced. Generic `gpt-5.6`,
`codex-auto-review`, `codex`, unknown models, and wildcard or prefix names
do not inherit a neighboring card. OpenAI cache-write charges, Anthropic
unknown-TTL cache writes, xAI long-context build aliases, Cursor cloud-agent
or third-party channels, platform fees, tool charges, credits, discounts,
taxes, and contract terms are omitted. The omission is intentional and is
listed in the source manifest.

Subscription cards are API-equivalent snapshot valuations. They do not claim
that a subscription provider bills by these rates. Local transcript usage is
estimated from reconstructed token counts and retains the adapter's
four-characters-per-token assumption.

## Custom packs and provenance

Use a custom pack as a strict replacement:

```bash
./claude-usage-calendar.py --pricing ./operator-pack.json --json -q
```

The loader reads exactly one pack at startup, validates the strict schema,
requires one common `valuation_as_of` date for all rates, rejects duplicate
or overlapping exact keys, and rejects unsafe or unbounded JSON. A custom pack
is not merged with the built-in pack. Output identifies application-owned
provenance as `pricing_pack.kind: "custom"` and includes a lowercase
SHA-256 digest of the exact pack bytes. The new built-in snapshot is selected
by the application default and is identified as `kind: "builtin"`.

## Reproducible data-only update process

1. Copy the current pack and source manifest to new date-stamped files. Set
   `valuation_as_of` and every rate's `valuation_date` to the review date.
2. Fetch the exact official provider, OpenRouter, and model-variant pages.
   Record URL, UTC check date, and SHA-256 in the matching source manifest.
3. Verify each exact model ID, channel, variant, service tier, context band,
   quality, meter, and USD-per-million-token value. Add only complete cards.
   If a dimension or rate is ambiguous, omit the card and explain it in
   `excluded_coverage`.
4. Run the built-in pricing tests, the schema validator, Ruff, and the full
   unittest suite. Review duplicate-key, source, and provenance checks before
   changing the default path.

Do not add a runtime fetch, scraper, hot reload, executable pricing rule, or
invoice calculation. A rate update changes reviewed data and citations only;
it does not alter the legacy token report or assert historical spend.

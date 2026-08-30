"""Normalize Cursor Cloud Agent and local Composer transcript usage.

The integration layer passes lossless records to this boundary.  Cloud Agent runs
use ``cursor.cloud-agent-run.v1`` and read camel-case counters from each
``runs[].usage`` payload; team usage events use ``cursor.team-usage.v1`` and read
the top-level ``tokenUsage`` payload.  Both require ``inputTokens`` and
``outputTokens``.  Under these explicit semantics contracts, ``inputTokens`` and
``cacheReadTokens`` are disjoint.  Generic ``cacheWriteTokens`` has no
5-minute/1-hour TTL dimension, so it is visible but deliberately unpriced.  The
legacy flattened source shape, if emitted by an integration, has the separate
``cursor.cloud.flattened.v1`` semantics version.
Auto selections must additionally carry the exact ``routed_model``.  Local
transcript records use ``cursor.local-transcript.v1`` and corresponding
``estimated_*`` counters; a transcript role needs only one valid input/output
component.

No channel, model, variant, platform fee, or source quality is inferred here.
That keeps an incomplete Cursor record visible without turning it into a guessed
complete cost.
"""

from __future__ import annotations

from collections.abc import Mapping

from costs.contracts import (
    BillableQuantity,
    Diagnostic,
    DiagnosticReason,
    MeasurementQuality,
    Meter,
    NormalizationResult,
    Provider,
    RawUsageRecord,
    Unit,
    UnpricedQuantity,
)

_CLOUD_AGENT_RUN_SEMANTICS = "cursor.cloud-agent-run.v1"
_TEAM_USAGE_SEMANTICS = "cursor.team-usage.v1"
_CLOUD_FLATTENED_SEMANTICS = "cursor.cloud.flattened.v1"
_LOCAL_TRANSCRIPT_SEMANTICS = "cursor.local-transcript.v1"
_LOCAL_TRANSCRIPT_ASSUMPTION = (
    "local transcript token reconstruction; not provider-reported"
)

_CLOUD_COUNTERS = (
    (Meter.INPUT_UNCACHED, "inputTokens"),
    (Meter.INPUT_CACHE_READ, "cacheReadTokens"),
    (Meter.OUTPUT, "outputTokens"),
)
_FLATTENED_COUNTERS = (
    (Meter.INPUT_UNCACHED, "input_tokens"),
    (Meter.INPUT_CACHE_READ, "cache_read_input_tokens"),
    (Meter.OUTPUT, "output_tokens"),
)
_LOCAL_COUNTERS = (
    (Meter.INPUT_UNCACHED, "estimated_input_tokens"),
    (Meter.INPUT_CACHE_READ, "estimated_cache_read_input_tokens"),
    (Meter.OUTPUT, "estimated_output_tokens"),
)
_CLOUD_CACHE_WRITE = "cacheWriteTokens"
_FLATTENED_CACHE_WRITE = "cache_write_input_tokens"
_LOCAL_CACHE_WRITE = "estimated_cache_write_input_tokens"
_REQUIRED_METERS = frozenset({Meter.INPUT_UNCACHED, Meter.OUTPUT})
_GENERIC_CACHE_WRITE_ASSUMPTION = (
    "Cursor reports cache writes without a TTL; no v1 cache-write meter is inferred"
)
_VISIBLE_CHARGES = (
    ("visible_tool_requests", Unit.REQUESTS),
    ("platform_fee_requests", Unit.REQUESTS),
    ("platform_fee_count", Unit.COUNT),
    ("platform_fee_tokens", Unit.TOKENS),
)
# Native Cloud platform-fee fields first, then snake-case integration aliases.
# They are monetary/platform charges, not token counters, so v1 preserves only a
# count marker rather than reinterpreting their amounts as tokens.  ``chargedCents``
# is deliberately excluded: it is observed total-charge metadata, not a fee.
_NATIVE_PLATFORM_FEE_FIELDS = (
    "cursorTokenFee",
    "cursor_token_fee",
    "platformFee",
    "platform_fee",
)
_PLATFORM_FEE_ASSUMPTION = (
    "native Cursor platform fee is not representable by v1 token meters"
)
_CHARGED_CENTS_ASSUMPTION = (
    "chargedCents is observed total-charge metadata, not a v1 platform-fee meter"
)


def normalize(record: RawUsageRecord) -> NormalizationResult:
    """Produce exact-key Cursor quantities, retaining incomplete usage unpriced."""

    counters, cache_write, missing, malformed = _counters(record)
    assumptions = _assumptions(record)
    model, model_reason = _exact_model(record)
    dimension_reason = _dimension_reason(record, model_reason)
    semantic_reason = _semantic_reason(record)

    quantities: list[BillableQuantity] = []
    unpriced: list[UnpricedQuantity] = []
    diagnostics: list[Diagnostic] = []

    malformed_reason = (
        DiagnosticReason.MALFORMED_SOURCE_RECORD if missing or malformed else None
    )
    reason = semantic_reason or dimension_reason or malformed_reason
    if reason is None:
        quantities.extend(
            _billable(record, model, meter, quantity, assumptions)
            for meter, quantity in counters
        )
    else:
        unpriced.extend(
            _unpriced(record, model, meter, quantity, Unit.TOKENS, reason, assumptions)
            for meter, quantity in counters
        )
        if counters:
            diagnostics.append(Diagnostic(reason, record.source_record_id))

    if missing:
        diagnostics.append(
            Diagnostic(
                DiagnosticReason.MALFORMED_SOURCE_RECORD,
                record.source_record_id,
                detail=f"missing required Cursor counter(s): {', '.join(missing)}",
            )
        )
        unpriced.extend(
            _unpriced(
                record,
                model,
                None,
                1,
                Unit.COUNT,
                DiagnosticReason.MALFORMED_SOURCE_RECORD,
                assumptions,
            )
            for _ in missing
        )

    for field, meter in malformed:
        unpriced.append(
            _unpriced(
                record,
                model,
                None,
                1,
                Unit.COUNT,
                DiagnosticReason.MALFORMED_SOURCE_RECORD,
                assumptions,
            )
        )
        diagnostics.append(
            Diagnostic(
                DiagnosticReason.INVALID_QUANTITY,
                record.source_record_id,
                meter,
                f"{field} must be a nonnegative integer",
            )
        )

    if cache_write is not None:
        cache_write_reason = reason or DiagnosticReason.UNSUPPORTED_METER
        cache_write_assumptions = assumptions
        if _GENERIC_CACHE_WRITE_ASSUMPTION not in cache_write_assumptions:
            cache_write_assumptions = (
                *cache_write_assumptions,
                _GENERIC_CACHE_WRITE_ASSUMPTION,
            )
        unpriced.append(
            _unpriced(
                record,
                model,
                None,
                cache_write,
                Unit.TOKENS,
                cache_write_reason,
                cache_write_assumptions,
            )
        )
        if cache_write_reason is DiagnosticReason.UNSUPPORTED_METER:
            diagnostics.append(
                Diagnostic(
                    DiagnosticReason.UNSUPPORTED_METER,
                    record.source_record_id,
                    detail="Cursor cache-write counter has no reported TTL",
                )
            )

    for field, unit in _VISIBLE_CHARGES:
        value = record.source.get(field)
        if value is None:
            continue
        if _is_quantity(value):
            if not value:
                continue
            unpriced.append(
                _unpriced(
                    record,
                    model,
                    None,
                    value,
                    unit,
                    DiagnosticReason.UNSUPPORTED_CHARGE,
                    assumptions,
                )
            )
        else:
            unpriced.append(
                _unpriced(
                    record,
                    model,
                    None,
                    1,
                    Unit.COUNT,
                    DiagnosticReason.INVALID_QUANTITY,
                    assumptions,
                )
            )
            diagnostics.append(
                Diagnostic(
                    DiagnosticReason.INVALID_QUANTITY,
                    record.source_record_id,
                    None,
                    f"{field} must be a nonnegative integer",
                )
            )

    for field, value in _native_platform_fees(record):
        if not _is_positive_amount(value):
            continue
        unpriced.append(
            _unpriced(
                record,
                model,
                None,
                1,
                Unit.COUNT,
                DiagnosticReason.UNSUPPORTED_CHARGE,
                (*assumptions, _PLATFORM_FEE_ASSUMPTION),
            )
        )
        diagnostics.append(
            Diagnostic(
                DiagnosticReason.UNSUPPORTED_CHARGE,
                record.source_record_id,
                detail=f"{field} is a visible native platform fee",
            )
        )

    return NormalizationResult(
        quantities=tuple(quantities),
        unpriced_quantities=tuple(unpriced),
        diagnostics=tuple(diagnostics),
    )


def _counters(
    record: RawUsageRecord,
) -> tuple[
    list[tuple[Meter, int]], int | None, list[str], list[tuple[str, Meter | None]]
]:
    sources, definitions, cache_write_field, shape_errors = _counter_sources(record)
    counter_totals = {meter: 0 for meter, _ in definitions}
    missing: list[str] = []
    malformed: list[tuple[str, Meter | None]] = []
    valid_primary_counter = False
    cache_write_total = 0
    for source in sources:
        for meter, field in definitions:
            value = source.get(field)
            if value is None:
                if (
                    record.semantics_version != _LOCAL_TRANSCRIPT_SEMANTICS
                    and meter in _REQUIRED_METERS
                ):
                    missing.append(field)
                continue
            if _is_quantity(value):
                if meter in _REQUIRED_METERS:
                    valid_primary_counter = True
                counter_totals[meter] += value
            else:
                malformed.append((field, meter))
        cache_write_value = source.get(cache_write_field)
        if cache_write_value is not None:
            if _is_quantity(cache_write_value):
                cache_write_total += cache_write_value
            else:
                malformed.append((cache_write_field, None))
    missing.extend(shape_errors)
    if (
        record.semantics_version == _LOCAL_TRANSCRIPT_SEMANTICS
        and not valid_primary_counter
    ):
        missing.append("estimated_input_tokens or estimated_output_tokens")
    counters = [
        (meter, quantity) for meter, quantity in counter_totals.items() if quantity
    ]
    cache_write = cache_write_total or None
    return counters, cache_write, missing, malformed


def _counter_sources(
    record: RawUsageRecord,
) -> tuple[
    tuple[Mapping[str, object], ...],
    tuple[tuple[Meter, str], ...],
    str,
    list[str],
]:
    if record.semantics_version == _CLOUD_AGENT_RUN_SEMANTICS:
        runs = record.source.get("runs")
        sources: list[Mapping[str, object]] = []
        shape_errors: list[str] = []
        if not isinstance(runs, tuple) or not runs:
            shape_errors.append("runs[].usage")
        else:
            for index, run in enumerate(runs):
                usage = run.get("usage") if isinstance(run, Mapping) else None
                if isinstance(usage, Mapping):
                    sources.append(usage)
                else:
                    shape_errors.append(f"runs[{index}].usage")
        if not sources:
            fallback = record.source.get("tokenUsage")
            if isinstance(fallback, Mapping):
                sources.append(fallback)
        return tuple(sources), _CLOUD_COUNTERS, _CLOUD_CACHE_WRITE, shape_errors
    if record.semantics_version == _TEAM_USAGE_SEMANTICS:
        token_usage = record.source.get("tokenUsage")
        shape_errors = []
        if not isinstance(token_usage, Mapping):
            shape_errors.append("tokenUsage")
            token_usage = _first_run_usage(record.source)
        sources = (token_usage,) if isinstance(token_usage, Mapping) else ()
        return sources, _CLOUD_COUNTERS, _CLOUD_CACHE_WRITE, shape_errors
    if record.semantics_version == _LOCAL_TRANSCRIPT_SEMANTICS:
        return (record.source,), _LOCAL_COUNTERS, _LOCAL_CACHE_WRITE, []
    return (record.source,), _FLATTENED_COUNTERS, _FLATTENED_CACHE_WRITE, []


def _first_run_usage(source: Mapping[str, object]) -> Mapping[str, object] | None:
    runs = source.get("runs")
    if not isinstance(runs, tuple):
        return None
    for run in runs:
        usage = run.get("usage") if isinstance(run, Mapping) else None
        if isinstance(usage, Mapping):
            return usage
    return None


def _exact_model(record: RawUsageRecord) -> tuple[str | None, DiagnosticReason | None]:
    selection = record.source.get("selection")
    routed_model = record.source.get("routed_model")
    selection_is_auto = _is_auto(selection)
    outer_model_is_auto = _is_auto(record.model)
    selection_is_explicit = _is_explicit(selection)
    if (
        selection_is_auto and _is_identifier(record.model) and not outer_model_is_auto
    ) or (outer_model_is_auto and selection_is_explicit):
        return None, DiagnosticReason.SEMANTICS_INCONSISTENT
    if selection_is_auto or outer_model_is_auto:
        if not _is_exact_model(routed_model):
            return None, DiagnosticReason.UNKNOWN_ROUTED_MODEL
        return routed_model, None
    if _is_identifier(routed_model):
        if not _is_identifier(record.model) or record.model != routed_model:
            return None, DiagnosticReason.SEMANTICS_INCONSISTENT
        return routed_model, None
    if _is_identifier(record.model):
        return record.model, None
    return None, DiagnosticReason.MISSING_MODEL


def _dimension_reason(
    record: RawUsageRecord, model_reason: DiagnosticReason | None
) -> DiagnosticReason | None:
    if record.provider != Provider.CURSOR and record.provider != Provider.CURSOR.value:
        return (
            DiagnosticReason.MISSING_PROVIDER
            if record.provider is None
            else DiagnosticReason.UNKNOWN_PROVIDER
        )
    if model_reason is not None:
        return model_reason
    for value, reason in (
        (record.channel, DiagnosticReason.MISSING_CHANNEL),
        (record.variant, DiagnosticReason.MISSING_VARIANT),
        (record.service_tier, DiagnosticReason.MISSING_SERVICE_TIER),
        (record.context_band, DiagnosticReason.MISSING_CONTEXT_BAND),
    ):
        if not _is_identifier(value):
            return reason
    return None


def _semantic_reason(record: RawUsageRecord) -> DiagnosticReason | None:
    if record.semantics_version not in {
        _CLOUD_AGENT_RUN_SEMANTICS,
        _TEAM_USAGE_SEMANTICS,
        _CLOUD_FLATTENED_SEMANTICS,
        _LOCAL_TRANSCRIPT_SEMANTICS,
    }:
        return DiagnosticReason.UNSUPPORTED_SEMANTICS_VERSION
    if (
        record.semantics_version == _LOCAL_TRANSCRIPT_SEMANTICS
        and record.measurement_quality is not MeasurementQuality.ESTIMATED
    ):
        return DiagnosticReason.SEMANTICS_INCONSISTENT
    return None


def _assumptions(record: RawUsageRecord) -> tuple[str, ...]:
    assumptions = record.assumptions
    if (
        record.semantics_version == _LOCAL_TRANSCRIPT_SEMANTICS
        and _LOCAL_TRANSCRIPT_ASSUMPTION not in assumptions
    ):
        assumptions = (*assumptions, _LOCAL_TRANSCRIPT_ASSUMPTION)
    if _charged_cents_observed(record) and _CHARGED_CENTS_ASSUMPTION not in assumptions:
        assumptions = (*assumptions, _CHARGED_CENTS_ASSUMPTION)
    return assumptions


def _billable(
    record: RawUsageRecord,
    model: str | None,
    meter: Meter,
    quantity: int,
    assumptions: tuple[str, ...],
) -> BillableQuantity:
    assert model is not None
    assert record.channel is not None
    assert record.variant is not None
    assert record.service_tier is not None
    assert record.context_band is not None
    return BillableQuantity(
        source_record_id=record.source_record_id,
        agent=record.agent,
        provider=Provider.CURSOR,
        model=model,
        channel=record.channel,
        variant=record.variant,
        service_tier=record.service_tier,
        context_band=record.context_band,
        occurred_at=record.occurred_at,
        meter=meter,
        quantity=quantity,
        unit=Unit.TOKENS,
        measurement_quality=record.measurement_quality,
        semantics_version=record.semantics_version,
        assumptions=assumptions,
    )


def _unpriced(
    record: RawUsageRecord,
    model: str | None,
    meter: Meter | None,
    quantity: int,
    unit: Unit,
    reason: DiagnosticReason,
    assumptions: tuple[str, ...],
) -> UnpricedQuantity:
    return UnpricedQuantity(
        source_record_id=record.source_record_id,
        agent=record.agent,
        provider=record.provider,
        model=model,
        channel=record.channel,
        variant=record.variant,
        service_tier=record.service_tier,
        context_band=record.context_band,
        occurred_at=record.occurred_at,
        meter=meter,
        quantity=quantity,
        unit=unit,
        measurement_quality=record.measurement_quality,
        semantics_version=record.semantics_version,
        reason=reason,
        assumptions=assumptions,
    )


def _is_quantity(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _is_positive_amount(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0


def _native_platform_fees(record: RawUsageRecord):
    for source in _native_metadata_sources(record):
        for field in _NATIVE_PLATFORM_FEE_FIELDS:
            value = source.get(field)
            if value is not None:
                yield field, value


def _charged_cents_observed(record: RawUsageRecord) -> bool:
    return any(
        source.get("chargedCents") is not None
        or source.get("charged_cents") is not None
        for source in _native_metadata_sources(record)
    )


def _native_metadata_sources(
    record: RawUsageRecord,
) -> tuple[Mapping[str, object], ...]:
    sources: list[Mapping[str, object]] = [record.source]
    usage = record.source.get("usage")
    if isinstance(usage, Mapping):
        sources.append(usage)
    runs = record.source.get("runs")
    if isinstance(runs, tuple):
        for run in runs:
            if isinstance(run, Mapping):
                sources.append(run)
                run_usage = run.get("usage")
                if isinstance(run_usage, Mapping):
                    sources.append(run_usage)
    token_usage = record.source.get("tokenUsage")
    if isinstance(token_usage, Mapping):
        sources.append(token_usage)
    return tuple(sources)


def _is_auto(value: object) -> bool:
    return isinstance(value, str) and value.casefold() == "auto"


def _is_explicit(value: object) -> bool:
    return isinstance(value, str) and value.casefold() == "explicit"


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_exact_model(value: object) -> bool:
    return _is_identifier(value) and not _is_auto(value)

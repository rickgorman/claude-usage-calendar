"""Normalize request-scoped xAI/Grok usage without selecting a price."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from costs.contracts import (
    BillableQuantity,
    Diagnostic,
    DiagnosticReason,
    Meter,
    NormalizationResult,
    Provider,
    RawUsageRecord,
    Unit,
    UnpricedQuantity,
)

_CHAT_COMPLETIONS_SEMANTICS = frozenset(
    {"xai.chat-completions.v1", "xai.chat_completions.v1"}
)
_RESPONSES_SEMANTICS = frozenset({"xai.responses.v1"})
_GROK_CLI_AGGREGATE_SEMANTICS = frozenset({"xai.grok-cli.aggregate.v1"})
_GROK_CLI_API_EQUIVALENT_SEMANTICS = frozenset(
    {"xai.grok-cli.api-equivalent.v1"}
)
MODEL_LONG_CONTEXT_THRESHOLDS = MappingProxyType(
    {
        "grok-4.6": 200_000,
        "grok-test-1": 200_000,
        "grok-test-2": 200_000,
    }
)
_DIMENSION_REASONS = (
    ("model", DiagnosticReason.MISSING_MODEL),
    ("channel", DiagnosticReason.MISSING_CHANNEL),
    ("variant", DiagnosticReason.MISSING_VARIANT),
    ("service_tier", DiagnosticReason.MISSING_SERVICE_TIER),
)


def _is_token_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _usage_mapping(source: Mapping[str, object]) -> Mapping[str, object] | None:
    usage = source.get("usage")
    return usage if isinstance(usage, Mapping) else None


def _shape_values(
    usage: Mapping[str, object], shape: str
) -> tuple[object, object, object, tuple[tuple[Meter | None, int], ...], bool]:
    """Read one named native shape without treating another shape as equivalent."""

    if shape == "chat":
        total = usage.get("prompt_tokens")
        details = usage.get("prompt_tokens_details")
        output = usage.get("completion_tokens")
        keys = ("prompt_tokens", "prompt_tokens_details", "completion_tokens")
    else:
        total = usage.get("input_tokens")
        details = usage.get("input_tokens_details")
        output = usage.get("output_tokens")
        keys = ("input_tokens", "input_tokens_details", "output_tokens")
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
    visible = tuple(
        (meter, value)
        for meter, value in (
            (None, total),
            (Meter.INPUT_CACHE_READ, cached),
            (Meter.OUTPUT, output),
        )
        if _is_token_count(value)
    )
    return total, cached, output, visible, any(key in usage for key in keys)


def _all_native_visible(
    usage: Mapping[str, object],
) -> tuple[tuple[Meter | None, int], ...]:
    """Preserve all visible native values when an unsupported version cannot dispatch."""

    return _shape_values(usage, "chat")[3] + _shape_values(usage, "responses")[3]


def _native_usage(
    record: RawUsageRecord,
) -> tuple[
    tuple[tuple[Meter | None, int], ...],
    tuple[tuple[Meter, int], ...],
    int | None,
    DiagnosticReason | None,
]:
    """Read a native API usage object and derive uncached input from its total."""

    if record.semantics_version in _CHAT_COMPLETIONS_SEMANTICS:
        shape, foreign_shape = "chat", "responses"
    elif record.semantics_version in _RESPONSES_SEMANTICS:
        shape, foreign_shape = "responses", "chat"
    else:
        usage = _usage_mapping(record.source)
        visible = _all_native_visible(usage) if usage is not None else ()
        return visible, (), None, DiagnosticReason.UNSUPPORTED_SEMANTICS_VERSION

    usage = _usage_mapping(record.source)
    if usage is None:
        return (), (), None, DiagnosticReason.MISSING_REQUEST_TOTAL
    total, cached, output, visible, present = _shape_values(usage, shape)
    _, _, _, foreign_visible, foreign_present = _shape_values(usage, foreign_shape)
    if foreign_present:
        return (
            visible + foreign_visible,
            (),
            None,
            DiagnosticReason.SEMANTICS_INCONSISTENT,
        )
    if not present:
        return (), (), None, DiagnosticReason.MISSING_REQUEST_TOTAL
    details = usage.get(
        "prompt_tokens_details" if shape == "chat" else "input_tokens_details"
    )
    if details is None or (
        isinstance(details, Mapping) and "cached_tokens" not in details
    ):
        cached = 0
    if total is None:
        return visible, (), None, DiagnosticReason.MISSING_REQUEST_TOTAL
    if not all(_is_token_count(value) for value in (total, cached, output)):
        return visible, (), None, DiagnosticReason.MALFORMED_SOURCE_RECORD
    if cached > total:
        return (
            visible,
            (),
            None,
            DiagnosticReason.SEMANTICS_INCONSISTENT,
        )
    return (
        visible,
        (
            (Meter.INPUT_UNCACHED, total - cached),
            (Meter.INPUT_CACHE_READ, cached),
            (Meter.OUTPUT, output),
        ),
        total,
        None,
    )


def _aggregate_counters(
    record: RawUsageRecord,
) -> tuple[tuple[tuple[str, Meter | None, int], ...], tuple[str, ...]]:
    """Expose Grok CLI aggregate counters without treating them as a request."""

    counters: list[tuple[str, Meter | None, int]] = []
    malformed: list[str] = []
    for field_name, meter in (
        ("inputTokens", None),
        ("cachedReadTokens", Meter.INPUT_CACHE_READ),
        ("cacheCreationTokens", None),
        ("outputTokens", Meter.OUTPUT),
    ):
        if field_name not in record.source:
            continue
        value = record.source[field_name]
        if _is_token_count(value):
            counters.append((field_name, meter, value))
        else:
            malformed.append(field_name)
    return tuple(counters), tuple(malformed)


def _unpriced(
    record: RawUsageRecord,
    counters: tuple[tuple[Meter | None, int], ...],
    reason: DiagnosticReason,
) -> NormalizationResult:
    quantities = tuple(
        UnpricedQuantity(
            source_record_id=record.source_record_id,
            agent=record.agent,
            provider=record.provider,
            model=record.model,
            channel=record.channel,
            variant=record.variant,
            service_tier=record.service_tier,
            context_band=None,
            occurred_at=record.occurred_at,
            meter=meter,
            quantity=quantity,
            unit=Unit.TOKENS,
            measurement_quality=record.measurement_quality,
            semantics_version=record.semantics_version,
            reason=reason,
            assumptions=record.assumptions,
        )
        for meter, quantity in counters
    )
    return NormalizationResult(
        unpriced_quantities=quantities,
        diagnostics=(Diagnostic(reason, record.source_record_id),),
    )


def _reason_before_context(record: RawUsageRecord) -> DiagnosticReason | None:
    if record.provider is None:
        return DiagnosticReason.MISSING_PROVIDER
    if record.provider != Provider.XAI.value:
        return DiagnosticReason.UNKNOWN_PROVIDER
    for field_name, reason in _DIMENSION_REASONS:
        if getattr(record, field_name) is None:
            return reason
    return None


def _threshold(record: RawUsageRecord) -> int | None:
    """Resolve only verified exact-model metadata; there is no provider default."""

    return MODEL_LONG_CONTEXT_THRESHOLDS.get(record.model)


def _aggregate_result(
    record: RawUsageRecord,
    counters: tuple[tuple[str, Meter | None, int], ...],
) -> NormalizationResult:
    """Keep CLI aggregates visible without pretending their cache writes are priced."""

    quantities = tuple(
        UnpricedQuantity(
            source_record_id=record.source_record_id,
            agent=record.agent,
            provider=record.provider,
            model=record.model,
            channel=record.channel,
            variant=record.variant,
            service_tier=record.service_tier,
            context_band=None,
            occurred_at=record.occurred_at,
            meter=meter,
            quantity=quantity,
            unit=Unit.TOKENS,
            measurement_quality=record.measurement_quality,
            semantics_version=record.semantics_version,
            reason=(
                DiagnosticReason.UNSUPPORTED_METER
                if field_name == "cacheCreationTokens"
                else DiagnosticReason.MISSING_REQUEST_TOTAL
            ),
            assumptions=record.assumptions,
        )
        for field_name, meter, quantity in counters
    )
    reasons = {quantity.reason for quantity in quantities}
    return NormalizationResult(
        unpriced_quantities=quantities,
        diagnostics=tuple(
            Diagnostic(reason, record.source_record_id)
            for reason in sorted(reasons, key=lambda item: item.value)
        ),
    )


def _api_equivalent_result(
    record: RawUsageRecord,
    counters: tuple[tuple[str, Meter | None, int], ...],
) -> NormalizationResult:
    """Normalize one exact-model Grok CLI aggregate under its explicit estimate."""

    values = {field_name: quantity for field_name, _, quantity in counters}
    visible = tuple((meter, quantity) for _, meter, quantity in counters)
    if "inputTokens" not in values or "outputTokens" not in values:
        return _unpriced(record, visible, DiagnosticReason.MISSING_REQUEST_TOTAL)

    input_total = values["inputTokens"]
    cache_read = values.get("cachedReadTokens", 0)
    cache_creation = values.get("cacheCreationTokens", 0)
    uncached = input_total - cache_read - cache_creation
    if uncached < 0:
        return _unpriced(record, visible, DiagnosticReason.SEMANTICS_INCONSISTENT)

    quantities = tuple(
        BillableQuantity(
            source_record_id=record.source_record_id,
            agent=record.agent,
            provider=Provider.XAI,
            model=record.model,
            channel=record.channel,
            variant=record.variant,
            service_tier=record.service_tier,
            context_band="standard",
            occurred_at=record.occurred_at,
            meter=meter,
            quantity=quantity,
            unit=Unit.TOKENS,
            measurement_quality=record.measurement_quality,
            semantics_version=record.semantics_version,
            assumptions=record.assumptions,
        )
        for meter, quantity in (
            (Meter.INPUT_UNCACHED, uncached),
            (Meter.INPUT_CACHE_READ, cache_read),
            (Meter.OUTPUT, values["outputTokens"]),
        )
        if quantity > 0
    )
    if cache_creation <= 0:
        return NormalizationResult(quantities=quantities)

    unpriced = UnpricedQuantity(
        source_record_id=record.source_record_id,
        agent=record.agent,
        provider=record.provider,
        model=record.model,
        channel=record.channel,
        variant=record.variant,
        service_tier=record.service_tier,
        context_band="standard",
        occurred_at=record.occurred_at,
        meter=None,
        quantity=cache_creation,
        unit=Unit.TOKENS,
        measurement_quality=record.measurement_quality,
        semantics_version=record.semantics_version,
        reason=DiagnosticReason.UNSUPPORTED_METER,
        assumptions=record.assumptions,
    )
    return NormalizationResult(
        quantities=quantities,
        unpriced_quantities=(unpriced,),
        diagnostics=(
            Diagnostic(
                DiagnosticReason.UNSUPPORTED_METER,
                record.source_record_id,
                detail="Grok CLI cache creation has no explicit TTL meter",
            ),
        ),
    )


def _malformed_aggregate_result(
    record: RawUsageRecord,
    counters: tuple[tuple[str, Meter | None, int], ...],
    malformed_fields: tuple[str, ...],
) -> NormalizationResult:
    """Make invalid aggregate fields visible without inventing their token amounts."""

    valid_quantities = tuple(
        UnpricedQuantity(
            source_record_id=record.source_record_id,
            agent=record.agent,
            provider=record.provider,
            model=record.model,
            channel=record.channel,
            variant=record.variant,
            service_tier=record.service_tier,
            context_band=None,
            occurred_at=record.occurred_at,
            meter=meter,
            quantity=quantity,
            unit=Unit.TOKENS,
            measurement_quality=record.measurement_quality,
            semantics_version=record.semantics_version,
            reason=DiagnosticReason.MALFORMED_SOURCE_RECORD,
            assumptions=record.assumptions,
        )
        for _, meter, quantity in counters
    )
    markers = tuple(
        UnpricedQuantity(
            source_record_id=record.source_record_id,
            agent=record.agent,
            provider=record.provider,
            model=record.model,
            channel=record.channel,
            variant=record.variant,
            service_tier=record.service_tier,
            context_band=None,
            occurred_at=record.occurred_at,
            meter=None,
            quantity=1,
            unit=Unit.COUNT,
            measurement_quality=record.measurement_quality,
            semantics_version=record.semantics_version,
            reason=DiagnosticReason.MALFORMED_SOURCE_RECORD,
            assumptions=record.assumptions,
        )
        for _ in malformed_fields
    )
    return NormalizationResult(
        unpriced_quantities=valid_quantities + markers,
        diagnostics=(
            Diagnostic(
                DiagnosticReason.MALFORMED_SOURCE_RECORD, record.source_record_id
            ),
        ),
    )


def normalize(record: RawUsageRecord) -> NormalizationResult:
    """Convert one xAI request, classifying its context band before splitting input.

    Native API input totals include cached tokens.  A request-specific model threshold
    is also required; daily or CLI aggregate totals cannot select a context rate.
    """

    if record.semantics_version in (
        _GROK_CLI_AGGREGATE_SEMANTICS | _GROK_CLI_API_EQUIVALENT_SEMANTICS
    ):
        aggregate_counters, malformed_fields = _aggregate_counters(record)
        aggregate_visible = tuple(
            (meter, quantity) for _, meter, quantity in aggregate_counters
        )
        reason = _reason_before_context(record)
        if reason is not None:
            return _unpriced(record, aggregate_visible, reason)
        if malformed_fields:
            return _malformed_aggregate_result(
                record, aggregate_counters, malformed_fields
            )
        if record.semantics_version in _GROK_CLI_API_EQUIVALENT_SEMANTICS:
            return _api_equivalent_result(record, aggregate_counters)
        return _aggregate_result(record, aggregate_counters)

    visible, counters, total, native_reason = _native_usage(record)
    reason = _reason_before_context(record)
    if reason is not None:
        return _unpriced(record, visible, reason)
    if record.semantics_version not in (
        _CHAT_COMPLETIONS_SEMANTICS | _RESPONSES_SEMANTICS
    ):
        return _unpriced(
            record, visible, DiagnosticReason.UNSUPPORTED_SEMANTICS_VERSION
        )
    if native_reason is not None:
        return _unpriced(record, visible, native_reason)

    supplied_threshold = record.source.get("long_context_threshold_tokens")
    threshold = _threshold(record)
    if threshold is None:
        return _unpriced(record, visible, DiagnosticReason.MISSING_CONTEXT_BAND)
    if _is_token_count(supplied_threshold) and supplied_threshold != threshold:
        return _unpriced(record, visible, DiagnosticReason.SEMANTICS_INCONSISTENT)

    context_band = "long" if total >= threshold else "standard"
    return NormalizationResult(
        quantities=tuple(
            BillableQuantity(
                source_record_id=record.source_record_id,
                agent=record.agent,
                provider=Provider.XAI,
                model=record.model,
                channel=record.channel,
                variant=record.variant,
                service_tier=record.service_tier,
                context_band=context_band,
                occurred_at=record.occurred_at,
                meter=meter,
                quantity=quantity,
                unit=Unit.TOKENS,
                measurement_quality=record.measurement_quality,
                semantics_version=record.semantics_version,
                assumptions=record.assumptions,
            )
            for meter, quantity in counters
        )
    )

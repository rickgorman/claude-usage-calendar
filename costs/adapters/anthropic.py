"""Anthropic source-semantics adapter.

The Anthropic Messages usage fields are already disjoint: ``input_tokens`` is
uncached input, and cache reads and writes are reported independently.  This
adapter deliberately does not select rates or calculate money.
"""

from __future__ import annotations

from collections.abc import Mapping

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

_SEMANTICS_VERSION = "anthropic.v1"
_DIMENSION_REASONS = (
    ("model", DiagnosticReason.MISSING_MODEL),
    ("channel", DiagnosticReason.MISSING_CHANNEL),
    ("variant", DiagnosticReason.MISSING_VARIANT),
    ("service_tier", DiagnosticReason.MISSING_SERVICE_TIER),
    ("context_band", DiagnosticReason.MISSING_CONTEXT_BAND),
)


def _counter(source: Mapping[str, object], name: str) -> tuple[bool, int | None]:
    """Return whether *name* was supplied and its valid nonnegative integer."""

    if name not in source:
        return False, None
    value = source[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return True, None
    return True, value


def _unpriced(
    record: RawUsageRecord,
    meter: Meter | None,
    quantity: int,
    reason: DiagnosticReason,
) -> UnpricedQuantity:
    return UnpricedQuantity(
        source_record_id=record.source_record_id,
        agent=record.agent,
        provider=record.provider,
        model=record.model,
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
        reason=reason,
        assumptions=record.assumptions,
    )


def _billable(record: RawUsageRecord, meter: Meter, quantity: int) -> BillableQuantity:
    return BillableQuantity(
        source_record_id=record.source_record_id,
        agent=record.agent,
        provider=Provider.ANTHROPIC,
        model=record.model,
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
        assumptions=record.assumptions,
    )


def _record_reason(record: RawUsageRecord) -> DiagnosticReason | None:
    if record.provider is None:
        return DiagnosticReason.MISSING_PROVIDER
    if record.provider != Provider.ANTHROPIC:
        return DiagnosticReason.UNKNOWN_PROVIDER
    if record.semantics_version != _SEMANTICS_VERSION:
        return DiagnosticReason.UNSUPPORTED_SEMANTICS_VERSION
    for dimension, reason in _DIMENSION_REASONS:
        if getattr(record, dimension) is None:
            return reason
    return None


def normalize(record: RawUsageRecord) -> NormalizationResult:
    """Normalize one Anthropic usage record without aggregation or rate lookup.

    Detailed cache-write counters take precedence over the aggregate.  If
    the aggregate is larger, only the unallocated remainder is exposed as an
    unknown-TTL unpriced quantity; this avoids both double-counting and silently
    dropping visible usage.
    """

    source = record.source
    quantities: list[BillableQuantity] = []
    unpriced_quantities: list[UnpricedQuantity] = []
    diagnostics: list[Diagnostic] = []
    unavailable_reason = _record_reason(record)

    def diagnostic(reason: DiagnosticReason, meter: Meter | None = None) -> None:
        diagnostics.append(
            Diagnostic(
                reason=reason,
                source_record_id=record.source_record_id,
                meter=meter,
            )
        )

    def emit(
        meter: Meter, quantity: int, reason: DiagnosticReason | None = None
    ) -> None:
        if quantity == 0:
            return
        if reason is None:
            quantities.append(_billable(record, meter, quantity))
        else:
            unpriced_quantities.append(_unpriced(record, meter, quantity, reason))
            diagnostic(reason, meter)

    regular_counters = (
        ("input_tokens", Meter.INPUT_UNCACHED),
        ("cache_read_input_tokens", Meter.INPUT_CACHE_READ),
        ("output_tokens", Meter.OUTPUT),
    )
    detailed_counters = (
        ("ephemeral_5m_input_tokens", Meter.INPUT_CACHE_WRITE_5M),
        ("ephemeral_1h_input_tokens", Meter.INPUT_CACHE_WRITE_1H),
    )
    counters = {field: _counter(source, field) for field, _ in regular_counters}
    aggregate_present, aggregate = _counter(source, "cache_creation_input_tokens")
    cache_creation = source.get("cache_creation")
    cache_creation_present = "cache_creation" in source
    nested_counters: dict[str, tuple[bool, int | None]] = {}
    nested_malformed: list[Meter | None] = []
    unknown_nested_quantity = 0
    if cache_creation_present:
        if not isinstance(cache_creation, Mapping):
            nested_malformed.append(None)
        else:
            allowed_fields = {field for field, _ in detailed_counters}
            unknown_fields = set(cache_creation) - allowed_fields
            if unknown_fields:
                nested_malformed.append(None)
                for field in unknown_fields:
                    value = cache_creation[field]
                    if (
                        not isinstance(value, bool)
                        and isinstance(value, int)
                        and value >= 0
                    ):
                        unknown_nested_quantity += value
            nested_counters = {
                field: _counter(cache_creation, field) for field, _ in detailed_counters
            }

    legacy_detailed_counters = [
        (meter, value)
        for field, meter in detailed_counters
        for present, value in (_counter(source, field),)
        if present
    ]

    if (
        record.provider == Provider.ANTHROPIC
        and record.semantics_version == _SEMANTICS_VERSION
    ):
        malformed = []
        for field, meter in regular_counters:
            present, value = counters[field]
            if field in {"input_tokens", "output_tokens"} and not present:
                malformed.append((meter, "missing"))
            elif present and value is None:
                malformed.append((meter, "invalid"))
        for field, meter in detailed_counters:
            present, value = nested_counters.get(field, (False, None))
            if present and value is None:
                malformed.append((meter, "invalid"))
        if aggregate_present and aggregate is None:
            malformed.append((None, "invalid"))
        malformed.extend((meter, "invalid") for meter in nested_malformed)
        malformed.extend((meter, "legacy") for meter, _ in legacy_detailed_counters)

        if malformed:
            unavailable_reason = DiagnosticReason.MALFORMED_SOURCE_RECORD
            for meter, _ in malformed:
                diagnostic(DiagnosticReason.MALFORMED_SOURCE_RECORD, meter)
        elif aggregate is not None:
            detailed_values = [
                value
                for field, _ in detailed_counters
                for present, value in (nested_counters.get(field, (False, None)),)
                if present and value is not None
            ]
            if aggregate < sum(detailed_values):
                unavailable_reason = DiagnosticReason.SEMANTICS_INCONSISTENT

    if unavailable_reason is not None:
        for field, meter in (*regular_counters, *detailed_counters):
            if field in counters:
                present, value = counters[field]
            else:
                present, value = nested_counters.get(field, (False, None))
            if present and value is not None:
                emit(meter, value, unavailable_reason)
        for meter, value in legacy_detailed_counters:
            if value is not None:
                emit(meter, value, unavailable_reason)
        if unknown_nested_quantity:
            unpriced_quantities.append(
                _unpriced(
                    record,
                    None,
                    unknown_nested_quantity,
                    unavailable_reason,
                )
            )
        if aggregate is not None and aggregate != 0:
            unpriced_quantities.append(
                _unpriced(record, None, aggregate, unavailable_reason)
            )
        if unknown_nested_quantity or (aggregate is not None and aggregate != 0):
            diagnostic(unavailable_reason)
    else:
        for field, meter in regular_counters:
            _, value = counters[field]
            if value is not None:
                emit(meter, value)

        detailed_writes = [
            (meter, value)
            for field, meter in detailed_counters
            for present, value in (nested_counters.get(field, (False, None)),)
            if present and value is not None
        ]
        detailed_present = cache_creation_present
        for meter, quantity in detailed_writes:
            emit(meter, quantity)

        if aggregate is not None and aggregate != 0:
            if detailed_present:
                remainder = aggregate - sum(quantity for _, quantity in detailed_writes)
                if remainder > 0:
                    unpriced_quantities.append(
                        _unpriced(
                            record,
                            None,
                            remainder,
                            DiagnosticReason.UNKNOWN_CACHE_TTL,
                        )
                    )
                    diagnostic(DiagnosticReason.UNKNOWN_CACHE_TTL)
            else:
                unpriced_quantities.append(
                    _unpriced(
                        record,
                        None,
                        aggregate,
                        DiagnosticReason.UNKNOWN_CACHE_TTL,
                    )
                )
                diagnostic(DiagnosticReason.UNKNOWN_CACHE_TTL)

    return NormalizationResult(
        quantities=tuple(quantities),
        unpriced_quantities=tuple(unpriced_quantities),
        diagnostics=tuple(diagnostics),
    )

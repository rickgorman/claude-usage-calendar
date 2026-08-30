"""Normalize OpenAI/Codex inclusive token counters for exact-rate valuation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

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

SUPPORTED_SEMANTICS_VERSION = "openai.inclusive-input.v1"

_INPUT = "input_tokens"
_CACHE_READ = "cached_input_tokens"
_CACHE_WRITE = "cache_write_input_tokens"
_OUTPUT = "output_tokens"
_REASONING_OUTPUT = "reasoning_output_tokens"
_COUNTER_FIELDS = (
    _INPUT,
    _CACHE_READ,
    _CACHE_WRITE,
    _OUTPUT,
    _REASONING_OUTPUT,
)
_REQUIRED_COUNTER_FIELDS = frozenset({_INPUT, _OUTPUT})


@dataclass(frozen=True, slots=True)
class _Counters:
    values: dict[str, int]
    present: frozenset[str]
    invalid: tuple[str, ...]


def _read_counters(record: RawUsageRecord) -> _Counters:
    values: dict[str, int] = {}
    present = set()
    invalid = []
    for field in _COUNTER_FIELDS:
        if field not in record.source:
            values[field] = 0
            if field in _REQUIRED_COUNTER_FIELDS:
                invalid.append(field)
            continue
        present.add(field)
        value = record.source[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            invalid.append(field)
            continue
        values[field] = value
    return _Counters(values, frozenset(present), tuple(invalid))


def _diagnostic(
    record: RawUsageRecord,
    reason: DiagnosticReason,
    detail: str,
    meter: Meter | None = None,
) -> Diagnostic:
    return Diagnostic(reason, record.source_record_id, meter, detail)


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
    # Callers establish all exact dimensions before constructing a billable value.
    assert record.model is not None
    assert record.channel is not None
    assert record.variant is not None
    assert record.service_tier is not None
    assert record.context_band is not None
    return BillableQuantity(
        source_record_id=record.source_record_id,
        agent=record.agent,
        provider=Provider.OPENAI,
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


def _positive(
    items: Iterable[tuple[Meter | None, int]],
) -> tuple[tuple[Meter | None, int], ...]:
    return tuple((meter, quantity) for meter, quantity in items if quantity > 0)


def _raw_visible(counters: _Counters) -> tuple[tuple[Meter | None, int], ...]:
    """Preserve valid source counters without interpreting an invalid record."""

    values = counters.values
    items: list[tuple[Meter | None, int]] = []
    if _INPUT in values and _INPUT in counters.present:
        items.append((None, values[_INPUT]))
    if _CACHE_READ in values and _CACHE_READ in counters.present:
        items.append((Meter.INPUT_CACHE_READ, values[_CACHE_READ]))

    if _CACHE_WRITE in values and _CACHE_WRITE in counters.present:
        # V1 has no OpenAI-specific cache-write meter for this aggregate.
        items.append((None, values[_CACHE_WRITE]))

    if _OUTPUT in values and _OUTPUT in counters.present:
        items.append((Meter.OUTPUT, values[_OUTPUT]))
    # reasoning_output_tokens is a subset of output_tokens, never another charge.
    return _positive(items)


def _dimension_reason(record: RawUsageRecord) -> DiagnosticReason | None:
    if record.provider is None:
        return DiagnosticReason.MISSING_PROVIDER
    if record.provider != Provider.OPENAI.value:
        return DiagnosticReason.UNKNOWN_PROVIDER
    for value, reason in (
        (record.model, DiagnosticReason.MISSING_MODEL),
        (record.channel, DiagnosticReason.MISSING_CHANNEL),
        (record.variant, DiagnosticReason.MISSING_VARIANT),
        (record.service_tier, DiagnosticReason.MISSING_SERVICE_TIER),
        (record.context_band, DiagnosticReason.MISSING_CONTEXT_BAND),
    ):
        if value is None:
            return reason
    return None


def _whole_record_unpriced(
    record: RawUsageRecord,
    counters: _Counters,
    reason: DiagnosticReason,
    detail: str,
) -> NormalizationResult:
    unpriced = tuple(
        _unpriced(record, meter, quantity, reason)
        for meter, quantity in _raw_visible(counters)
    )
    return NormalizationResult(
        unpriced_quantities=unpriced,
        diagnostics=(_diagnostic(record, reason, detail),),
    )


def normalize(record: RawUsageRecord) -> NormalizationResult:
    """Convert one OpenAI inclusive-input record without selecting any rate."""

    counters = _read_counters(record)
    if record.semantics_version != SUPPORTED_SEMANTICS_VERSION:
        return _whole_record_unpriced(
            record,
            counters,
            DiagnosticReason.UNSUPPORTED_SEMANTICS_VERSION,
            f"unsupported OpenAI semantics version: {record.semantics_version}",
        )

    if counters.invalid:
        fields = ", ".join(counters.invalid)
        return _whole_record_unpriced(
            record,
            counters,
            DiagnosticReason.MALFORMED_SOURCE_RECORD,
            f"token counters must be nonnegative integers: {fields}",
        )

    values = counters.values
    cache_write = values[_CACHE_WRITE]
    ordinary_input = values[_INPUT] - values[_CACHE_READ] - cache_write
    reasoning_output = values[_REASONING_OUTPUT]
    if ordinary_input < 0:
        return _whole_record_unpriced(
            record,
            counters,
            DiagnosticReason.SEMANTICS_INCONSISTENT,
            "inclusive ordinary-input remainder is negative; entire record is unpriced",
        )
    if reasoning_output > values[_OUTPUT]:
        return _whole_record_unpriced(
            record,
            counters,
            DiagnosticReason.SEMANTICS_INCONSISTENT,
            "reasoning output exceeds inclusive output; entire record is unpriced",
        )

    components = _positive(
        (
            (Meter.INPUT_UNCACHED, ordinary_input),
            (Meter.INPUT_CACHE_READ, values[_CACHE_READ]),
            (Meter.OUTPUT, values[_OUTPUT]),
        )
    )
    dimension_reason = _dimension_reason(record)
    if dimension_reason is not None:
        detail = "record lacks a complete exact OpenAI rate key"
        unpriced_components = list(components)
        if cache_write > 0:
            unpriced_components.append((None, cache_write))
        return NormalizationResult(
            unpriced_quantities=tuple(
                _unpriced(record, meter, quantity, dimension_reason)
                for meter, quantity in unpriced_components
            ),
            diagnostics=(_diagnostic(record, dimension_reason, detail),),
        )

    quantities = tuple(
        _billable(record, meter, quantity)
        for meter, quantity in components
        if meter is not None
    )
    unpriced: tuple[UnpricedQuantity, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    if cache_write > 0:
        reason = DiagnosticReason.UNSUPPORTED_METER
        unpriced = (_unpriced(record, None, cache_write, reason),)
        diagnostics = (
            _diagnostic(
                record,
                reason,
                "OpenAI cache-write input has no supported v1 meter",
            ),
        )

    return NormalizationResult(quantities, unpriced, diagnostics)


__all__ = ["SUPPORTED_SEMANTICS_VERSION", "normalize"]

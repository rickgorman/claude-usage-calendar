"""Pure exact-key Decimal pricing and rollup engine.

The frozen formula is applied once per aggregated RateKey bucket:

    integer_token_quantity * Decimal(price_per_million_tokens) / 1_000_000

Rates are selected only by equality of the complete :class:`RateKey`.  Monetary
values remain ``Decimal`` throughout this module; display amounts are rounded
independently from exact component values and exact rollup sums.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from .contracts import (
    TOKENS_PER_MILLION,
    USD_JSON_QUANTUM,
    USD_ROUNDING,
    BillableQuantity,
    CostStatus,
    Diagnostic,
    DiagnosticReason,
    MeasurementQuality,
    RateKey,
    Unit,
    UnpricedQuantity,
)
from .pricing import PricingPack, PricingProvenance, RateCard

MAX_DIAGNOSTICS = 10_000


@dataclass(frozen=True, slots=True)
class CostRollup:
    status: CostStatus
    quality: MeasurementQuality
    total_usd: Decimal | None
    priced_subtotal_usd: Decimal


@dataclass(frozen=True, slots=True)
class PricedComponent:
    source_record_ids: tuple[str, ...]
    agent: str
    rate_key: RateKey
    occurred_on: date
    quantity: int
    unit: Unit
    price: Decimal
    exact_cost_usd: Decimal
    cost_usd: Decimal
    assumptions: tuple[str, ...] = ()
    occurred_hour: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.occurred_hour, bool) or not 0 <= self.occurred_hour <= 23:
            raise ValueError("occurred_hour must be an integer from 0 through 23")
        object.__setattr__(self, "source_record_ids", tuple(self.source_record_ids))
        object.__setattr__(self, "assumptions", tuple(self.assumptions))


@dataclass(frozen=True, slots=True)
class DailyCostRollup:
    occurred_on: date
    rollup: CostRollup


@dataclass(frozen=True, slots=True)
class HourlyCostRollup:
    occurred_on: date
    hour: int
    rollup: CostRollup

    def __post_init__(self) -> None:
        if isinstance(self.hour, bool) or not 0 <= self.hour <= 23:
            raise ValueError("hour must be an integer from 0 through 23")


@dataclass(frozen=True, slots=True)
class MonthlyCostRollup:
    month: str
    rollup: CostRollup


@dataclass(frozen=True, slots=True)
class YearlyCostRollup:
    year: str
    rollup: CostRollup


@dataclass(frozen=True, slots=True)
class AgentCostRollup:
    agent: str
    display_name: str
    rollup: CostRollup
    daily: tuple[DailyCostRollup, ...]
    hourly: tuple[HourlyCostRollup, ...] = ()
    monthly: tuple[MonthlyCostRollup, ...] = ()
    yearly: tuple[YearlyCostRollup, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "daily", tuple(self.daily))
        object.__setattr__(self, "hourly", tuple(self.hourly))
        object.__setattr__(self, "monthly", tuple(self.monthly))
        object.__setattr__(self, "yearly", tuple(self.yearly))


@dataclass(frozen=True, slots=True)
class CostResult:
    valuation_as_of: date
    provenance: PricingProvenance
    rollup: CostRollup
    components: tuple[PricedComponent, ...]
    unpriced_quantities: tuple[UnpricedQuantity, ...]
    diagnostics: tuple[Diagnostic, ...]
    agents: tuple[AgentCostRollup, ...]
    assumptions: tuple[str, ...] = ()
    hourly: tuple[HourlyCostRollup, ...] = ()
    daily: tuple[DailyCostRollup, ...] = ()
    monthly: tuple[MonthlyCostRollup, ...] = ()
    yearly: tuple[YearlyCostRollup, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        object.__setattr__(self, "unpriced_quantities", tuple(self.unpriced_quantities))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "agents", tuple(self.agents))
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        object.__setattr__(self, "hourly", tuple(self.hourly))
        object.__setattr__(self, "daily", tuple(self.daily))
        object.__setattr__(self, "monthly", tuple(self.monthly))
        object.__setattr__(self, "yearly", tuple(self.yearly))


def price_quantities(
    quantities: Sequence[BillableQuantity],
    unpriced_quantities: Sequence[UnpricedQuantity],
    pricing_pack: PricingPack,
    diagnostics: Sequence[Diagnostic] = (),
) -> CostResult:
    """Price normalized usage without mutating it or applying rate fallbacks."""

    source_quantities = tuple(quantities)
    all_unpriced = list(unpriced_quantities)
    lookup_diagnostics: list[Diagnostic] = []
    rates = _index_rates(pricing_pack.rates)

    buckets: dict[
        tuple[str, date, int, RateKey],
        tuple[int, list[str], set[str]],
    ] = {}
    for quantity in source_quantities:
        rate_key = _rate_key(quantity, pricing_pack.valuation_as_of)
        matching_rates = rates.get(rate_key, ())
        if len(matching_rates) != 1:
            reason = (
                DiagnosticReason.RATE_AMBIGUOUS
                if matching_rates
                else DiagnosticReason.RATE_NOT_FOUND
            )
            all_unpriced.append(_as_unpriced(quantity, reason))
            lookup_diagnostics.append(
                Diagnostic(
                    reason=reason,
                    source_record_id=quantity.source_record_id,
                    meter=quantity.meter,
                )
            )
            continue

        bucket_key = (
            quantity.agent,
            quantity.occurred_at.date(),
            quantity.occurred_at.hour,
            rate_key,
        )
        if bucket_key not in buckets:
            buckets[bucket_key] = (0, [], set())
        total, source_record_ids, assumptions = buckets[bucket_key]
        source_record_ids.append(quantity.source_record_id)
        assumptions.update(quantity.assumptions)
        buckets[bucket_key] = (
            total + quantity.quantity,
            source_record_ids,
            assumptions,
        )

    components = tuple(
        _component(bucket_key, bucket, rates[bucket_key[3]][0])
        for bucket_key, bucket in sorted(buckets.items(), key=_bucket_sort_key)
    )
    ordered_unpriced = tuple(sorted(all_unpriced, key=_unpriced_sort_key))
    merged_diagnostics, truncation_assumption = _merge_diagnostics(
        tuple(diagnostics), tuple(lookup_diagnostics)
    )
    agents = _agent_rollups(components, ordered_unpriced)
    hourly, daily, monthly, yearly = _scope_rollups(components, ordered_unpriced)
    result_assumptions = {
        assumption
        for item in (*components, *ordered_unpriced)
        for assumption in item.assumptions
    }
    if truncation_assumption is not None:
        result_assumptions.add(truncation_assumption)

    return CostResult(
        valuation_as_of=pricing_pack.valuation_as_of,
        provenance=pricing_pack.provenance,
        rollup=_rollup(components, ordered_unpriced),
        components=components,
        unpriced_quantities=ordered_unpriced,
        diagnostics=merged_diagnostics,
        agents=agents,
        assumptions=tuple(sorted(result_assumptions)),
        hourly=hourly,
        daily=daily,
        monthly=monthly,
        yearly=yearly,
    )


def _index_rates(rates: Sequence[RateCard]) -> dict[RateKey, tuple[RateCard, ...]]:
    indexed: dict[RateKey, list[RateCard]] = {}
    for rate in rates:
        indexed.setdefault(rate.key, []).append(rate)
    return {key: tuple(matches) for key, matches in indexed.items()}


def _rate_key(quantity: BillableQuantity, valuation_date: date) -> RateKey:
    return RateKey(
        provider=quantity.provider,
        model=quantity.model,
        channel=quantity.channel,
        variant=quantity.variant,
        service_tier=quantity.service_tier,
        context_band=quantity.context_band,
        meter=quantity.meter,
        valuation_date=valuation_date,
        measurement_quality=quantity.measurement_quality,
    )


def _as_unpriced(
    quantity: BillableQuantity, reason: DiagnosticReason
) -> UnpricedQuantity:
    return UnpricedQuantity(
        source_record_id=quantity.source_record_id,
        agent=quantity.agent,
        provider=quantity.provider,
        model=quantity.model,
        channel=quantity.channel,
        variant=quantity.variant,
        service_tier=quantity.service_tier,
        context_band=quantity.context_band,
        occurred_at=quantity.occurred_at,
        meter=quantity.meter,
        quantity=quantity.quantity,
        unit=quantity.unit,
        measurement_quality=quantity.measurement_quality,
        semantics_version=quantity.semantics_version,
        reason=reason,
        assumptions=quantity.assumptions,
    )


def _bucket_sort_key(
    item: tuple[tuple[str, date, int, RateKey], object],
) -> tuple[str, ...]:
    agent, occurred_on, occurred_hour, key = item[0]
    return (
        agent,
        occurred_on.isoformat(),
        f"{occurred_hour:02}",
        key.provider.value,
        key.model,
        key.channel,
        key.variant,
        key.service_tier,
        key.context_band,
        key.meter.value,
        key.valuation_date.isoformat(),
        key.measurement_quality.value,
    )


def _unpriced_sort_key(quantity: UnpricedQuantity) -> tuple[object, ...]:
    return (
        quantity.agent,
        quantity.occurred_at.date().isoformat(),
        quantity.provider or "",
        quantity.model or "",
        quantity.channel or "",
        quantity.variant or "",
        quantity.service_tier or "",
        quantity.context_band or "",
        quantity.meter.value if quantity.meter is not None else "",
        quantity.measurement_quality.value,
        quantity.reason.value,
        quantity.source_record_id,
        quantity.quantity,
        quantity.unit.value,
        quantity.semantics_version,
        quantity.assumptions,
    )


def _component(
    bucket_key: tuple[str, date, int, RateKey],
    bucket: tuple[int, list[str], set[str]],
    rate: RateCard,
) -> PricedComponent:
    agent, occurred_on, occurred_hour, rate_key = bucket_key
    quantity, source_record_ids, assumptions = bucket
    exact_cost = _exact_cost(quantity, rate.price)
    return PricedComponent(
        source_record_ids=tuple(sorted(set(source_record_ids))),
        agent=agent,
        rate_key=rate_key,
        occurred_on=occurred_on,
        quantity=quantity,
        unit=Unit.TOKENS,
        price=rate.price,
        exact_cost_usd=exact_cost,
        cost_usd=_quantize(exact_cost),
        assumptions=tuple(sorted(assumptions)),
        occurred_hour=occurred_hour,
    )


def _exact_cost(quantity: int, price: Decimal) -> Decimal:
    # Multiplication needs enough local precision to remain independent of the
    # process-wide Decimal context. Division by 10**6 is terminating.
    precision = max(50, len(str(max(1, quantity))) + len(price.as_tuple().digits) + 2)
    with localcontext() as context:
        context.prec = precision
        return Decimal(quantity) * price / Decimal(TOKENS_PER_MILLION)


def _quantize(value: Decimal) -> Decimal:
    integer_digits = max(1, value.adjusted() + 1) if value else 1
    with localcontext() as context:
        context.prec = max(50, integer_digits + 7)
        return value.quantize(USD_JSON_QUANTUM, rounding=USD_ROUNDING)


def _rounded_sum(values: Iterable[Decimal]) -> Decimal:
    exact_values = tuple(values)
    if not exact_values:
        return Decimal(0).quantize(USD_JSON_QUANTUM)

    nonzero = tuple(value for value in exact_values if value)
    if not nonzero:
        return Decimal(0).quantize(USD_JSON_QUANTUM)
    minimum_exponent = min(value.as_tuple().exponent for value in nonzero)
    maximum_adjusted = max(value.adjusted() for value in nonzero)
    carry_digits = len(str(len(nonzero)))
    with localcontext() as context:
        context.prec = max(
            50,
            maximum_adjusted - minimum_exponent + carry_digits + 2,
        )
        exact_sum = sum(nonzero, Decimal(0))
        return exact_sum.quantize(USD_JSON_QUANTUM, rounding=USD_ROUNDING)


def _quality(
    components: Sequence[PricedComponent],
    unpriced: Sequence[UnpricedQuantity],
) -> MeasurementQuality:
    qualities = {
        *[component.rate_key.measurement_quality for component in components],
        *[quantity.measurement_quality for quantity in unpriced],
    }
    if not qualities:
        # The frozen result type has no unknown/empty quality.  This neutral
        # value is observable only on a disabled, usage-free result.
        return MeasurementQuality.PROVIDER_REPORTED
    if len(qualities) == 1:
        return next(iter(qualities))
    return MeasurementQuality.MIXED


def _rollup(
    components: Sequence[PricedComponent],
    unpriced: Sequence[UnpricedQuantity],
) -> CostRollup:
    subtotal = _rounded_sum(component.exact_cost_usd for component in components)
    if components and unpriced:
        status = CostStatus.PARTIAL
    elif components:
        status = CostStatus.COMPLETE
    elif unpriced:
        status = CostStatus.UNPRICED
    else:
        # Calling the engine means pricing is enabled and a pack is present.
        # DISABLED is represented outside the engine by an absent CostResult.
        status = CostStatus.COMPLETE
    return CostRollup(
        status=status,
        quality=_quality(components, unpriced),
        total_usd=subtotal if status is CostStatus.COMPLETE else None,
        priced_subtotal_usd=subtotal,
    )


def _diagnostic_sort_key(diagnostic: Diagnostic) -> tuple[str, ...]:
    return (
        diagnostic.source_record_id,
        diagnostic.reason.value,
        diagnostic.meter.value if diagnostic.meter is not None else "",
        diagnostic.detail or "",
    )


def _merge_diagnostics(
    adapter_diagnostics: Sequence[Diagnostic],
    lookup_diagnostics: Sequence[Diagnostic],
) -> tuple[tuple[Diagnostic, ...], str | None]:
    # Exact duplicate adapter diagnostics collapse, but distinct details remain.
    # A lookup diagnostic is redundant when an adapter diagnostic already covers
    # the same bounded source/reason/meter identity; in that case the adapter's
    # more informative detail wins.
    unique: dict[tuple[str, ...], Diagnostic] = {}
    adapter_identities: set[tuple[str, DiagnosticReason, object]] = set()
    for diagnostic in adapter_diagnostics:
        unique.setdefault(_diagnostic_sort_key(diagnostic), diagnostic)
        adapter_identities.add(
            (diagnostic.source_record_id, diagnostic.reason, diagnostic.meter)
        )
    for diagnostic in lookup_diagnostics:
        identity = (
            diagnostic.source_record_id,
            diagnostic.reason,
            diagnostic.meter,
        )
        if identity not in adapter_identities:
            unique.setdefault(_diagnostic_sort_key(diagnostic), diagnostic)

    ordered = tuple(unique[key] for key in sorted(unique))
    if len(ordered) <= MAX_DIAGNOSTICS:
        return ordered, None
    assumption = (
        f"diagnostics truncated: kept {MAX_DIAGNOSTICS} of {len(ordered)} "
        "deterministic entries; inspect unpriced components for complete "
        "unsupported-usage coverage"
    )
    return ordered[:MAX_DIAGNOSTICS], assumption


def _agent_rollups(
    components: Sequence[PricedComponent],
    unpriced: Sequence[UnpricedQuantity],
) -> tuple[AgentCostRollup, ...]:
    components_by_agent: dict[str, list[PricedComponent]] = {}
    unpriced_by_agent: dict[str, list[UnpricedQuantity]] = {}
    dates_by_agent: dict[str, set[date]] = {}

    for component in components:
        components_by_agent.setdefault(component.agent, []).append(component)
        dates_by_agent.setdefault(component.agent, set()).add(component.occurred_on)
    for quantity in unpriced:
        unpriced_by_agent.setdefault(quantity.agent, []).append(quantity)
        occurred_on = quantity.occurred_at.date()
        dates_by_agent.setdefault(quantity.agent, set()).add(occurred_on)

    result = []
    for agent in sorted(dates_by_agent):
        agent_components = tuple(components_by_agent.get(agent, ()))
        agent_unpriced = tuple(unpriced_by_agent.get(agent, ()))
        hourly, daily, monthly, yearly = _scope_rollups(
            agent_components, agent_unpriced
        )
        result.append(
            AgentCostRollup(
                agent=agent,
                display_name=agent,
                rollup=_rollup(agent_components, agent_unpriced),
                daily=daily,
                hourly=hourly,
                monthly=monthly,
                yearly=yearly,
            )
        )
    return tuple(result)


def _scope_rollups(
    components: Sequence[PricedComponent],
    unpriced: Sequence[UnpricedQuantity],
) -> tuple[
    tuple[HourlyCostRollup, ...],
    tuple[DailyCostRollup, ...],
    tuple[MonthlyCostRollup, ...],
    tuple[YearlyCostRollup, ...],
]:
    """Build every display scope from the same exact backend components."""

    def grouped_rollup(component_key, unpriced_key):
        priced_groups: dict[object, list[PricedComponent]] = {}
        unpriced_groups: dict[object, list[UnpricedQuantity]] = {}
        for component in components:
            priced_groups.setdefault(component_key(component), []).append(component)
        for quantity in unpriced:
            unpriced_groups.setdefault(unpriced_key(quantity), []).append(quantity)
        return {
            key: _rollup(priced_groups.get(key, ()), unpriced_groups.get(key, ()))
            for key in sorted(set(priced_groups) | set(unpriced_groups))
        }

    hourly_values = grouped_rollup(
        lambda item: (item.occurred_on, item.occurred_hour),
        lambda item: (item.occurred_at.date(), item.occurred_at.hour),
    )
    daily_values = grouped_rollup(
        lambda item: item.occurred_on,
        lambda item: item.occurred_at.date(),
    )
    monthly_values = grouped_rollup(
        lambda item: item.occurred_on.strftime("%Y-%m"),
        lambda item: item.occurred_at.strftime("%Y-%m"),
    )
    yearly_values = grouped_rollup(
        lambda item: item.occurred_on.strftime("%Y"),
        lambda item: item.occurred_at.strftime("%Y"),
    )
    return (
        tuple(
            HourlyCostRollup(occurred_on=day, hour=hour, rollup=rollup)
            for (day, hour), rollup in hourly_values.items()
        ),
        tuple(
            DailyCostRollup(occurred_on=day, rollup=rollup)
            for day, rollup in daily_values.items()
        ),
        tuple(
            MonthlyCostRollup(month=month, rollup=rollup)
            for month, rollup in monthly_values.items()
        ),
        tuple(
            YearlyCostRollup(year=year, rollup=rollup)
            for year, rollup in yearly_values.items()
        ),
    )


__all__ = [
    "AgentCostRollup",
    "CostResult",
    "CostRollup",
    "DailyCostRollup",
    "HourlyCostRollup",
    "MonthlyCostRollup",
    "PricedComponent",
    "YearlyCostRollup",
    "price_quantities",
]

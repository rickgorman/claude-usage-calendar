"""Lazy public API for feature-gated cost estimation.

Importing :mod:`costs` or :mod:`costs.cli` must not load pricing, engine,
serialization, or dashboard modules on the costs-disabled path.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "CostCliConfig": ("cli", "CostCliConfig"),
    "configure_cost_cli": ("cli", "configure_cost_cli"),
    "COSTS_ENABLED_BY_DEFAULT": ("contracts", "COSTS_ENABLED_BY_DEFAULT"),
    "COST_ESTIMATES_SCHEMA_VERSION": ("contracts", "COST_ESTIMATES_SCHEMA_VERSION"),
    "CURRENCY": ("contracts", "CURRENCY"),
    "PRICING_PACK_SCHEMA_VERSION": ("contracts", "PRICING_PACK_SCHEMA_VERSION"),
    "TOKENS_PER_MILLION": ("contracts", "TOKENS_PER_MILLION"),
    "VALUATION_BASIS": ("contracts", "VALUATION_BASIS"),
    "BillableQuantity": ("contracts", "BillableQuantity"),
    "ContractViolation": ("contracts", "ContractViolation"),
    "CostStatus": ("contracts", "CostStatus"),
    "Diagnostic": ("contracts", "Diagnostic"),
    "DiagnosticReason": ("contracts", "DiagnosticReason"),
    "JsonObject": ("contracts", "JsonObject"),
    "MeasurementQuality": ("contracts", "MeasurementQuality"),
    "Meter": ("contracts", "Meter"),
    "NormalizationResult": ("contracts", "NormalizationResult"),
    "Provider": ("contracts", "Provider"),
    "RateKey": ("contracts", "RateKey"),
    "RateUnit": ("contracts", "RateUnit"),
    "RawUsageRecord": ("contracts", "RawUsageRecord"),
    "UnpricedQuantity": ("contracts", "UnpricedQuantity"),
    "Unit": ("contracts", "Unit"),
    "UsageNormalizer": ("contracts", "UsageNormalizer"),
    "validate_cost_estimates": ("contracts", "validate_cost_estimates"),
    "DashboardOutput": ("dashboard", "DashboardOutput"),
    "SafeHtmlFragment": ("dashboard", "SafeHtmlFragment"),
    "render_cost_dashboard": ("dashboard", "render_cost_dashboard"),
    "AgentCostRollup": ("engine", "AgentCostRollup"),
    "CostResult": ("engine", "CostResult"),
    "CostRollup": ("engine", "CostRollup"),
    "DailyCostRollup": ("engine", "DailyCostRollup"),
    "HourlyCostRollup": ("engine", "HourlyCostRollup"),
    "MonthlyCostRollup": ("engine", "MonthlyCostRollup"),
    "PricedComponent": ("engine", "PricedComponent"),
    "YearlyCostRollup": ("engine", "YearlyCostRollup"),
    "price_quantities": ("engine", "price_quantities"),
    "PricingLoadOptions": ("pricing", "PricingLoadOptions"),
    "PricingLoadResult": ("pricing", "PricingLoadResult"),
    "PricingPack": ("pricing", "PricingPack"),
    "PricingPackKind": ("pricing", "PricingPackKind"),
    "PricingProvenance": ("pricing", "PricingProvenance"),
    "PricingSource": ("pricing", "PricingSource"),
    "RateCard": ("pricing", "RateCard"),
    "load_pricing_pack": ("pricing", "load_pricing_pack"),
    "serialize_cost_estimates": ("serializer", "serialize_cost_estimates"),
}

__all__ = sorted(_EXPORTS)  # noqa: PLE0605 - kept in lockstep with lazy map


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(f".{module_name}", __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})

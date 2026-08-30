"""Argument registration and typed resolution for optional cost controls."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CostCliConfig:
    enabled: bool = False
    pricing_path: Path | None = None
    require_complete_pricing: bool = False
    explain_costs: bool = False


class CostCliError(ValueError):
    """A bounded usage error suitable for routing through ``parser.error``."""


def configure_cost_cli(parser: ArgumentParser) -> None:
    """Register workstream-A controls without parsing or loading pricing."""

    cost_gate = parser.add_mutually_exclusive_group()
    cost_gate.add_argument(
        "--costs",
        dest="costs",
        action="store_true",
        help="Enable published-rate-equivalent cost estimates",
    )
    cost_gate.add_argument(
        "--no-costs",
        dest="costs",
        action="store_false",
        help="Disable cost estimates (the default)",
    )
    parser.set_defaults(costs=None)
    parser.add_argument(
        "--pricing",
        type=Path,
        metavar="PATH",
        help="Use a strict custom pricing pack; implies --costs",
    )
    parser.add_argument(
        "--require-complete-pricing",
        action="store_true",
        help="Fail when any visible usage cannot be priced; implies --costs",
    )
    parser.add_argument(
        "--explain-costs",
        action="store_true",
        help="Include bounded pricing diagnostics; implies --costs",
    )


def resolve_cost_cli(namespace: Namespace) -> CostCliConfig:
    """Resolve implications and order-independent conflicts into a frozen config."""

    costs = getattr(namespace, "costs", None)
    pricing = getattr(namespace, "pricing", None)
    require_complete = getattr(namespace, "require_complete_pricing", False)
    explain = getattr(namespace, "explain_costs", False)
    if costs is not None and type(costs) is not bool:
        raise CostCliError("invalid cost option state")
    if type(require_complete) is not bool or type(explain) is not bool:
        raise CostCliError("invalid cost option state")
    if pricing is not None and not isinstance(pricing, (str, Path)):
        raise CostCliError("invalid pricing path")
    pricing_path = Path(pricing) if pricing is not None else None
    implied = pricing_path is not None or require_complete or explain
    if costs is False and implied:
        raise CostCliError(
            "--no-costs conflicts with --pricing, --require-complete-pricing, and --explain-costs"
        )
    return CostCliConfig(
        enabled=costs is True or implied,
        pricing_path=pricing_path,
        require_complete_pricing=require_complete,
        explain_costs=explain,
    )


__all__ = [
    "CostCliConfig",
    "CostCliError",
    "configure_cost_cli",
    "resolve_cost_cli",
]

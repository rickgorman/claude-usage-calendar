"""Strict, startup-only loading for immutable pricing packs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import (
    CURRENCY,
    FORBIDDEN_MAPPING_KEYS,
    MAX_IDENTIFIER_LENGTH,
    PRICING_PACK_SCHEMA_VERSION,
    MeasurementQuality,
    Meter,
    Provider,
    RateKey,
    RateUnit,
)

MAX_PRICING_PACK_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_RATES = 10_000
MAX_LABEL_LENGTH = 512
MAX_URL_LENGTH = 2048
MAX_PRICE_LENGTH = 64
MAX_PRICE_SCALE = 12
BUILTIN_PRICING_PACK_PATH = (
    Path(__file__).with_name("pricing_packs") / "builtin-2026-08-29.json"
)

_IDENTIFIER_PATTERN = re.compile(r"^[^*?\[\]{}()|^$\\]+$")
_PRICE_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(?:\.([0-9]{1,12}))?$")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "pack_id",
        "display_name",
        "currency",
        "valuation_as_of",
        "rates",
    }
)
_RATE_FIELDS = frozenset(
    {
        "provider",
        "model",
        "channel",
        "variant",
        "service_tier",
        "context_band",
        "meter",
        "valuation_date",
        "measurement_quality",
        "unit",
        "price",
        "source",
    }
)
_SOURCE_FIELDS = frozenset({"title", "url", "checked_at"})
_RATE_QUALITIES = frozenset(
    {
        MeasurementQuality.PROVIDER_REPORTED.value,
        MeasurementQuality.DERIVED.value,
        MeasurementQuality.ESTIMATED.value,
    }
)


class PricingPackError(ValueError):
    """A bounded validation error for an untrusted pricing pack."""


class PricingPackKind(str, Enum):
    BUILTIN = "builtin"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class PricingSource:
    title: str
    url: str
    checked_at: date


@dataclass(frozen=True, slots=True)
class RateCard:
    key: RateKey
    unit: RateUnit
    price: Decimal
    source: PricingSource


@dataclass(frozen=True, slots=True)
class PricingProvenance:
    pack_id: str
    kind: PricingPackKind
    sha256: str


@dataclass(frozen=True, slots=True)
class PricingPack:
    display_name: str
    valuation_as_of: date
    rates: tuple[RateCard, ...]
    provenance: PricingProvenance
    schema_version: int = PRICING_PACK_SCHEMA_VERSION
    currency: str = CURRENCY

    def __post_init__(self) -> None:
        object.__setattr__(self, "rates", tuple(self.rates))


@dataclass(frozen=True, slots=True)
class PricingLoadOptions:
    path: Path | None = None
    use_builtin: bool = True


@dataclass(frozen=True, slots=True)
class PricingLoadResult:
    pack: PricingPack


def _read_once(path: Path) -> bytes:
    try:
        with path.open("rb") as pack_file:
            raw = pack_file.read(MAX_PRICING_PACK_BYTES + 1)
    except (OSError, ValueError) as error:
        raise PricingPackError("pricing pack could not be read") from error
    if len(raw) > MAX_PRICING_PACK_BYTES:
        raise PricingPackError("pricing pack exceeds the 1 MiB limit")
    return raw


def _check_json_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise PricingPackError("pricing pack exceeds the JSON depth limit")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                break


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PricingPackError("pricing pack contains a duplicate JSON name")
        if key in FORBIDDEN_MAPPING_KEYS:
            raise PricingPackError("pricing pack contains a forbidden mapping key")
        result[key] = value
    return result


def _bounded_integer(value: str) -> int:
    if len(value.lstrip("-")) > 16:
        raise PricingPackError("pricing pack contains an oversized integer")
    return int(value)


def _reject_float(_: str) -> object:
    raise PricingPackError("pricing pack numbers must be bounded integers")


def _reject_constant(_: str) -> object:
    raise PricingPackError("pricing pack contains a non-finite number")


def _decode_json(raw: bytes) -> Mapping[str, object]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise PricingPackError("pricing pack must be UTF-8") from error
    _check_json_depth(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_int=_bounded_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except PricingPackError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PricingPackError("pricing pack is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise PricingPackError("pricing pack root must be an object")
    return value


def _require_fields(
    value: Mapping[str, object], expected: frozenset[str], name: str
) -> None:
    if frozenset(value) != expected:
        raise PricingPackError(f"{name} has missing or unknown fields")


def _require_string(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PricingPackError(f"{name} must be a bounded non-empty string")
    return value


def _require_identifier(value: object, name: str) -> str:
    identifier = _require_string(value, name, MAX_IDENTIFIER_LENGTH)
    if identifier in FORBIDDEN_MAPPING_KEYS or not _IDENTIFIER_PATTERN.fullmatch(
        identifier
    ):
        raise PricingPackError(f"{name} must be an exact non-pattern identifier")
    return identifier


def _require_date(value: object, name: str) -> date:
    text = _require_string(value, name, 10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise PricingPackError(f"{name} must be a canonical ISO date") from error
    if parsed.isoformat() != text:
        raise PricingPackError(f"{name} must be a canonical ISO date")
    return parsed


def _require_url(value: object) -> str:
    url = _require_string(value, "source.url", MAX_URL_LENGTH)
    if any(ord(character) < 0x20 for character in url):
        raise PricingPackError("source.url must be a bounded absolute URI")
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise PricingPackError("source.url must be a bounded absolute URI") from error
    if not parsed.scheme:
        raise PricingPackError("source.url must be a bounded absolute URI")
    return url


def _require_price(value: object) -> Decimal:
    if not isinstance(value, str) or len(value) > MAX_PRICE_LENGTH:
        raise PricingPackError("rate.price must be a bounded decimal string")
    match = _PRICE_PATTERN.fullmatch(value)
    if match is None or len(match.group(2) or "") > MAX_PRICE_SCALE:
        raise PricingPackError("rate.price must be a bounded decimal string")
    return Decimal(value)


def _enum_member(enum_type: type[Enum], value: object, name: str) -> Enum:
    if not isinstance(value, str):
        raise PricingPackError(f"{name} is not supported")
    try:
        return enum_type(value)
    except ValueError as error:
        raise PricingPackError(f"{name} is not supported") from error


def _build_source(value: object) -> PricingSource:
    if not isinstance(value, Mapping):
        raise PricingPackError("rate.source must be an object")
    _require_fields(value, _SOURCE_FIELDS, "rate.source")
    return PricingSource(
        title=_require_string(value["title"], "source.title", MAX_LABEL_LENGTH),
        url=_require_url(value["url"]),
        checked_at=_require_date(value["checked_at"], "source.checked_at"),
    )


def _build_rate(value: object) -> RateCard:
    if not isinstance(value, Mapping):
        raise PricingPackError("each pricing rate must be an object")
    _require_fields(value, _RATE_FIELDS, "rate")
    quality_text = value["measurement_quality"]
    if not isinstance(quality_text, str) or quality_text not in _RATE_QUALITIES:
        raise PricingPackError("rate.measurement_quality is not supported")
    unit = _enum_member(RateUnit, value["unit"], "rate.unit")
    key = RateKey(
        provider=_enum_member(Provider, value["provider"], "rate.provider"),  # type: ignore[arg-type]
        model=_require_identifier(value["model"], "rate.model"),
        channel=_require_identifier(value["channel"], "rate.channel"),
        variant=_require_identifier(value["variant"], "rate.variant"),
        service_tier=_require_identifier(value["service_tier"], "rate.service_tier"),
        context_band=_require_identifier(value["context_band"], "rate.context_band"),
        meter=_enum_member(Meter, value["meter"], "rate.meter"),  # type: ignore[arg-type]
        valuation_date=_require_date(value["valuation_date"], "rate.valuation_date"),
        measurement_quality=MeasurementQuality(quality_text),
    )
    return RateCard(
        key=key,
        unit=unit,  # type: ignore[arg-type]
        price=_require_price(value["price"]),
        source=_build_source(value["source"]),
    )


def _build_pack(
    document: Mapping[str, object], raw: bytes, kind: PricingPackKind
) -> PricingPack:
    _require_fields(document, _TOP_LEVEL_FIELDS, "pricing pack")
    schema_version = document["schema_version"]
    if type(schema_version) is not int or schema_version != PRICING_PACK_SCHEMA_VERSION:
        raise PricingPackError("pricing pack schema version is not supported")
    if document["currency"] != CURRENCY:
        raise PricingPackError("pricing pack currency is not supported")
    raw_rates = document["rates"]
    if not isinstance(raw_rates, list) or len(raw_rates) > MAX_RATES:
        raise PricingPackError("pricing pack rates must be a bounded array")
    valuation_as_of = _require_date(document["valuation_as_of"], "valuation_as_of")
    rates = tuple(_build_rate(value) for value in raw_rates)
    if any(rate.key.valuation_date != valuation_as_of for rate in rates):
        raise PricingPackError(
            "every rate valuation_date must equal the pricing pack valuation_as_of"
        )
    rate_keys = {rate.key for rate in rates}
    if len(rate_keys) != len(rates):
        raise PricingPackError(
            "pricing pack contains duplicate or overlapping RateKeys"
        )
    pack_id = _require_identifier(document["pack_id"], "pricing pack id")
    return PricingPack(
        display_name=_require_string(
            document["display_name"], "pricing pack display name", MAX_LABEL_LENGTH
        ),
        valuation_as_of=valuation_as_of,
        rates=rates,
        provenance=PricingProvenance(
            pack_id=pack_id,
            kind=kind,
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
    )


def load_pricing_pack(options: PricingLoadOptions) -> PricingLoadResult:
    """Read and validate exactly one replacement pricing pack at startup."""

    if not isinstance(options, PricingLoadOptions):
        raise TypeError("options must be PricingLoadOptions")
    if not isinstance(options.use_builtin, bool):
        raise TypeError("use_builtin must be a bool")
    if options.path is not None and not isinstance(options.path, Path):
        raise TypeError("path must be a Path or None")
    if options.path is None:
        if not options.use_builtin:
            raise PricingPackError("pricing is disabled; no pack may be loaded")
        path = BUILTIN_PRICING_PACK_PATH
        kind = PricingPackKind.BUILTIN
    else:
        path = options.path
        kind = PricingPackKind.CUSTOM
    raw = _read_once(path)
    return PricingLoadResult(pack=_build_pack(_decode_json(raw), raw, kind))


__all__ = [
    "BUILTIN_PRICING_PACK_PATH",
    "MAX_JSON_DEPTH",
    "MAX_PRICING_PACK_BYTES",
    "MAX_RATES",
    "PricingLoadOptions",
    "PricingLoadResult",
    "PricingPack",
    "PricingPackError",
    "PricingPackKind",
    "PricingProvenance",
    "PricingSource",
    "RateCard",
    "load_pricing_pack",
]

"""Configurable verticals: one codebase, many hyperlocal marketplaces.

A *vertical* is a named configuration bundle — brand nouns, enabled
modules, credit economy, platform fee, taxonomy, geo default, trust
requirements — that the shared marketplace engine reads at runtime
instead of baking Garden Swap's literals into the domain modules.

Selection (first match wins, resolved once per process):

1. ``VERTICAL_CONFIG_PATH`` — path to a JSON config file.
2. ``VERTICAL_ID`` — loads ``api/app/verticals/<id>.json``; an unknown
   id is a hard error (fail boot loudly, never silently fall back).
3. Built-in :data:`GARDEN_DEFAULT` — today's Garden Swap behavior,
   byte-identical to the pre-config literals. No file is read.

Validation is strict and runs at load: unknown keys are rejected, the
economy/fee bounds mirror the DB constraints (listing credit cost can
never exceed the 0–100 CHECK ceiling — migration 0043 widened the
original 1–100), and ``credit_expiry`` only
supports ``"seasonal"`` in v1. A malformed config must kill the boot
(``main.py`` calls :func:`get_vertical` in the lifespan) rather than
degrade into a silently different marketplace.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

VERTICALS_DIR = Path(__file__).resolve().parent / "verticals"


# ---------------------------------------------------------------------------
# Schema (frozen; lists are tuples so a cached config can never mutate)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrandNouns:
    offer: str = "listing"
    offer_plural: str = "listings"
    provider: str = "sitter"
    seeker: str = "claimer"
    credit: str = "credit"


@dataclass(frozen=True)
class BrandConfig:
    display_name: str = "Garden Swap"
    nouns: BrandNouns = BrandNouns()


@dataclass(frozen=True)
class ModulesConfig:
    # Declarative client guidance in v1: every router stays registered
    # regardless of these flags (no module gating / 404-ing yet).
    listings: bool = True
    trees: bool = True
    sitters: bool = True
    wallet: bool = True
    want_list: bool = True
    chat: bool = True
    bookings: bool = True


@dataclass(frozen=True)
class EconomyConfig:
    credit_name: str = "credit"
    # Master switches for the two money systems. credits_enabled=False
    # runs the vertical as free/plain exchange: only free (cost 0)
    # listings/slots may be created, claims and slot claims move no
    # credits, and no starter lot is granted. usd_services_enabled=False
    # means sitters may only price in credits: USD-priced profiles,
    # bookings, and payment quotes are rejected. Both default on, so
    # the garden default is byte-identical to the pre-switch behavior.
    credits_enabled: bool = True
    usd_services_enabled: bool = True
    starter_credits: int = 3
    earn_cap_amount: int = 10
    earn_cap_window_days: int = 7
    # Mirrors the listings.credit_cost DB CHECK (0..100 since migration
    # 0043 freed the floor): a vertical may lower the price ceiling,
    # never raise it past the schema bound.
    max_listing_cost: int = 100
    # Only "seasonal" is implemented (credits.py season math); "never"
    # is rejected at load until that mode actually exists.
    credit_expiry: str = "seasonal"


@dataclass(frozen=True)
class FeesConfig:
    sitter_platform_fee_pct: float = 18


@dataclass(frozen=True)
class TaxonomyConfig:
    # WARNING (v1): listing_types is DECLARED, NOT ENFORCED. It is display
    # metadata served via /v1/config; the write path still rejects any
    # type outside ("seedling", "harvest", "tree") — ListingIn's static
    # pattern AND the listings.type DB CHECK (migration 0004). A
    # vertical that declares other types here advertises a taxonomy its
    # own API 422s on create. Making declared types enforceable needs a
    # future migration that wires this list into ListingIn validation
    # and the DB CHECK together; until then every fixture must keep
    # listing_types == the enforced vocabulary.
    # sitter_services, by contrast, IS enforced (sitter.py reads it live).
    listing_types: tuple[str, ...] = ("seedling", "harvest", "tree")
    sitter_services: tuple[str, ...] = (
        "watering",
        "repotting",
        "fertilizing",
        "pruning",
        "pest_control",
        "vacation_care",
    )


@dataclass(frozen=True)
class GeoConfig:
    default_radius_miles: float = 5


@dataclass(frozen=True)
class TrustConfig:
    idv_required_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerticalConfig:
    vertical_id: str = "garden"
    # config_version discipline: bump this whenever the serialized
    # /v1/config payload's meaning changes (field added/reinterpreted,
    # default flipped). The version rides in the client cache key
    # (ETag "<id>-<version>"), so the same id + version must NEVER ship
    # different content — clients holding the old payload could not tell.
    config_version: int = 1
    brand: BrandConfig = BrandConfig()
    modules: ModulesConfig = ModulesConfig()
    economy: EconomyConfig = EconomyConfig()
    fees: FeesConfig = FeesConfig()
    taxonomy: TaxonomyConfig = TaxonomyConfig()
    geo: GeoConfig = GeoConfig()
    trust: TrustConfig = TrustConfig()


GARDEN_DEFAULT = VerticalConfig()


# ---------------------------------------------------------------------------
# Strict parsing / validation
# ---------------------------------------------------------------------------

class VerticalConfigError(ValueError):
    """Malformed vertical config. Raised at load so boot fails loudly."""


def _keys(d: dict, allowed: set[str], ctx: str) -> None:
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise VerticalConfigError(f"{ctx}: unknown keys {unknown}")


def _section(d: dict, name: str) -> dict:
    v = d.get(name, {})
    if not isinstance(v, dict):
        raise VerticalConfigError(f"{name}: must be an object")
    return v


def _str(d: dict, key: str, default: str, ctx: str) -> str:
    v = d.get(key, default)
    if not isinstance(v, str) or not v:
        raise VerticalConfigError(f"{ctx}.{key}: must be a non-empty string")
    return v


def _int(d: dict, key: str, default: int, ctx: str) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise VerticalConfigError(f"{ctx}.{key}: must be an integer")
    return v


def _num(d: dict, key: str, default: float, ctx: str) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise VerticalConfigError(f"{ctx}.{key}: must be a number")
    # JSON's NaN/Infinity literals parse to non-finite floats; a NaN fee
    # or radius would slip through range checks (all comparisons false),
    # so reject non-finite values here, at the shared choke point.
    if not math.isfinite(v):
        raise VerticalConfigError(f"{ctx}.{key}: must be a finite number")
    return v


def _bool(d: dict, key: str, default: bool, ctx: str) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise VerticalConfigError(f"{ctx}.{key}: must be a boolean")
    return v


def _str_tuple(d: dict, key: str, default: tuple[str, ...], ctx: str) -> tuple[str, ...]:
    v = d.get(key, list(default))
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise VerticalConfigError(f"{ctx}.{key}: must be a list of non-empty strings")
    return tuple(v)


def vertical_from_dict(d: dict) -> VerticalConfig:
    """Build a validated VerticalConfig from parsed JSON (strict schema)."""
    if not isinstance(d, dict):
        raise VerticalConfigError("vertical config must be a JSON object")
    _keys(d, {"vertical_id", "config_version", "brand", "modules", "economy",
              "fees", "taxonomy", "geo", "trust"}, "vertical")

    brand_d = _section(d, "brand")
    _keys(brand_d, {"display_name", "nouns"}, "brand")
    nouns_d = _section(brand_d, "nouns")
    _keys(nouns_d, {"offer", "offer_plural", "provider", "seeker", "credit"},
          "brand.nouns")
    brand = BrandConfig(
        display_name=_str(brand_d, "display_name", "Garden Swap", "brand"),
        nouns=BrandNouns(
            offer=_str(nouns_d, "offer", "listing", "brand.nouns"),
            offer_plural=_str(nouns_d, "offer_plural", "listings", "brand.nouns"),
            provider=_str(nouns_d, "provider", "sitter", "brand.nouns"),
            seeker=_str(nouns_d, "seeker", "claimer", "brand.nouns"),
            credit=_str(nouns_d, "credit", "credit", "brand.nouns"),
        ),
    )

    mod_d = _section(d, "modules")
    mod_keys = {"listings", "trees", "sitters", "wallet", "want_list",
                "chat", "bookings"}
    _keys(mod_d, mod_keys, "modules")
    modules = ModulesConfig(**{
        k: _bool(mod_d, k, True, "modules") for k in sorted(mod_keys)
    })

    eco_d = _section(d, "economy")
    _keys(eco_d, {"credit_name", "credits_enabled", "usd_services_enabled",
                  "starter_credits", "earn_cap_amount",
                  "earn_cap_window_days", "max_listing_cost", "credit_expiry"},
          "economy")
    starter = _int(eco_d, "starter_credits", 3, "economy")
    if starter < 0:
        raise VerticalConfigError("economy.starter_credits: must be >= 0")
    cap_amt = _int(eco_d, "earn_cap_amount", 10, "economy")
    if cap_amt < 0:
        raise VerticalConfigError("economy.earn_cap_amount: must be >= 0")
    cap_days = _int(eco_d, "earn_cap_window_days", 7, "economy")
    if cap_days < 1:
        raise VerticalConfigError("economy.earn_cap_window_days: must be >= 1")
    max_cost = _int(eco_d, "max_listing_cost", 100, "economy")
    if not 1 <= max_cost <= 100:
        raise VerticalConfigError(
            "economy.max_listing_cost: must be 1..100 (DB CHECK ceiling)")
    expiry = _str(eco_d, "credit_expiry", "seasonal", "economy")
    if expiry != "seasonal":
        raise VerticalConfigError(
            f"economy.credit_expiry: {expiry!r} is not implemented in v1; "
            "only 'seasonal' is supported")
    economy = EconomyConfig(
        credit_name=_str(eco_d, "credit_name", "credit", "economy"),
        credits_enabled=_bool(eco_d, "credits_enabled", True, "economy"),
        usd_services_enabled=_bool(eco_d, "usd_services_enabled", True, "economy"),
        starter_credits=starter,
        earn_cap_amount=cap_amt,
        earn_cap_window_days=cap_days,
        max_listing_cost=max_cost,
        credit_expiry=expiry,
    )

    fees_d = _section(d, "fees")
    _keys(fees_d, {"sitter_platform_fee_pct"}, "fees")
    pct = _num(fees_d, "sitter_platform_fee_pct", 18, "fees")
    if not 0 <= pct <= 100:
        raise VerticalConfigError("fees.sitter_platform_fee_pct: must be 0..100")
    fees = FeesConfig(sitter_platform_fee_pct=pct)

    tax_d = _section(d, "taxonomy")
    _keys(tax_d, {"listing_types", "sitter_services"}, "taxonomy")
    taxonomy = TaxonomyConfig(
        listing_types=_str_tuple(tax_d, "listing_types",
                                 TaxonomyConfig.listing_types, "taxonomy"),
        sitter_services=_str_tuple(tax_d, "sitter_services",
                                   TaxonomyConfig.sitter_services, "taxonomy"),
    )
    if modules.listings and not taxonomy.listing_types:
        raise VerticalConfigError(
            "taxonomy.listing_types: must be non-empty when modules.listings is on")
    if modules.sitters and not taxonomy.sitter_services:
        raise VerticalConfigError(
            "taxonomy.sitter_services: must be non-empty when modules.sitters is on")

    geo_d = _section(d, "geo")
    _keys(geo_d, {"default_radius_miles"}, "geo")
    radius = _num(geo_d, "default_radius_miles", 5, "geo")
    if radius <= 0:
        raise VerticalConfigError("geo.default_radius_miles: must be > 0")
    geo = GeoConfig(default_radius_miles=radius)

    trust_d = _section(d, "trust")
    _keys(trust_d, {"idv_required_actions"}, "trust")
    trust = TrustConfig(idv_required_actions=_str_tuple(
        trust_d, "idv_required_actions", (), "trust"))

    version = _int(d, "config_version", 1, "vertical")
    if version < 1:
        raise VerticalConfigError("vertical.config_version: must be >= 1")
    return VerticalConfig(
        vertical_id=_str(d, "vertical_id", "garden", "vertical"),
        config_version=version,
        brand=brand, modules=modules, economy=economy, fees=fees,
        taxonomy=taxonomy, geo=geo, trust=trust,
    )


# ---------------------------------------------------------------------------
# Loading (once per process; cached)
# ---------------------------------------------------------------------------

_cache: VerticalConfig | None = None


def _load_uncached() -> VerticalConfig:
    path = os.environ.get("VERTICAL_CONFIG_PATH")
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise VerticalConfigError(
                f"VERTICAL_CONFIG_PATH {path!r} unreadable: {exc}") from exc
        try:
            return vertical_from_dict(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise VerticalConfigError(
                f"VERTICAL_CONFIG_PATH {path!r}: invalid JSON: {exc}") from exc
    vertical_id = os.environ.get("VERTICAL_ID")
    if vertical_id:
        candidate = VERTICALS_DIR / f"{vertical_id}.json"
        if not candidate.is_file():
            raise VerticalConfigError(
                f"unknown VERTICAL_ID {vertical_id!r}: no config at {candidate}")
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise VerticalConfigError(
                f"VERTICAL_ID {vertical_id!r}: unreadable config: {exc}"
            ) from exc
        try:
            return vertical_from_dict(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise VerticalConfigError(
                f"VERTICAL_ID {vertical_id!r}: invalid JSON: {exc}") from exc
    return GARDEN_DEFAULT


def get_vertical() -> VerticalConfig:
    """The process-wide vertical config. Loads + validates on first call."""
    global _cache
    if _cache is None:
        _cache = _load_uncached()
    return _cache


def reset_vertical_cache() -> None:
    """Drop the cached config (tests monkeypatch env, then call this)."""
    global _cache
    _cache = None

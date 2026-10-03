"""GET /v1/config — the public vertical contract for clients.

Unauthenticated by design (see EXEMPT_PATHS in auth.py): a client must
learn its brand/modules/taxonomy before sign-in. The response is an
explicit allowlist — economy tuning internals (the anti-gaming earn cap)
and anything secret-adjacent (database URLs, keys) are never serialized.
The earn-cap fields are server-side anti-abuse tuning; clients must not
depend on them, so they stay out of the payload.

Cacheable for 5 minutes: vertical config is fixed per deployment, so
clients can refetch cheaply on launch without hammering the API.
"""

from __future__ import annotations

# NOTE: taxonomy.listing_types in this payload is display metadata in
# v1 — declared by the vertical, NOT enforced on the write path.
# ListingIn and the listings.type DB CHECK still only accept
# ("seedling", "harvest", "tree"); a vertical advertising other types
# would see its own creates 422. taxonomy.sitter_services IS enforced
# server-side. See the WARNING on TaxonomyConfig in vertical.py.

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Response

from .vertical import VerticalConfig, get_vertical

router = APIRouter(tags=["config"])


def public_vertical_config(v: VerticalConfig) -> dict[str, Any]:
    """Allowlist serializer: exactly the fields a client may consume."""
    return {
        "vertical_id": v.vertical_id,
        "config_version": v.config_version,
        "brand": {
            "display_name": v.brand.display_name,
            "nouns": asdict(v.brand.nouns),
        },
        "modules": asdict(v.modules),
        "economy": {
            "credit_name": v.economy.credit_name,
            "starter_credits": v.economy.starter_credits,
            "max_listing_cost": v.economy.max_listing_cost,
            "credit_expiry": v.economy.credit_expiry,
            # earn_cap_amount / earn_cap_window_days deliberately excluded:
            # anti-gaming tuning is server-internal.
        },
        "fees": {"sitter_platform_fee_pct": v.fees.sitter_platform_fee_pct},
        "taxonomy": {
            "listing_types": list(v.taxonomy.listing_types),
            "sitter_services": list(v.taxonomy.sitter_services),
        },
        "geo": {"default_radius_miles": v.geo.default_radius_miles},
        "trust": {"idv_required_actions": list(v.trust.idv_required_actions)},
    }


@router.get("/v1/config")
def get_config(response: Response) -> dict[str, Any]:
    v = get_vertical()
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["ETag"] = f'"{v.vertical_id}-{v.config_version}"'
    return public_vertical_config(v)

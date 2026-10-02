"""Feed & discovery (API-021).

``GET /v1/feed`` returns live listings ranked in the database, globally
across pages (no page-local rerank):

1. **exact want-list matches** — the listing variety equals one of the
   viewer's want entries (case-insensitive), the entry's type filter allows
   the listing type (empty = any), and the listing is not the viewer's own.
2. **inexact want-list matches** — the variety is a literal substring match
   in either direction (case-insensitive) but not exact, same eligibility.
3. **everything else**.

Within each tier: posting date descending (nulls last), then expiry
ascending (nulls last), then id ascending for determinism. ``LIMIT`` /
``OFFSET`` paginate the ranked order, so a match on a later DB page still
outranks non-matches above it.

Cursor pagination: opaque base64 offset cursor over the ranked order. Geo
is fuzzed via ``listings.public_listing`` — true coordinates never leave
the server.

``way=sitting`` returns sitter profiles under a ``"sitters"`` key (not
``"listings"``).

Caching: ranked feed pages are cached per viewer (``l:live:ranked:<uid>``)
because tiers depend on the viewer's want-list — a shared key would serve
one viewer's ranking to another. Listing writes invalidate every ``l:live``
key (ranked pages included); want-list writes invalidate the writer's
ranked keys. TTL bounds any residual staleness.
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import get_current_uid
from .listings import ListingRepo, batch_owners, get_listing_repo, public_listing
from .sitter import SitterRepo, _display_name, _serialize_profile, get_sitter_repo
from .users import UserRepo, get_user_repo
from .wantlist import WantRepo, get_want_repo

router = APIRouter(prefix="/v1", tags=["feed"])

PAGE_LIMIT = 20
MAX_LIMIT = 50

# API-123: Explore way-cards. "pick" is pick-your-own -> tree listings;
# "sitting" lists sitter profiles instead of listings.
WAYS = ("seedling", "harvest", "pick", "sitting")
_WAY_TO_LISTING_TYPE = {"seedling": "seedling", "harvest": "harvest", "pick": "tree"}


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise HTTPException(400, {"code": "invalid_cursor", "message": "Malformed feed cursor"})
    if offset < 0:
        raise HTTPException(400, {"code": "invalid_cursor", "message": "Malformed feed cursor"})
    return offset


def _batch_display_names(user_repo: UserRepo, uids: list[str]) -> dict[str, str | None]:
    """Display names for many uids with a single batched read (M10c).

    Prefers ``UserRepo.get_many`` when the repo offers it (one query for the
    whole page of sitters); falls back to per-uid ``get`` otherwise. Uids are
    de-duplicated so each user is read at most once per call.
    """
    unique = list(dict.fromkeys(uids))
    get_many = getattr(user_repo, "get_many", None)
    if callable(get_many):
        rows = get_many(unique)
        if isinstance(rows, dict):
            return {u: (rows.get(u) or {}).get("display_name") for u in unique}
        return {u: (r or {}).get("display_name") for u, r in zip(unique, rows)}
    return {u: _display_name(user_repo, u) for u in unique}


@router.get("/feed")
def get_feed(
    limit: int = Query(default=PAGE_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    way: str | None = Query(default=None, pattern="^(seedling|harvest|pick|sitting)$"),
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    want_repo: WantRepo = Depends(get_want_repo),
    sitter_repo: SitterRepo = Depends(get_sitter_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Ranked discovery feed of live listings (fuzzed geo, no PII).

    Without ``way``: the ranked, cursor-paginated feed (API-021). Ranking
    happens in the database (``list_live_ranked``) so pagination walks the
    global order: exact want-list matches, then inexact matches, then the
    rest; each tier by newest post, then soonest expiry (nulls last).
    With ``way`` (API-123): exact ``{"listings": [...]}`` for one Explore
    way-card — except ``way=sitting``, which returns sitter profiles under a
    ``{"sitters": [...]}`` key (L4c).

    Ranking (both listing branches): exact want-list matches first, then
    inexact matches, then everything else; newest post, then soonest expiry
    (nulls last) within each tier.
    """
    if way is not None:
        if way == "sitting":
            profiles = sitter_repo.list_active()
            # M10c: one batched display-name lookup, not one query per sitter.
            names = _batch_display_names(user_repo, [r["uid"] for r in profiles])
            return {"sitters": [_serialize_profile(r, names.get(r["uid"]))
                                for r in profiles[:limit]]}
        entries = want_repo.list_for_user(uid)
        ranked = repo.list_live_ranked(entries, limit=limit,
                                       listing_type=_WAY_TO_LISTING_TYPE[way])
        owners = batch_owners(user_repo, ranked)
        return {"listings": [public_listing(r, viewer_uid=uid, owners=owners)
                             for r in ranked]}

    offset = _decode_cursor(cursor)
    entries = want_repo.list_for_user(uid)
    ranked = repo.list_live_ranked(entries, limit=limit, offset=offset)
    total = repo.count_live()
    next_cursor = _encode_cursor(offset + limit) if offset + limit < total else None
    owners = batch_owners(user_repo, ranked)
    return {
        "items": [public_listing(r, viewer_uid=uid, owners=owners) for r in ranked],
        "next_cursor": next_cursor,
    }

"""Feed & discovery (API-021).

``GET /v1/feed`` returns live listings ranked by a transparent score:

- **urgency** (weight 0.6): expires soon first — PRD ranks by *time remaining*,
  not recency. A seedling with 1 day left outranks one listed an hour ago with
  6 days left.
- **want-list match**: listings matching the caller's want-list come first.
- **posting date**: newest first breaks match ties.
- **expiry**: soonest expiry first breaks date ties (nulls last).

Cursor pagination: opaque base64 offset cursor. Geo is fuzzed via
``listings.public_listing`` — true coordinates never leave the server.

``way=sitting`` returns sitter profiles under a ``"sitters"`` key (not
``"listings"``).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import get_current_uid
from .listings import ListingRepo, batch_owners, get_listing_repo, public_listing
from .sitter import SitterRepo, _display_name, _serialize_profile, get_sitter_repo
from .users import UserRepo, get_user_repo
from .wantlist import WantRepo, find_matches, get_want_repo

router = APIRouter(prefix="/v1", tags=["feed"])

PAGE_LIMIT = 20
MAX_LIMIT = 50

# API-123: Explore way-cards. "pick" is pick-your-own -> tree listings;
# "sitting" lists sitter profiles instead of listings.
WAYS = ("seedling", "harvest", "pick", "sitting")
_WAY_TO_LISTING_TYPE = {"seedling": "seedling", "harvest": "harvest", "pick": "tree"}


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _feed_rank_key(row: dict[str, Any],
                   entries: list[dict[str, Any]]) -> tuple[int, float, tuple]:
    """Lexicographic feed rank: want-list match first, then newest post,
    then soonest expiry (nulls last, id tiebreak). Deterministic and
    testable. ``entries`` are the caller's want-list rows; ``find_matches``
    excludes the caller's own listings and honors entry type filters.
    """
    matched = bool(find_matches(row, entries))
    created = _parse_dt(row.get("created_at"))
    created_ts = created.timestamp() if created else 0.0
    return (0 if matched else 1, -created_ts, _expiry_key(row))


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


def _expiry_key(row: dict[str, Any]) -> tuple[bool, float, str]:
    """Freshest-first by expires_at ascending, nulls last (API-123)."""
    dt = _parse_dt(row.get("expires_at"))
    return (dt is None, dt.timestamp() if dt else 0.0, str(row.get("id") or ""))


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

    Without ``way``: the scored, cursor-paginated feed (API-021). Pagination
    is DB-level (M9): one page of rows is fetched with limit/offset and only
    that page is scored in Python, so per-request work stays bounded no
    matter how large the listings table grows.
    With ``way`` (API-123): exact ``{"listings": [...]}`` for one Explore
    way-card — except ``way=sitting``, which returns sitter profiles under a
    ``{"sitters": [...]}`` key (L4c).

    Ranking (both branches): want-list matches first, then newest post,
    then soonest expiry (nulls last).
    """
    if way is not None:
        if way == "sitting":
            profiles = sitter_repo.list_active()
            # M10c: one batched display-name lookup, not one query per sitter.
            names = _batch_display_names(user_repo, [r["uid"] for r in profiles])
            return {"sitters": [_serialize_profile(r, names.get(r["uid"]))
                                for r in profiles[:limit]]}
        live = repo.list_live(limit=limit, listing_type=_WAY_TO_LISTING_TYPE[way])
        entries = want_repo.list_for_user(uid)
        ranked = sorted(live, key=lambda r: _feed_rank_key(r, entries))
        owners = batch_owners(user_repo, ranked[:limit])
        return {"listings": [public_listing(r, viewer_uid=uid, owners=owners)
                             for r in ranked[:limit]]}

    offset = _decode_cursor(cursor)
    # M9: DB-level pagination — fetch one page, rank only the page.
    page = repo.list_live(limit=limit, offset=offset)
    entries = want_repo.list_for_user(uid)
    ranked = sorted(page, key=lambda r: _feed_rank_key(r, entries))
    total = repo.count_live()
    next_cursor = _encode_cursor(offset + limit) if offset + limit < total else None
    owners = batch_owners(user_repo, ranked)
    return {
        "items": [public_listing(r, viewer_uid=uid, owners=owners) for r in ranked],
        "next_cursor": next_cursor,
    }

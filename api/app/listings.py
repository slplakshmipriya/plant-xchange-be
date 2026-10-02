"""Listing CRUD + lifecycle state machine (API-020).

One engine serves seedling, harvest, and tree listings. Rules enforced
server-side:

- Photo required: a listing cannot go live (or be created live) without >= 1
  photo URL.
- ``spray_disclosure`` is mandatory text on every listing (PRD: no silent
  pesticide use).
- ``credit_cost`` bounded to 1..3 (DB CHECK + API validation).
- Lifecycle is a strict state machine; illegal transitions are 422, never
  silently coerced. Terminal states: completed, expired, cancelled.
- SEC-010: responses carry FUZZED geo via ``fuzz_location_for_listing`` — a
  DETERMINISTIC per-listing offset (HMAC of the listing id, 0.05–0.5 mi band),
  so repeated reads return the same point and averaging cannot triangulate
  the true coordinate (M1). True coordinates never leave the server. True
  coordinates are stored ENCRYPTED at rest (Fernet, ``GEO_ENCRYPTION_KEY``):
  the ``geo_lat`` / ``geo_lon`` columns hold ciphertext, encrypted on every
  repo write and decrypted in-process only inside the serializers,
  immediately before fuzzing. (Exact address stays hidden until the
  exchange-confirm flow lands in a later wave.)
- Expiry: ``POST /v1/internal/sweep`` flips live->expired past ``expires_at``.
  Idempotent; service-to-service auth via ``X-Sweep-Secret``. The same sweep
  enforces the retention policy (M19): notification_log 90d, resolved disputes
  2y, media GC for terminal listings 180d. Append-only by design (never
  purged): the credit ledger (financial record), harvest_events (pick audit),
  moderation_views (safety audit), and reports/strikes/enforcement history.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import time
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import ensure_owner, get_current_uid
from .cache import CachedListingRepo
from .config import get_settings
from .crypto import GEO_KEY_ENV, decrypt_float, encrypt_float
from .db import get_db_conn
from .images import (
    GCSBlobStore,
    StoredImagesRepo,
    get_blob_store_or_none,
    get_images_repo,
    release_listing_images,
)
from .notify import (
    NotificationRepo,
    get_notification_repo,
    on_listing_expiry_nudge,
    send_notification,
)
from .users import UserRepo, get_user_repo
from .wantlist import WantRepo, find_matches, get_want_repo, notify_matches

router = APIRouter(prefix="/v1", tags=["listings"])
internal_router = APIRouter(prefix="/v1/internal", tags=["internal"])

LISTING_TYPES = ("seedling", "harvest", "tree")
STATUSES = ("draft", "live", "claimed", "completed", "expired", "cancelled")

# Server-side lifecycle. Only these transitions are legal.
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"live", "cancelled"},
    "live": {"claimed", "expired", "cancelled"},
    "claimed": {"completed", "cancelled", "live"},
    "completed": set(),
    "expired": set(),
    "cancelled": set(),
}

EDITABLE_STATUSES = ("draft", "live")  # PATCH allowed only in these states


def can_transition(frm: str, to: str) -> bool:
    return to in TRANSITIONS.get(frm, set())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime | None) -> datetime | None:
    """Treat naive datetimes as UTC. Clients that omit the offset (e.g. local
    ISO strings) must not 500 the naive/aware comparison or poison the sweep
    job — assume UTC and say so in the stored value."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------- geo fuzzing

def fuzz_location(lat: float, lon: float, rng: random.Random | None = None) -> tuple[float, float]:
    """Jitter a coordinate by up to ~0.5 mi in a random direction (SEC-010).

    ``rng`` is injectable for deterministic tests; production serializers use
    ``fuzz_location_for_listing`` (deterministic per listing) instead.
    """
    r = rng or random.SystemRandom()
    miles = r.uniform(0.05, 0.5)  # never return the exact point
    theta = r.uniform(0, 2 * math.pi)
    dlat = miles / 69.0 * math.cos(theta)
    dlon = miles / (69.0 * math.cos(math.radians(lat))) * math.sin(theta)
    return lat + dlat, lon + dlon


def _fuzz_offset_for_listing(listing_id: str) -> tuple[float, float]:
    """Deterministic per-listing fuzz offset (M1): HMAC(listing_id) -> (miles, theta).

    A fresh random offset per request is triangulable — averaging N reads of
    the same listing converges on the true coordinate. Deriving the offset
    from ``HMAC(listing_id, GEO_ENCRYPTION_KEY)`` makes repeated reads return
    the SAME fuzzed point, so averaging buys the attacker nothing. The geo
    key doubles as the HMAC secret: it is a server-side secret, stable across
    restarts (unlike a boot-time random), and already required to be set for
    geo handling. Distinct listings get distinct offsets.
    """
    secret = os.environ.get(GEO_KEY_ENV, "").encode("utf-8")
    mac = hmac.new(secret, str(listing_id).encode("utf-8"), hashlib.sha256).digest()
    miles = 0.05 + (int.from_bytes(mac[:8], "big") / 2**64) * 0.45  # 0.05..0.5 mi band
    theta = (int.from_bytes(mac[8:16], "big") / 2**64) * 2 * math.pi
    return miles, theta


def fuzz_location_for_listing(lat: float, lon: float, listing_id: str) -> tuple[float, float]:
    """SEC-010 fuzz for a listing: stable per listing id (M1)."""
    miles, theta = _fuzz_offset_for_listing(listing_id)
    dlat = miles / 69.0 * math.cos(theta)
    dlon = miles / (69.0 * math.cos(math.radians(lat))) * math.sin(theta)
    return lat + dlat, lon + dlon


def public_listing(row: dict[str, Any], rng: random.Random | None = None,
                   viewer_uid: str | None = None,
                   owners: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Public serializer: fuzzed geo, no owner PII (owner is just a uid).

    ``row`` carries ENCRYPTED geo (repo contract); decrypt in-process here,
    immediately before fuzzing, so true coordinates never sit in a served
    dict. Fail closed: bad ciphertext raises.

    Fuzzing is deterministic per listing id (M1) unless an explicit ``rng``
    is passed (tests).

    ``owners`` maps owner uid -> user row (see :func:`batch_owners`); the
    listing carries the owner's display name + avatar URL for the index
    card and detail view. Absent (or nameless) owners fall back to
    "Neighbor" / null so old callers keep working.

    L8: ``claimer_uid`` is revealed only to the listing's owner or claimer.
    Pass the viewer's uid explicitly from every PUBLIC route (feed, detail,
    cards). ``viewer_uid=None`` (the default) means participant/internal
    context — the calling route has already established the viewer is a
    party to the listing (the claim/exchange flows, which must show the
    claimer to the counterparty to coordinate pickup) — so it is revealed.
    """
    lat = decrypt_float(row.get("geo_lat"), GEO_KEY_ENV)
    lon = decrypt_float(row.get("geo_lon"), GEO_KEY_ENV)
    if lat is not None and lon is not None:
        if rng is not None:
            flat, flon = fuzz_location(lat, lon, rng)
        else:
            flat, flon = fuzz_location_for_listing(lat, lon, row["id"])
    else:
        flat, flon = None, None
    window = row.get("pickup_window")
    claimer_uid = row.get("claimer_uid")
    # L8: viewer_uid=None is participant/internal context (claim/exchange
    # routes, whose viewer is always owner or claimer) -> reveal. An explicit
    # viewer must be the owner or the claimer; everyone else gets None.
    is_party = (viewer_uid is None
                or viewer_uid in (row.get("owner_uid"), claimer_uid))
    owner = (owners or {}).get(row.get("owner_uid")) or {}
    return {
        "id": str(row["id"]),
        "owner_uid": row["owner_uid"],
        "owner_display_name": owner.get("display_name") or "Neighbor",
        "owner_avatar_url": owner.get("avatar_url"),
        "type": row["type"],
        "photos": list(row.get("photos") or []),
        "variety": row.get("variety"),
        "quantity": float(row["quantity"]) if row.get("quantity") is not None else None,
        "unit": row.get("unit"),
        "credit_cost": row["credit_cost"],
        "pickup_window": {"start": window[0], "end": window[1]} if window else None,
        "expires_at": row.get("expires_at"),
        "geo_lat": flat,
        "geo_lon": flon,
        "spray_disclosure": row.get("spray_disclosure"),
        "status": row["status"],
        "created_at": row.get("created_at"),
        "remaining_qty": (float(row["remaining_qty"])
                          if row.get("remaining_qty") is not None else None),
        "visit_rules": row.get("visit_rules"),
        "claimer_uid": claimer_uid if is_party else None,
        # AND-125/AND-126: optional create-form fields (absent on old rows).
        "potSize": row.get("pot_size"),
        "plantAgeYears": row.get("plant_age_years"),
        "pickupWindowDays": row.get("pickup_window_days", 4),
    }


def batch_owners(user_repo: UserRepo,
                 rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Owner user rows for many listings with a single batched read (M10c).

    Returns ``{owner_uid: user_row}`` for every distinct owner in ``rows``;
    callers pass it as ``public_listing(..., owners=...)``. Prefers
    ``UserRepo.get_many`` when the repo offers it, falling back to per-uid
    ``get`` otherwise. Uids are de-duplicated so each user is read at most
    once per call.
    """
    uids = sorted({r.get("owner_uid") for r in rows if r.get("owner_uid")})
    if not uids:
        return {}
    get_many = getattr(user_repo, "get_many", None)
    if callable(get_many):
        by_uid = get_many(uids)
        if isinstance(by_uid, dict):
            return {u: (by_uid.get(u) or {}) for u in uids}
    return {u: (user_repo.get(u) or {}) for u in uids}


# ---------------------------------------------------------------- repository

class ListingRepo(Protocol):
    def create(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def get(self, listing_id: str) -> dict[str, Any] | None: ...
    def update(self, listing_id: str, fields: dict[str, Any]) -> dict[str, Any] | None: ...
    def set_status(self, listing_id: str, status: str) -> dict[str, Any] | None: ...
    def claim(self, listing_id: str, claimer_uid: str) -> dict[str, Any] | None:
        """Atomically claim a live listing (live -> claimed + claimer_uid).

        Returns the updated row, or None when the listing is not live —
        concurrent claimants cannot both win."""
        ...
    def complete_if_claimed(self, listing_id: str) -> dict[str, Any] | None:
        """Atomically flip claimed -> completed. Only one caller wins; the
        loser gets None. Guards the exactly-once credit move."""
        ...
    def complete_if_live(self, listing_id: str) -> dict[str, Any] | None:
        """Atomically flip live -> completed in ONE conditional UPDATE (M6).

        Used when a harvest is fully picked: no claimer exists, so the
        listing completes directly instead of walking live -> claimed ->
        completed as two commits (a crash between them would strand the
        listing in claimed with no sweeper). Returns the updated row, or
        None when the listing is not live — concurrent racers cannot both
        win."""
        ...
    def sweep_expired(self, now: datetime) -> int: ...
    def list_live(self, limit: int | None = None, offset: int = 0,
                  listing_type: str | None = None) -> list[dict[str, Any]]:
        """Live listings, DB-paginated (M9). Ordered by expires_at ascending
        (nulls last), then created_at descending — the same order the feed
        scores within a page."""
        ...
    def count_live(self, listing_type: str | None = None) -> int:
        """Total live listings (M9: feed next_cursor without loading rows)."""
        ...
    def list_live_expiring_before(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Live listings with expires_at < cutoff (M9). Bounds the sweep's
        expiry-nudge scan to listings that can actually need a nudge instead
        of iterating every live listing in-request."""
        ...
    def list_by_owner(self, uid: str) -> list[dict[str, Any]]:
        """All listings owned by uid (any status) — for thread scoping."""
        ...
    def decrement_remaining(self, listing_id: str, delta: float) -> dict[str, Any] | None:
        """Atomically subtract delta from remaining (COALESCE remaining_qty, quantity).
        Returns the updated row, or None when the listing is missing or delta
        exceeds what is available."""
        ...
    def log_harvest_event(self, listing_id: str, recorder_uid: str,
                          delta_kg: float, remaining_after: float) -> None: ...
    def list_harvest_events(self, listing_id: str) -> list[dict[str, Any]]: ...


class PostgresListingRepo:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        lo = d.pop("window_start", None)
        hi = d.pop("window_end", None)
        d["pickup_window"] = (lo.isoformat() if lo else None, hi.isoformat() if hi else None) if lo or hi else None
        for k in ("expires_at", "created_at"):
            v = d.get(k)
            d[k] = v.isoformat() if hasattr(v, "isoformat") else v
        return d

    _SELECT = (
        "SELECT id, owner_uid, type, photos, variety, quantity, unit, credit_cost, "
        "lower(pickup_window) AS window_start, upper(pickup_window) AS window_end, "
        "expires_at, geo_lat, geo_lon, spray_disclosure, status, created_at, "
        "COALESCE(remaining_qty, quantity) AS remaining_qty, visit_rules, claimer_uid, "
        "pot_size, plant_age_years, pickup_window_days FROM listings"
    )

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        window = data.get("pickup_window")
        row = self._conn.execute(
            "INSERT INTO listings (id, owner_uid, type, photos, variety, quantity, unit, "
            "credit_cost, pickup_window, expires_at, geo_lat, geo_lon, spray_disclosure, status, "
            "remaining_qty, visit_rules, pot_size, plant_age_years, pickup_window_days) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,tstzrange(%s,%s,'[)'),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            + "RETURNING id",
            (
                data["id"], data["owner_uid"], data["type"], data["photos"],
                data.get("variety"), data.get("quantity"), data.get("unit"),
                data["credit_cost"],
                window[0] if window else None, window[1] if window else None,
                data.get("expires_at"),
                encrypt_float(data.get("geo_lat"), GEO_KEY_ENV),
                encrypt_float(data.get("geo_lon"), GEO_KEY_ENV),
                data["spray_disclosure"], data.get("status", "draft"),
                data.get("remaining_qty"), data.get("visit_rules"),
                data.get("pot_size"), data.get("plant_age_years"),
                data.get("pickup_window_days", 4),
            ),
        ).fetchone()
        self._conn.commit()
        return self.get(str(row["id"]))

    def get(self, listing_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(self._SELECT + " WHERE id = %s", (listing_id,)).fetchone()
        return self._row(row) if row else None

    def update(self, listing_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        if not fields:
            return self.get(listing_id)
        window = fields.pop("pickup_window", None)
        for geo_key in ("geo_lat", "geo_lon"):
            if geo_key in fields:
                fields[geo_key] = encrypt_float(fields[geo_key], GEO_KEY_ENV)
        # L1a: column whitelist — dict keys must never reach SQL unchecked.
        # Allowed = the fixed ListingPatch model fields (+ remaining_qty,
        # written by the claim-quantity path in claims.py).
        unknown = [k for k in fields if k not in _LISTING_UPDATE_COLUMNS]
        if unknown:
            raise ValueError(f"refusing to update unknown listing columns: {unknown}")
        sets, params = [], []
        for k, v in fields.items():
            sets.append(f"{k} = %s")
            params.append(v)
        if window is not None:
            sets.append("pickup_window = tstzrange(%s,%s,'[)')")
            params.extend([window[0], window[1]])
        params.append(listing_id)
        self._conn.execute(f"UPDATE listings SET {', '.join(sets)} WHERE id = %s", params)
        self._conn.commit()
        return self.get(listing_id)

    def set_status(self, listing_id: str, status: str) -> dict[str, Any] | None:
        self._conn.execute("UPDATE listings SET status = %s WHERE id = %s", (status, listing_id))
        self._conn.commit()
        return self.get(listing_id)

    def claim(self, listing_id: str, claimer_uid: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "UPDATE listings SET status = 'claimed', claimer_uid = %s "
            "WHERE id = %s AND status = 'live'",
            (claimer_uid, listing_id),
        )
        self._conn.commit()
        return self.get(listing_id) if (cur.rowcount or 0) > 0 else None

    def complete_if_claimed(self, listing_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "UPDATE listings SET status = 'completed' "
            "WHERE id = %s AND status = 'claimed'",
            (listing_id,),
        )
        self._conn.commit()
        return self.get(listing_id) if (cur.rowcount or 0) > 0 else None

    def complete_if_live(self, listing_id: str) -> dict[str, Any] | None:
        # M6: single conditional UPDATE, one commit — no stranded claimed state.
        cur = self._conn.execute(
            "UPDATE listings SET status = 'completed' "
            "WHERE id = %s AND status = 'live'",
            (listing_id,),
        )
        self._conn.commit()
        return self.get(listing_id) if (cur.rowcount or 0) > 0 else None

    def sweep_expired(self, now: datetime) -> int:
        cur = self._conn.execute(
            "UPDATE listings SET status = 'expired' "
            "WHERE status = 'live' AND expires_at IS NOT NULL AND expires_at < %s",
            (now,),
        )
        self._conn.commit()
        return cur.rowcount or 0

    def list_live(self, limit: int | None = None, offset: int = 0,
                  listing_type: str | None = None) -> list[dict[str, Any]]:
        # M9: DB-level pagination — the feed scores one page, not the table.
        query = self._SELECT + " WHERE status = 'live'"
        params: list[Any] = []
        if listing_type is not None:
            query += " AND type = %s"
            params.append(listing_type)
        query += " ORDER BY expires_at NULLS LAST, created_at DESC"
        if limit is not None:
            query += " LIMIT %s OFFSET %s"
            params.extend([limit, offset])
        elif offset:
            query += " OFFSET %s"
            params.append(offset)
        rows = self._conn.execute(query, params).fetchall()
        return [self._row(r) for r in rows]

    def count_live(self, listing_type: str | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM listings WHERE status = 'live'"
        params: list[Any] = []
        if listing_type is not None:
            query += " AND type = %s"
            params.append(listing_type)
        row = self._conn.execute(query, params).fetchone()
        return int(row["n"])

    def list_live_expiring_before(self, cutoff: datetime) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            self._SELECT + " WHERE status = 'live' AND expires_at IS NOT NULL "
            "AND expires_at < %s ORDER BY expires_at",
            (cutoff,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def list_by_owner(self, uid: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            self._SELECT + " WHERE owner_uid = %s ORDER BY created_at DESC",
            (uid,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def decrement_remaining(self, listing_id: str, delta: float) -> dict[str, Any] | None:
        # Single atomic UPDATE: concurrent pickers cannot oversell the harvest.
        # M5a: the delta is cast to NUMERIC so the subtraction never evaluates
        # in float8 — a Python float bound into NUMERIC arithmetic leaves
        # float dust that breaks the "fully picked" (remaining == 0) check.
        # The predicate tolerates tiny NEGATIVE dust (a legit final pick whose
        # decimal expansion lands a hair below zero, e.g. 0.9 - 3x(0.1+0.2));
        # GREATEST clamps it to zero instead of stranding the pick as a 422.
        cur = self._conn.execute(
            "UPDATE listings SET remaining_qty = "
            "GREATEST(COALESCE(remaining_qty, quantity) - %s::numeric, 0) "
            "WHERE id = %s AND COALESCE(remaining_qty, quantity) - %s::numeric "
            ">= -%s::numeric",
            (delta, listing_id, delta, _REMAINING_EPSILON),
        )
        self._conn.commit()
        if not cur.rowcount:
            return None
        return self.get(listing_id)

    def log_harvest_event(self, listing_id: str, recorder_uid: str,
                          delta_kg: float, remaining_after: float) -> None:
        self._conn.execute(
            "INSERT INTO harvest_events (id, listing_id, recorder_uid, delta_kg, remaining_after) "
            "VALUES (%s,%s,%s,%s,%s)",
            (str(uuid.uuid4()), listing_id, recorder_uid, delta_kg, remaining_after),
        )
        self._conn.commit()

    def list_harvest_events(self, listing_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, listing_id, recorder_uid, delta_kg, remaining_after, created_at "
            "FROM harvest_events WHERE listing_id = %s ORDER BY created_at",
            (listing_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            c = d.get("created_at")
            d["created_at"] = c.isoformat() if hasattr(c, "isoformat") else c
            d["delta_kg"] = float(d["delta_kg"])
            d["remaining_after"] = float(d["remaining_after"])
            out.append(d)
        return out


class MemoryListingRepo:
    def __init__(self):
        self._rows: dict[str, dict[str, Any]] = {}
        self._harvest_events: list[dict[str, Any]] = []

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        row = dict(data)
        row["geo_lat"] = encrypt_float(row.get("geo_lat"), GEO_KEY_ENV)
        row["geo_lon"] = encrypt_float(row.get("geo_lon"), GEO_KEY_ENV)
        row.setdefault("created_at", utcnow().isoformat())
        self._rows[row["id"]] = row
        return dict(row)

    def get(self, listing_id: str) -> dict[str, Any] | None:
        row = self._rows.get(listing_id)
        return dict(row) if row else None

    def update(self, listing_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        row = self._rows.get(listing_id)
        if row is None:
            return None
        for geo_key in ("geo_lat", "geo_lon"):
            if geo_key in fields:
                fields[geo_key] = encrypt_float(fields[geo_key], GEO_KEY_ENV)
        row.update(fields)
        return dict(row)

    def set_status(self, listing_id: str, status: str) -> dict[str, Any] | None:
        return self.update(listing_id, {"status": status})

    def claim(self, listing_id: str, claimer_uid: str) -> dict[str, Any] | None:
        row = self._rows.get(listing_id)
        if row is None or row.get("status") != "live":
            return None
        row["status"] = "claimed"
        row["claimer_uid"] = claimer_uid
        return dict(row)

    def complete_if_claimed(self, listing_id: str) -> dict[str, Any] | None:
        row = self._rows.get(listing_id)
        if row is None or row.get("status") != "claimed":
            return None
        row["status"] = "completed"
        return dict(row)

    def complete_if_live(self, listing_id: str) -> dict[str, Any] | None:
        row = self._rows.get(listing_id)
        if row is None or row.get("status") != "live":
            return None
        row["status"] = "completed"
        return dict(row)

    def sweep_expired(self, now: datetime) -> int:
        n = 0
        for row in self._rows.values():
            exp = row.get("expires_at")
            try:
                exp_dt = datetime.fromisoformat(exp) if isinstance(exp, str) else exp
            except (ValueError, TypeError):
                continue
            if row["status"] == "live" and exp_dt and exp_dt < now:
                row["status"] = "expired"
                n += 1
        return n

    def list_live(self, limit: int | None = None, offset: int = 0,
                  listing_type: str | None = None) -> list[dict[str, Any]]:
        rows = [r for r in self._rows.values()
                if r.get("status") == "live"
                and (listing_type is None or r.get("type") == listing_type)]
        # Mirror the Postgres order: expires_at ascending (nulls last), then
        # created_at descending (stable sorts, applied in reverse priority).
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        rows.sort(key=lambda r: (r.get("expires_at") is None, str(r.get("expires_at") or "")))
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return [dict(r) for r in rows]

    def count_live(self, listing_type: str | None = None) -> int:
        return sum(1 for r in self._rows.values()
                   if r.get("status") == "live"
                   and (listing_type is None or r.get("type") == listing_type))

    def list_live_expiring_before(self, cutoff: datetime) -> list[dict[str, Any]]:
        out = []
        for r in self._rows.values():
            if r.get("status") != "live":
                continue
            exp = r.get("expires_at")
            try:
                exp_dt = datetime.fromisoformat(exp) if isinstance(exp, str) else exp
            except (ValueError, TypeError):
                continue
            if exp_dt is not None:
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt < cutoff:
                    out.append(dict(r))
        out.sort(key=lambda r: str(r.get("expires_at") or ""))
        return out

    def list_by_owner(self, uid: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows.values() if r.get("owner_uid") == uid]

    def decrement_remaining(self, listing_id: str, delta: float) -> dict[str, Any] | None:
        row = self._rows.get(listing_id)
        if row is None:
            return None
        avail = row.get("remaining_qty")
        if avail is None:
            avail = row.get("quantity")
        if avail is None:
            return None
        # M5a: epsilon-tolerant overpick check — a legitimate final pick that
        # lands a hair below zero (float dust, e.g. 0.9 - 3x(0.1+0.2)) is
        # still a legal pick; only a real shortfall is rejected.
        if float(avail) < delta and delta - float(avail) >= _REMAINING_EPSILON:
            return None
        new_remaining = float(avail) - delta
        if abs(new_remaining) < _REMAINING_EPSILON:  # float dust -> zero
            new_remaining = 0.0
        row["remaining_qty"] = new_remaining
        return dict(row)

    def log_harvest_event(self, listing_id: str, recorder_uid: str,
                          delta_kg: float, remaining_after: float) -> None:
        self._harvest_events.append({
            "id": str(uuid.uuid4()),
            "listing_id": listing_id,
            "recorder_uid": recorder_uid,
            "delta_kg": float(delta_kg),
            "remaining_after": float(remaining_after),
            "created_at": utcnow().isoformat(),
        })

    def list_harvest_events(self, listing_id: str) -> list[dict[str, Any]]:
        return [dict(e) for e in self._harvest_events if e["listing_id"] == listing_id]


def get_listing_repo(conn=Depends(get_db_conn)) -> ListingRepo:
    return CachedListingRepo(PostgresListingRepo(conn))


# ---------------------------------------------------------------- API models

class PickupWindow(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end", mode="after")
    @classmethod
    def _utc_window(cls, v):
        return _coerce_utc(v)


class ListingIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(pattern="^(seedling|harvest|tree)$")
    photos: list[str] = Field(min_length=1)
    variety: str | None = Field(default=None, max_length=120)
    quantity: float | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=20)
    credit_cost: int = Field(ge=1, le=100)
    pickup_window: PickupWindow | None = None
    expires_at: datetime | None = None
    geo_lat: float | None = Field(default=None, ge=-90, le=90)
    geo_lon: float | None = Field(default=None, ge=-180, le=180)
    spray_disclosure: str = Field(min_length=1, max_length=2000)
    status: str = Field(default="draft", pattern="^(draft|live)$")
    visit_rules: str | None = Field(default=None, max_length=2000)
    # AND-125/AND-126 (create form): optional pot size + plant age (seedlings),
    # and the pickup-window length in days (defaults to 4).
    pot_size: str | None = Field(default=None, alias="potSize", max_length=40)
    plant_age_years: str | None = Field(default=None, alias="plantAgeYears", max_length=40)
    pickup_window_days: int = Field(default=4, alias="pickupWindowDays", ge=1, le=14)

    @field_validator("expires_at", mode="after")
    @classmethod
    def _utc_expires(cls, v):
        return _coerce_utc(v)


class ListingPatch(BaseModel):
    photos: list[str] | None = Field(default=None, min_length=1)
    variety: str | None = Field(default=None, max_length=120)
    quantity: float | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=20)
    credit_cost: int | None = Field(default=None, ge=1, le=100)
    pickup_window: PickupWindow | None = None
    expires_at: datetime | None = None
    spray_disclosure: str | None = Field(default=None, min_length=1, max_length=2000)
    # H1: PATCH may only move between draft/live/cancelled. claimed/completed
    # are reachable ONLY through the claim/confirm/exchange endpoints (which
    # move credits and set claimer_uid); letting an owner PATCH straight to
    # claimed/completed would bypass the entire claim flow.
    status: str | None = Field(default=None, pattern="^(draft|live|cancelled)$")
    visit_rules: str | None = Field(default=None, max_length=2000)

    @field_validator("expires_at", mode="after")
    @classmethod
    def _utc_expires(cls, v):
        return _coerce_utc(v)


# L1a: column whitelist for PostgresListingRepo.update — mirrors the fixed
# ListingPatch model (+ remaining_qty, written by the claim-quantity path in
# claims.py). Defined after the model so it tracks the schema.
_LISTING_UPDATE_COLUMNS = frozenset(ListingPatch.model_fields) | {"remaining_qty"}


# ---------------------------------------------------------------- retention (M19)

# Retention windows, enforced by the internal sweep. Tables not listed here
# are append-only BY DESIGN and never purged:
# - credit ledger (credit_entries): financial record; expiry is logical
#   (balance() excludes expired lots) so the audit trail must survive.
# - harvest_events: pick audit log.
# - moderation_views: safety audit log (append-only is the point).
# - reports / strikes / enforcement: trust-&-safety history.
# - messages: user content; removed only via account-deletion cascade.
NOTIFICATION_LOG_RETENTION_DAYS = 90
RESOLVED_DISPUTE_RETENTION_DAYS = 730  # 2 years
TERMINAL_LISTING_MEDIA_GC_DAYS = 180

_UPLOAD_PUBLIC_MARKER = "/uploads/public/"


def _upload_key_from_url(url: Any) -> str | None:
    """Extract the upload registry key from a listing photo URL, if it is one
    of ours (``…/uploads/public/<key>``). External URLs return None."""
    if not isinstance(url, str) or _UPLOAD_PUBLIC_MARKER not in url:
        return None
    key = url.split(_UPLOAD_PUBLIC_MARKER, 1)[1].split("?", 1)[0].strip("/")
    return key or None


#: Callable the retention sweep uses to refcount-release GCS objects for
#: swept photo URLs. Takes the listing's photo URLs; returns GCS objects
#: deleted. ``None`` (or a no-op) on non-GCS backends.
GCSReleaser = Callable[[list[str]], int]


class RetentionRepo(Protocol):
    def purge_notification_log(self, older_than_days: int) -> int:
        """Delete notification_log rows older than the window. Returns count."""
        ...
    def purge_resolved_disputes(self, older_than_days: int) -> int:
        """Delete resolved disputes older than the window. Returns count."""
        ...
    def gc_terminal_listing_media(
        self,
        older_than_days: int,
        gcs_releaser: GCSReleaser | None = None,
    ) -> int:
        """Media GC for terminal listings (M19). For listings in a terminal
        state (completed/expired/cancelled) older than the window, delete the
        matching ``uploads`` registry rows and clear the listing's photos.
        ``gcs_releaser`` (optional) is called with the swept photo URLs so the
        GCS backend can refcount-release the stored images; it is a backstop
        for rows not already released at completion time. Returns the number
        of registry rows deleted."""
        ...


class PostgresRetentionRepo:
    def __init__(self, conn):
        self._conn = conn

    def purge_notification_log(self, older_than_days: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM notification_log WHERE sent_at < now() - make_interval(days => %s)",
            (older_than_days,),
        )
        self._conn.commit()
        return cur.rowcount or 0

    def purge_resolved_disputes(self, older_than_days: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM disputes WHERE status = 'resolved' AND resolved_at IS NOT NULL "
            "AND resolved_at < now() - make_interval(days => %s)",
            (older_than_days,),
        )
        self._conn.commit()
        return cur.rowcount or 0

    def gc_terminal_listing_media(
        self,
        older_than_days: int,
        gcs_releaser: GCSReleaser | None = None,
    ) -> int:
        rows = self._conn.execute(
            "SELECT id, photos FROM listings "
            "WHERE status IN ('completed','expired','cancelled') "
            "AND created_at < now() - make_interval(days => %s) "
            "AND photos <> '{}'",
            (older_than_days,),
        ).fetchall()
        keys: set[str] = set()
        ids: list[Any] = []
        swept_urls: list[str] = []
        for r in rows:
            ids.append(r["id"])
            for url in r["photos"] or []:
                swept_urls.append(url)
                key = _upload_key_from_url(url)
                if key:
                    keys.add(key)
        deleted = 0
        if keys:
            # After the C7 fix, serve_public 404s without a finalized registry
            # row — deleting the row stops the bytes being served. Physical
            # GCS byte deletion happens via gcs_releaser below (refcounted);
            # local-stub bytes are dev-only and left to the OS.
            cur = self._conn.execute(
                "DELETE FROM uploads WHERE key = ANY(%s)", (list(keys),))
            deleted = cur.rowcount or 0
        if ids:
            self._conn.execute(
                "UPDATE listings SET photos = '{}' WHERE id = ANY(%s)", (ids,))
        self._conn.commit()
        if gcs_releaser is not None and swept_urls:
            # Backstop: normally already released at completion time; this
            # catches rows from before the release hooks existed.
            gcs_releaser(swept_urls)
        return deleted


class MemoryRetentionRepo:
    """In-memory retention sweeps (tests). Operates on the memory stores it
    is handed — pass the fakes' ``_log``/``_rows`` (or plain lists/dicts)
    from the test fixture."""

    def __init__(
        self,
        notification_log: list[dict[str, Any]] | None = None,
        disputes: list[dict[str, Any]] | None = None,
        uploads: dict[str, dict[str, Any]] | None = None,
        listings: dict[str, dict[str, Any]] | None = None,
    ):
        self._log = notification_log if notification_log is not None else []
        self._disputes = disputes if disputes is not None else []
        self._uploads = uploads if uploads is not None else {}
        self._listings = listings if listings is not None else {}

    @staticmethod
    def _as_aware(value: Any) -> datetime | None:
        if value is None:
            return None
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(dt, datetime):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def purge_notification_log(self, older_than_days: int) -> int:
        cutoff = utcnow() - timedelta(days=older_than_days)
        keep = [e for e in self._log
                if (self._as_aware(e.get("sent_at")) or utcnow()) >= cutoff]
        purged = len(self._log) - len(keep)
        self._log[:] = keep
        return purged

    def purge_resolved_disputes(self, older_than_days: int) -> int:
        cutoff = utcnow() - timedelta(days=older_than_days)
        keep = [d for d in self._disputes
                if not (d.get("status") == "resolved"
                        and (self._as_aware(d.get("resolved_at")) or utcnow()) < cutoff)]
        purged = len(self._disputes) - len(keep)
        self._disputes[:] = keep
        return purged

    def gc_terminal_listing_media(
        self,
        older_than_days: int,
        gcs_releaser: GCSReleaser | None = None,
    ) -> int:
        cutoff = utcnow() - timedelta(days=older_than_days)
        deleted = 0
        swept_urls: list[str] = []
        for row in self._listings.values():
            if row.get("status") not in ("completed", "expired", "cancelled"):
                continue
            created = self._as_aware(row.get("created_at"))
            if created is None or created >= cutoff:
                continue
            for url in row.get("photos") or []:
                swept_urls.append(url)
                key = _upload_key_from_url(url)
                if key and self._uploads.pop(key, None) is not None:
                    deleted += 1
            row["photos"] = []
        if gcs_releaser is not None and swept_urls:
            gcs_releaser(swept_urls)
        return deleted


def get_retention_repo(conn=Depends(get_db_conn)) -> RetentionRepo:
    return PostgresRetentionRepo(conn)


def _validate_common(data: ListingIn | ListingPatch) -> None:
    for url in data.photos or []:
        if not url.startswith(("https://", "http://")):
            raise HTTPException(400, {"code": "invalid_photo_url", "message": "photo URLs must be http(s)"})
    window = data.pickup_window
    if window and window.end <= window.start:
        raise HTTPException(400, {"code": "invalid_window", "message": "pickup window end must be after start"})
    if data.expires_at and data.expires_at <= utcnow():
        raise HTTPException(400, {"code": "invalid_expiry", "message": "expires_at must be in the future"})


@router.post("/listings", status_code=201)
def create_listing(
    data: ListingIn,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    user_repo: UserRepo = Depends(get_user_repo),
    want_repo: WantRepo = Depends(get_want_repo),
    notify_repo: NotificationRepo = Depends(get_notification_repo),
) -> dict[str, Any]:
    _validate_common(data)
    if user_repo.get(uid) is None:
        raise HTTPException(400, {"code": "profile_required",
                                  "message": "Create a profile (POST /v1/users) before listing"})
    row = repo.create({
        "id": str(uuid.uuid4()),
        "owner_uid": uid,
        "type": data.type,
        "photos": data.photos,
        "variety": data.variety,
        "quantity": data.quantity,
        "unit": data.unit,
        "credit_cost": data.credit_cost,
        "pickup_window": (data.pickup_window.start.isoformat(), data.pickup_window.end.isoformat())
        if data.pickup_window else None,
        "expires_at": data.expires_at.isoformat() if data.expires_at else None,
        "geo_lat": data.geo_lat,
        "geo_lon": data.geo_lon,
        "spray_disclosure": data.spray_disclosure.strip(),
        "status": data.status,
        # Harvest listings track what is left to pick (API-040).
        "remaining_qty": data.quantity if data.type == "harvest" else None,
        "visit_rules": data.visit_rules.strip() if data.visit_rules else None,
        "pot_size": data.pot_size.strip() if data.pot_size else None,
        "plant_age_years": data.plant_age_years.strip() if data.plant_age_years else None,
        "pickup_window_days": data.pickup_window_days,
    })
    if row["status"] == "live":
        # A listing going live is the match event (API-030).
        notify_matches(row, want_repo, notify_repo)
    return public_listing(row, viewer_uid=uid,
                          owners=batch_owners(user_repo, [row]))


@router.get("/listings/mine")
def list_my_listings(
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Listings owned by the current user, any status, newest first."""
    rows = repo.list_by_owner(uid)
    owners = batch_owners(user_repo, rows)
    return {"listings": [public_listing(r, viewer_uid=uid, owners=owners)
                         for r in rows]}


@router.get("/want-list/matches", tags=["want-list"])
def get_want_matches(
    uid: str = Depends(get_current_uid),
    want_repo: WantRepo = Depends(get_want_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Live listings matching the current user's want-list entries.

    Defined here (not in wantlist.py) to avoid a listings<->wantlist
    circular import; the route path is unchanged.
    """
    entries = want_repo.list_for_user(uid)
    if not entries:
        return {"items": []}
    matched = [row for row in listing_repo.list_live(limit=200)
               if find_matches(row, entries)]
    owners = batch_owners(user_repo, matched)
    return {"items": [public_listing(row, viewer_uid=uid, owners=owners)
                      for row in matched]}


@router.get("/listings/{listing_id}")
def get_listing(
    listing_id: str,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    row = repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    # L8: the FirebaseAuthMiddleware guarantees an authenticated viewer here;
    # claimer_uid is hidden from everyone except owner/claimer.
    return public_listing(row, viewer_uid=uid,
                          owners=batch_owners(user_repo, [row]))


@router.patch("/listings/{listing_id}")
def patch_listing(
    listing_id: str,
    data: ListingPatch,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    want_repo: WantRepo = Depends(get_want_repo),
    notify_repo: NotificationRepo = Depends(get_notification_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    row = repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    ensure_owner(row["owner_uid"], uid)
    if row["status"] not in EDITABLE_STATUSES:
        raise HTTPException(422, {"code": "listing_locked",
                                  "message": f"Cannot edit a listing in status '{row['status']}'"})
    _validate_common(data)
    fields: dict[str, Any] = {k: v for k, v in data.model_dump(exclude_unset=True).items()
                              if v is not None and k != "status"}
    if "pickup_window" in fields and fields["pickup_window"]:
        w = fields["pickup_window"]
        fields["pickup_window"] = (w["start"], w["end"])
    if data.status and data.status != row["status"]:
        if not can_transition(row["status"], data.status):
            raise HTTPException(422, {"code": "invalid_transition",
                                      "message": f"Cannot move listing from '{row['status']}' to '{data.status}'"})
        fields["status"] = data.status
    if "spray_disclosure" in fields:
        fields["spray_disclosure"] = fields["spray_disclosure"].strip()
    if "visit_rules" in fields and fields["visit_rules"]:
        fields["visit_rules"] = fields["visit_rules"].strip()
    updated = repo.update(listing_id, fields)
    if updated and row["status"] != "live" and updated.get("status") == "live":
        # draft -> live is the match event (API-030).
        notify_matches(updated, want_repo, notify_repo)
    return public_listing(updated, viewer_uid=uid,
                          owners=batch_owners(user_repo, [updated]))


@router.post("/listings/{listing_id}/cancel")
def cancel_listing(
    listing_id: str,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    images_repo: StoredImagesRepo = Depends(get_images_repo),
    blob_store: GCSBlobStore | None = Depends(get_blob_store_or_none),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    row = repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    ensure_owner(row["owner_uid"], uid)
    if not can_transition(row["status"], "cancelled"):
        raise HTTPException(422, {"code": "invalid_transition",
                                  "message": f"Cannot cancel a listing in status '{row['status']}'"})
    updated = repo.set_status(listing_id, "cancelled")
    # Cancelled is terminal: the photos no longer back an active listing.
    # Refcounted release (no-op unless STORAGE_BACKEND=gcs).
    release_listing_images(
        (updated or row).get("photos") or [],
        images_repo=images_repo,
        blob_store=blob_store,
        bucket=get_settings().gcs_bucket,
    )
    return public_listing(updated, viewer_uid=uid,
                          owners=batch_owners(user_repo, [updated]))


# Epsilon for the "fully picked" check (M5a): NUMERIC arithmetic is exact,
# but the API compares in float — treat anything this close to zero as zero
# so float dust can never strand a listing live with ~0 quantity.
_REMAINING_EPSILON = 1e-6


class HarvestEventIn(BaseModel):
    listing_id: str = Field(min_length=1)
    delta_kg: float = Field(gt=0, le=10000)


@router.post("/harvest-events", status_code=201, tags=["listings"])
def record_harvest_event(
    data: HarvestEventIn,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    images_repo: StoredImagesRepo = Depends(get_images_repo),
    blob_store: GCSBlobStore | None = Depends(get_blob_store_or_none),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Record kilos picked from a harvest listing (owner only, live only).

    Atomic decrement — concurrent pickers cannot oversell. When the harvest
    is fully picked (remaining hits ~0) the listing completes via one
    conditional live -> completed flip (M6): a single commit, so a crash can
    never strand the listing in claimed with no sweeper.
    """
    row = repo.get(data.listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    ensure_owner(row["owner_uid"], uid)
    if row["type"] != "harvest":
        raise HTTPException(422, {"code": "not_harvest_listing",
                                  "message": "Harvest events apply to harvest listings only"})
    if row["status"] != "live":
        raise HTTPException(422, {"code": "listing_not_live",
                                  "message": f"Cannot record a pick on a '{row['status']}' listing"})
    if row.get("remaining_qty") is None:
        raise HTTPException(422, {"code": "quantity_not_tracked",
                                  "message": "This harvest listing has no quantity to pick from"})
    updated = repo.decrement_remaining(data.listing_id, data.delta_kg)
    if updated is None:
        raise HTTPException(422, {"code": "insufficient_quantity",
                                  "message": "Not that much harvest remaining"})
    remaining = float(updated["remaining_qty"])
    repo.log_harvest_event(data.listing_id, uid, data.delta_kg, remaining)
    if abs(remaining) < _REMAINING_EPSILON:
        # Fully picked: one atomic flip, no two-commit walk (M6).
        remaining = 0.0
        flipped = repo.complete_if_live(data.listing_id)
        updated = flipped if flipped is not None else repo.get(data.listing_id)
        if flipped is not None:
            # The harvest is done: its photos no longer back an active
            # listing. Refcounted release (no-op unless STORAGE_BACKEND=gcs).
            release_listing_images(
                flipped.get("photos"),
                images_repo=images_repo,
                blob_store=blob_store,
                bucket=get_settings().gcs_bucket,
            )
    return {
        "listing": public_listing(updated, viewer_uid=uid,
                                  owners=batch_owners(user_repo, [updated])),
        "delta_kg": data.delta_kg,
        "remaining_kg": remaining,
    }


@router.get("/listings/{listing_id}/harvest-events", tags=["listings"])
def get_harvest_events(
    listing_id: str,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
) -> dict[str, Any]:
    """Pick audit log for a harvest listing.

    Visible to any authenticated user (the route requires auth; the
    recorder_uid values are the audit trail)."""
    row = repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    return {"listing_id": listing_id, "events": repo.list_harvest_events(listing_id)}


def _tree_card(row: dict[str, Any]) -> dict[str, Any]:
    """Compact public tree card: id, variety, approx location, ripe window.

    Same encrypted-geo contract as ``public_listing``: decrypt in-process,
    immediately before fuzzing."""
    lat = decrypt_float(row.get("geo_lat"), GEO_KEY_ENV)
    lon = decrypt_float(row.get("geo_lon"), GEO_KEY_ENV)
    if lat is not None and lon is not None:
        flat, flon = fuzz_location_for_listing(lat, lon, row["id"])  # M1: stable per listing
    else:
        flat, flon = None, None
    window = row.get("pickup_window")
    return {
        "id": str(row["id"]),
        "variety": row.get("variety"),
        "geo_lat": flat,
        "geo_lon": flon,
        "ripe_window": {"start": window[0], "end": window[1]} if window else None,
        "expires_at": row.get("expires_at"),
        "spray_disclosure": row.get("spray_disclosure"),
        "visit_rules": row.get("visit_rules"),
    }


@router.get("/trees", tags=["trees"])
def list_trees(
    repo: ListingRepo = Depends(get_listing_repo),
) -> dict[str, Any]:
    """Live tree listings for pick-your-own (API-135)."""
    trees = [r for r in repo.list_live() if r.get("type") == "tree"]
    return {"trees": [_tree_card(r) for r in trees]}


@router.post("/trees/{listing_id}/ripe-alert", tags=["trees"])
def ripe_alert(
    listing_id: str,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    want_repo: WantRepo = Depends(get_want_repo),
    notify_repo: NotificationRepo = Depends(get_notification_repo),
) -> dict[str, Any]:
    """Notify want-list matches that a tree is ripe for picking (API-050).

    Owner-only. The notify layer's 24h dedupe on ref ``<listing>:<date>``
    makes this safe to re-trigger — each user gets at most one ripe alert
    per tree per day.
    """
    from .wantlist import RIPE_ALERT_CATEGORY

    row = repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    ensure_owner(row["owner_uid"], uid)
    if row["type"] != "tree":
        raise HTTPException(422, {"code": "not_tree_listing",
                                  "message": "Ripe alerts apply to tree listings only"})
    today = utcnow().date().isoformat()
    ref = f"{listing_id}:{today}"
    notified = 0
    # M9: DB-level matching — one indexed query, not a full want_list scan.
    # getattr fallback keeps third-party WantRepo implementations working.
    find = getattr(want_repo, "find_matches_for_listing", None)
    matches = find(row) if find is not None else find_matches(row, want_repo.list_all())
    for entry in matches:
        result = send_notification(
            entry["user_uid"],
            RIPE_ALERT_CATEGORY,
            "Fruit is ripe near you",
            f"{row.get('variety') or 'A tree'} you want is ripe for picking.",
            data={"listing_id": listing_id},
            ref=ref,
            repo=notify_repo,
        )
        if result["status"] in ("sent", "would_send"):
            notified += 1
    return {"listing_id": listing_id, "notified": notified, "date": today}


@internal_router.get("/warm")
def warm_ping(
    request: Request,
    conn=Depends(get_db_conn),
) -> dict[str, Any]:
    """Keep-warm ping for Cloud Scheduler: touches Neon so it doesn't suspend.

    Auth: same shared secret as the sweep job (x-sweep-secret header), exempt
    from Firebase auth via EXEMPT_PATHS. Runs SELECT 1 through the normal
    pooled connection — the DB touch is what keeps Neon's compute from
    idling into suspend, and the request itself keeps a Cloud Run instance
    warm. The response carries the DB round-trip ms for scheduler-log
    observability; a waking Neon shows up here as a large db_ms, not an
    error (get_db_conn already retries through the wake).
    """
    secret = get_settings().sweep_secret
    if not secret:
        raise HTTPException(503, {"code": "sweep_not_configured",
                                  "message": "SWEEP_SECRET is not configured"})
    presented = request.headers.get("x-sweep-secret", "")
    if not hmac.compare_digest(secret, presented):
        raise HTTPException(401, {"code": "unauthorized",
                                  "message": "Bad sweep secret"})

    start = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    db_ms = (time.perf_counter() - start) * 1000
    return {"status": "warm", "db_ms": round(db_ms, 1)}


@internal_router.post("/sweep")
def sweep_expired(
    request: Request,
    repo: ListingRepo = Depends(get_listing_repo),
    notify_repo: NotificationRepo = Depends(get_notification_repo),
    retention_repo: RetentionRepo = Depends(get_retention_repo),
    images_repo: StoredImagesRepo = Depends(get_images_repo),
    blob_store: GCSBlobStore | None = Depends(get_blob_store_or_none),
) -> dict[str, Any]:
    """Idempotent expiry job. Auth: shared secret header (NOT a user token).

    Also fires expiry nudges: live listings within 48h / 12h of expiry get a
    nudge to the owner via the notify pipeline (per-mark dedupe refs keep
    this idempotent across sweep runs). The nudge scan is bounded to listings
    expiring within 48h (M9) instead of iterating every live listing.

    Retention (M19), all idempotent: notification_log older than 90d is
    purged, resolved disputes older than 2y are purged, and photos of
    terminal listings (completed/expired/cancelled) older than 180d are
    garbage-collected from the uploads registry (unserved after the C7
    registry gate) with the listing's photo list cleared; on the GCS backend
    the stored images are additionally refcount-released (deleted only when
    the last referencing listing is gone). The credit ledger,
    harvest_events, moderation_views, and reports/strikes/enforcement are
    append-only by design and never purged (see module docstring).
    """
    secret = get_settings().sweep_secret
    if not secret:
        raise HTTPException(503, {"code": "sweep_not_configured",
                                  "message": "SWEEP_SECRET is not configured"})
    presented = request.headers.get("x-sweep-secret", "")
    if not hmac.compare_digest(secret, presented):
        raise HTTPException(401, {"code": "unauthorized", "message": "Bad sweep secret"})

    def _release_swept(urls: list[str]) -> int:
        # Backstop for stored_images rows not already released at completion
        # time (e.g. completed before the release hooks existed). No-op off
        # GCS — blob_store is not None here by construction.
        return release_listing_images(
            urls,
            images_repo=images_repo,
            blob_store=blob_store,
            bucket=get_settings().gcs_bucket,
        )

    now = utcnow()
    n = repo.sweep_expired(now)
    nudged = {48: 0, 12: 0}
    for row in repo.list_live_expiring_before(now + timedelta(hours=48)):
        exp = _parse_expiry(row.get("expires_at"))
        if exp is None:
            continue
        hours_left = (exp - now).total_seconds() / 3600
        if hours_left <= 0:
            continue
        mark = 12 if hours_left <= 12 else (48 if hours_left <= 48 else None)
        if mark is None:
            continue
        result = on_listing_expiry_nudge(row, mark, notify_repo=notify_repo, now=now)
        nudged[mark] += result.get("delivered", 0)
    retention = {
        "notification_log_purged": retention_repo.purge_notification_log(
            NOTIFICATION_LOG_RETENTION_DAYS),
        "resolved_disputes_purged": retention_repo.purge_resolved_disputes(
            RESOLVED_DISPUTE_RETENTION_DAYS),
        "terminal_media_gc": retention_repo.gc_terminal_listing_media(
            TERMINAL_LISTING_MEDIA_GC_DAYS,
            gcs_releaser=_release_swept if blob_store is not None else None),
    }
    return {"expired": n, "nudged_48h": nudged[48], "nudged_12h": nudged[12],
            "retention": retention}


def _parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

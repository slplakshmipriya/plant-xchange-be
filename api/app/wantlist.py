"""Seasonal want-list + match engine (API-030).

Want-list is the primary discovery mechanism (PRD pillar 1): users state what
they want to grow; when a matching listing goes **live**, each matching user
gets one push (category ``"match"``, ref = listing id — the notify layer's
24h dedupe guarantees at-most-one per listing per day).

Matching rule: variety substring, case-insensitive, either direction
(``"tomato"`` matches ``"Cherokee Purple tomato"`` and vice versa), AND the
listing type must be in the entry's ``types`` list (empty = any type).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field

from .auth import ensure_owner, get_current_uid
from .cache import cache
from .db import get_db_conn
from .notify import NotificationRepo, get_notification_repo, send_notification


class WantDuplicateError(Exception):
    """Raised when a want-list entry duplicates an existing variety."""

router = APIRouter(prefix="/v1/want-list", tags=["want-list"])

MATCH_CATEGORY = "match"
RIPE_ALERT_CATEGORY = "ripe_alert"


def variety_matches(want: str, listing_variety: str | None) -> bool:
    """Substring match, case-insensitive, either direction."""
    w = (want or "").strip().lower()
    lv = (listing_variety or "").strip().lower()
    return bool(w and lv) and (w in lv or lv in w)


def _matches_types(entry_types: list[str], listing_type: str) -> bool:
    return not entry_types or listing_type in entry_types


class WantIn(BaseModel):
    variety: str = Field(min_length=1, max_length=120)
    types: list[str] = Field(default_factory=list, max_length=3)


class WantPatch(BaseModel):
    variety: str | None = Field(default=None, min_length=1, max_length=120)
    types: list[str] | None = Field(default=None, max_length=3)


def _validate_types(types: list[str] | None) -> None:
    from .listings import LISTING_TYPES

    for t in types or []:
        if t not in LISTING_TYPES:
            raise HTTPException(400, {"code": "invalid_want_type",
                                      "message": f"Unknown listing type '{t}'"})


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "user_uid": row["user_uid"],
        "variety": row["variety"],
        "types": list(row.get("types") or []),
        "created_at": row.get("created_at"),
    }


class WantRepo(Protocol):
    def create(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def list_for_user(self, uid: str) -> list[dict[str, Any]]: ...
    def get(self, want_id: str) -> dict[str, Any] | None: ...
    def update(self, want_id: str, fields: dict[str, Any]) -> dict[str, Any] | None: ...
    def delete(self, want_id: str) -> bool: ...
    def list_all(self) -> list[dict[str, Any]]: ...
    def find_matches_for_listing(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Want-list entries matching a listing row (M9: DB-level, one query).

        Same semantics as ``find_matches``: excludes the listing owner,
        variety substring either direction (case-insensitive), listing type
        in the entry's types (empty = any)."""
        ...


# L1b: column whitelist for PostgresWantRepo.update — the fixed WantPatch
# model fields. Dict keys must never reach SQL unchecked.
_WANT_UPDATE_COLUMNS = frozenset({"variety", "types"})


def _like_escape(value: str) -> str:
    """Escape LIKE metacharacters so a variety matches literally (the Python
    matcher uses ``in``, not patterns)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PostgresWantRepo:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        c = d.get("created_at")
        d["created_at"] = c.isoformat() if hasattr(c, "isoformat") else c
        return d

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            row = self._conn.execute(
                "INSERT INTO want_list (id, user_uid, variety, types) VALUES (%s,%s,%s,%s) RETURNING *",
                (data["id"], data["user_uid"], data["variety"], data.get("types") or []),
            ).fetchone()
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            # Race guard: the endpoint checks for duplicates first, but two
            # concurrent creates can still collide on the unique index.
            # Match the structured diag, never the exception text.
            if (isinstance(exc, pg_errors.UniqueViolation)
                    and getattr(exc.diag, "constraint_name", None)
                    == "want_list_user_variety_uidx"):
                raise WantDuplicateError(
                    f"duplicate want variety for user {data.get('user_uid')}"
                ) from exc
            raise
        return self._row(row)

    def list_for_user(self, uid: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM want_list WHERE user_uid = %s ORDER BY created_at", (uid,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, want_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM want_list WHERE id = %s", (want_id,)).fetchone()
        return self._row(row) if row else None

    def update(self, want_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        if not fields:
            return self.get(want_id)
        # L1b: column whitelist — dict keys must never reach SQL unchecked.
        unknown = [k for k in fields if k not in _WANT_UPDATE_COLUMNS]
        if unknown:
            raise ValueError(f"refusing to update unknown want_list columns: {unknown}")
        sets = ", ".join(f"{k} = %s" for k in fields)
        try:
            self._conn.execute(
                f"UPDATE want_list SET {sets} WHERE id = %s", (*fields.values(), want_id)
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            if (isinstance(exc, pg_errors.UniqueViolation)
                    and getattr(exc.diag, "constraint_name", None)
                    == "want_list_user_variety_uidx"):
                raise WantDuplicateError(
                    f"duplicate want variety on update {want_id}"
                ) from exc
            raise
        return self.get(want_id)

    def delete(self, want_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM want_list WHERE id = %s", (want_id,))
        self._conn.commit()
        return (cur.rowcount or 0) > 0

    def list_all(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM want_list").fetchall()
        return [self._row(r) for r in rows]

    def find_matches_for_listing(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """DB-level match (M9): one indexed query instead of scanning the
        whole want_list table in Python.

        Semantics mirror ``find_matches`` exactly: owner excluded, variety
        substring in EITHER direction (case-insensitive, literal — LIKE
        metacharacters escaped), and the listing type must be in the entry's
        types (empty array = any type). The ``lower(variety) LIKE '%…%'``
        arm is served by the pg_trgm GIN index from migration 0030; the
        ``position(...)`` arm covers the reverse direction literally.
        """
        variety = (row.get("variety") or "").strip()
        if not variety:
            return []
        lowered = variety.lower()
        rows = self._conn.execute(
            "SELECT * FROM want_list "
            "WHERE user_uid <> %s "
            "AND variety <> '' "
            "AND (lower(variety) LIKE '%%' || %s || '%%' ESCAPE '\\' "
            "     OR position(lower(variety) in lower(%s)) > 0) "
            "AND (cardinality(types) = 0 OR %s = ANY(types))",
            (row.get("owner_uid"), _like_escape(lowered), lowered, row.get("type")),
        ).fetchall()
        return [self._row(r) for r in rows]


class MemoryWantRepo:
    def __init__(self):
        self._rows: dict[str, dict[str, Any]] = {}

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        row = dict(data)
        self._rows[row["id"]] = row
        return dict(row)

    def list_for_user(self, uid: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows.values() if r["user_uid"] == uid]

    def get(self, want_id: str) -> dict[str, Any] | None:
        row = self._rows.get(want_id)
        return dict(row) if row else None

    def update(self, want_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        row = self._rows.get(want_id)
        if row is None:
            return None
        row.update(fields)
        return dict(row)

    def delete(self, want_id: str) -> bool:
        return self._rows.pop(want_id, None) is not None

    def list_all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows.values()]

    def find_matches_for_listing(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        # Memory twin of the DB-level matcher: same pure function, so the
        # memory and Postgres paths agree by construction.
        return find_matches(row, self.list_all())


def get_want_repo(conn=Depends(get_db_conn)) -> WantRepo:
    return PostgresWantRepo(conn)


def _entry_tier(row: dict[str, Any], entry: dict[str, Any]) -> int:
    """Match tier of one want-list entry against a listing row.

    0 = exact variety match (case-insensitive equality), 1 = inexact
    (substring in either direction, case-insensitive), 2 = no match.
    Eligibility mirrors ``find_matches``: the entry's owner is excluded
    (a viewer never matches their own listing) and the listing type must
    be in the entry's types (empty = any).
    """
    if entry["user_uid"] == row.get("owner_uid"):
        return 2
    if not _matches_types(list(entry.get("types") or []), row.get("type")):
        return 2
    want_var = (entry.get("variety") or "").strip().lower()
    list_var = (row.get("variety") or "").strip().lower()
    if not want_var or not list_var:
        return 2
    if want_var == list_var:
        return 0
    if want_var in list_var or list_var in want_var:
        return 1
    return 2


def match_tier(row: dict[str, Any], entries: list[dict[str, Any]]) -> int:
    """Best match tier across entries: 0 exact, 1 inexact, 2 none.

    ``entries`` are the viewer's want-list rows. Drives feed ranking;
    ``find_matches`` (any tier < 2) keeps the boolean semantics used by
    the want-matches endpoint and match notifications.
    """
    best = 2
    for e in entries:
        tier = _entry_tier(row, e)
        if tier == 0:
            return 0
        if tier < best:
            best = tier
    return best


def find_matches(row: dict[str, Any], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Want-list entries matching a listing row. Excludes the listing owner."""
    return [e for e in entries if _entry_tier(row, e) < 2]


def notify_matches(
    row: dict[str, Any],
    want_repo: WantRepo,
    notify_repo: NotificationRepo,
) -> int:
    """Push one ``match`` notification per matching user. Returns count sent
    (status sent/would_send; skips don't count).

    M9: matching is DB-level (``find_matches_for_listing`` — one indexed
    query) instead of scanning the whole want_list table with ~4 queries
    per match. Fan-out itself stays synchronous on the request path by
    design (pragmatic scope): each send runs the per-user guard chain
    (dedupe, caps, quiet hours), which is inherently per-user work.
    """
    n = 0
    # getattr fallback keeps third-party WantRepo implementations working.
    find = getattr(want_repo, "find_matches_for_listing", None)
    matches = find(row) if find is not None else find_matches(row, want_repo.list_all())
    for entry in matches:
        uid = entry["user_uid"]
        result = send_notification(
            uid,
            MATCH_CATEGORY,
            "A seedling you want is nearby",
            f"{row.get('variety') or 'A plant'} you want was just listed.",
            data={"listing_id": str(row["id"])},
            ref=str(row["id"]),
            repo=notify_repo,
        )
        if result["status"] in ("sent", "would_send"):
            n += 1
    return n


def _normalize_variety(variety: str) -> str:
    """Canonical form for duplicate detection: trimmed, case-folded."""
    return variety.strip().lower()


def _duplicate_entry(
    repo: WantRepo, uid: str, variety: str, exclude_id: str | None = None
) -> bool:
    """True if the user already wants this variety (case-insensitive)."""
    needle = _normalize_variety(variety)
    return any(
        e["id"] != exclude_id and _normalize_variety(e.get("variety") or "") == needle
        for e in repo.list_for_user(uid)
    )


def _duplicate_response() -> HTTPException:
    return HTTPException(
        409,
        {"code": "want_duplicate", "message": "That variety is already in your want list"},
    )


@router.post("", status_code=201)
def create_want(
    data: WantIn,
    uid: str = Depends(get_current_uid),
    repo: WantRepo = Depends(get_want_repo),
) -> dict[str, Any]:
    _validate_types(data.types)
    variety = data.variety.strip()
    if _duplicate_entry(repo, uid, variety):
        raise _duplicate_response()
    try:
        row = repo.create({
            "id": str(uuid.uuid4()),
            "user_uid": uid,
            "variety": variety,
            "types": data.types,
        })
    except WantDuplicateError:
        # Lost a concurrent-create race on the unique index.
        raise _duplicate_response()
    # A new want changes the writer's feed tiers -> drop their ranked pages.
    cache.invalidate_prefix(f"l:live:ranked:{uid}:")
    return _serialize(row)


@router.get("")
def list_wants(
    uid: str = Depends(get_current_uid),
    repo: WantRepo = Depends(get_want_repo),
) -> dict[str, Any]:
    return {"items": [_serialize(r) for r in repo.list_for_user(uid)]}


@router.patch("/{want_id}")
def patch_want(
    want_id: str,
    data: WantPatch,
    uid: str = Depends(get_current_uid),
    repo: WantRepo = Depends(get_want_repo),
) -> dict[str, Any]:
    row = repo.get(want_id)
    if row is None:
        raise HTTPException(404, {"code": "want_not_found", "message": "No such want-list entry"})
    ensure_owner(row["user_uid"], uid)
    _validate_types(data.types)
    fields = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if "variety" in fields:
        fields["variety"] = fields["variety"].strip()
        if _duplicate_entry(repo, uid, fields["variety"], exclude_id=want_id):
            raise _duplicate_response()
    try:
        updated = repo.update(want_id, fields)
    except WantDuplicateError:
        raise _duplicate_response()
    # Variety/type edits change the writer's feed tiers.
    cache.invalidate_prefix(f"l:live:ranked:{uid}:")
    return _serialize(updated)


@router.delete("/{want_id}", status_code=204)
def delete_want(
    want_id: str,
    uid: str = Depends(get_current_uid),
    repo: WantRepo = Depends(get_want_repo),
) -> None:
    row = repo.get(want_id)
    if row is None:
        raise HTTPException(404, {"code": "want_not_found", "message": "No such want-list entry"})
    ensure_owner(row["user_uid"], uid)
    repo.delete(want_id)
    # Removing a want changes the writer's feed tiers.
    cache.invalidate_prefix(f"l:live:ranked:{uid}:")
    return None

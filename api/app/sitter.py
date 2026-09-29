"""Plant sitting: sitter profiles, requests, and reviews (API-070).

- ``PUT /v1/sitters/me``: upsert the caller's sitter profile (opt-in to offer
  sitting). ``GET /v1/sitters`` lists active sitters (PII-safe).
- ``POST /v1/sitting-requests``: a plant owner requests a sitter.
  Lifecycle: ``requested -> accepted | declined``, ``accepted -> completed |
  cancelled``, ``requested -> cancelled`` (owner).
- ``POST /v1/sitting-requests/{id}/reviews``: exactly one review per sitting,
  authored by the owner, only after the sitting is completed.
- ``GET /v1/sitters/{uid}/reviews``: public review list for a sitter.
"""

from __future__ import annotations

import struct
import uuid
from datetime import date
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field, field_validator

from .auth import get_current_uid
from .db import get_db_conn
from .users import UserRepo, get_user_repo

router = APIRouter(prefix="/v1", tags=["sitting"])

_REQUEST_TRANSITIONS = {
    "requested": {"accepted", "declined", "cancelled"},
    "accepted": {"completed", "cancelled"},
    "declined": set(),
    "completed": set(),
    "cancelled": set(),
}


def can_transition_request(from_status: str, to_status: str) -> bool:
    return to_status in _REQUEST_TRANSITIONS.get(from_status, set())


class SitterRepo(Protocol):
    def upsert_profile(self, uid: str, fields: dict[str, Any]) -> dict[str, Any]: ...
    def get_profile(self, uid: str) -> dict[str, Any] | None: ...
    def list_active(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]: ...
    def create_request(self, row: dict[str, Any]) -> dict[str, Any]: ...
    def get_request(self, request_id: str) -> dict[str, Any] | None: ...
    def set_request_status(self, request_id: str, status: str,
                           expected_status: str) -> dict[str, Any]: ...
    def create_review(self, row: dict[str, Any]) -> dict[str, Any]: ...
    def get_review_by_sitting(self, sitting_id: str) -> dict[str, Any] | None: ...
    def list_reviews_for_sitter(self, sitter_uid: str) -> list[dict[str, Any]]: ...


def _serialize_profile(row: dict[str, Any], display_name: str | None) -> dict[str, Any]:
    return {
        "uid": row["uid"],
        "display_name": display_name,
        "bio": row.get("bio", ""),
        "experience_years": row.get("experience_years", 0),
        "service_radius_miles": row.get("service_radius_miles", 5),
        "active": row.get("active", True),
    }


def _serialize_request(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "owner_uid": row["owner_uid"],
        "sitter_uid": row["sitter_uid"],
        "plant_count": row["plant_count"],
        "start_date": str(row["start_date"]),
        "end_date": str(row["end_date"]),
        "notes": row.get("notes", ""),
        "status": row["status"],
        "created_at": row.get("created_at"),
    }


def _serialize_review(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "sitting_id": str(row["sitting_id"]),
        "reviewer_uid": row["reviewer_uid"],
        "rating": row["rating"],
        "comment": row.get("comment", ""),
        "created_at": row.get("created_at"),
    }


class PostgresSitterRepo:
    def __init__(self, conn):
        self._conn = conn

    def upsert_profile(self, uid, fields):
        row = self._conn.execute(
            """INSERT INTO sitter_profiles (uid, bio, experience_years, service_radius_miles, active)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (uid) DO UPDATE SET
                 bio = EXCLUDED.bio, experience_years = EXCLUDED.experience_years,
                 service_radius_miles = EXCLUDED.service_radius_miles,
                 active = EXCLUDED.active, updated_at = now()
               RETURNING *""",
            (uid, fields.get("bio", ""), fields.get("experience_years", 0),
             fields.get("service_radius_miles", 5), fields.get("active", True)),
        ).fetchone()
        self._conn.commit()
        return dict(row)

    def get_profile(self, uid):
        row = self._conn.execute(
            "SELECT * FROM sitter_profiles WHERE uid = %s", (uid,)).fetchone()
        return dict(row) if row else None

    def list_active(self, limit=100, offset=0):
        rows = self._conn.execute(
            "SELECT * FROM sitter_profiles WHERE active ORDER BY created_at "
            "LIMIT %s OFFSET %s",
            (limit, offset)).fetchall()
        return [dict(r) for r in rows]

    def create_request(self, row):
        rid = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO sitting_requests
               (id, owner_uid, sitter_uid, plant_count, start_date, end_date, notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (rid, row["owner_uid"], row["sitter_uid"], row["plant_count"],
             row["start_date"], row["end_date"], row.get("notes", "")),
        )
        self._conn.commit()
        return self.get_request(rid)

    def get_request(self, request_id):
        row = self._conn.execute(
            "SELECT * FROM sitting_requests WHERE id = %s", (request_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("start_date", "end_date", "created_at"):
            v = d.get(k)
            d[k] = v.isoformat() if hasattr(v, "isoformat") else v
        return d

    def set_request_status(self, request_id, status, expected_status):
        """Atomic status flip (M7): the row only moves when it still holds
        ``expected_status``. A concurrent accept/decline race resolves here —
        the loser gets 409 instead of silently winning by commit order."""
        row = self._conn.execute(
            """UPDATE sitting_requests SET status = %s
               WHERE id = %s AND status = %s RETURNING id""",
            (status, request_id, expected_status)).fetchone()
        if row is None:
            self._conn.rollback()
            raise _conflict("transition_conflict",
                            "This request changed while you were acting; "
                            "please refresh and retry")
        self._conn.commit()
        return self.get_request(request_id)

    def create_review(self, row):
        rid = str(uuid.uuid4())
        try:
            self._conn.execute(
                """INSERT INTO sitting_reviews (id, sitting_id, reviewer_uid, rating, comment)
                   VALUES (%s,%s,%s,%s,%s)""",
                (rid, row["sitting_id"], row["reviewer_uid"],
                 row["rating"], row.get("comment", "")),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            # M15: match the structured diag, never the exception text —
            # constraint names survive PG version/locale changes, text doesn't.
            if (isinstance(exc, pg_errors.UniqueViolation)
                    and getattr(exc.diag, "constraint_name", None)
                    == "sitting_reviews_sitting_id_key"):
                raise _conflict("duplicate_review",
                                "This sitting already has a review") from exc
            raise
        return self.get_review_by_sitting(row["sitting_id"])

    def get_review_by_sitting(self, sitting_id):
        row = self._conn.execute(
            "SELECT * FROM sitting_reviews WHERE sitting_id = %s", (sitting_id,)).fetchone()
        return _review_row(row) if row else None

    def list_reviews_for_sitter(self, sitter_uid):
        # M10b: one JOIN, no per-review re-query (was N+1 via get_review_by_sitting).
        rows = self._conn.execute(
            """SELECT r.* FROM sitting_reviews r
               JOIN sitting_requests s ON s.id = r.sitting_id
               WHERE s.sitter_uid = %s ORDER BY r.created_at""",
            (sitter_uid,)).fetchall()
        return [_review_row(r) for r in rows]


def _review_row(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw sitting_reviews DB row like get_review_by_sitting does."""
    d = dict(row)
    c = d.get("created_at")
    d["created_at"] = c.isoformat() if hasattr(c, "isoformat") else c
    return d


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(409, {"code": code, "message": message})


class MemorySitterRepo:
    def __init__(self):
        self._profiles: dict[str, dict[str, Any]] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._reviews: dict[str, dict[str, Any]] = {}
        self._review_by_sitting: dict[str, str] = {}

    def upsert_profile(self, uid, fields):
        from .listings import utcnow
        row = self._profiles.get(uid, {"uid": uid})
        row.update({
            "bio": fields.get("bio", row.get("bio", "")),
            "experience_years": fields.get("experience_years", row.get("experience_years", 0)),
            "service_radius_miles": fields.get("service_radius_miles",
                                               row.get("service_radius_miles", 5)),
            "active": fields.get("active", row.get("active", True)),
            "created_at": row.get("created_at", utcnow().isoformat()),
            "updated_at": utcnow().isoformat(),
        })
        self._profiles[uid] = row
        return dict(row)

    def get_profile(self, uid):
        row = self._profiles.get(uid)
        return dict(row) if row else None

    def list_active(self, limit=100, offset=0):
        active = [r for r in self._profiles.values() if r.get("active", True)]
        return [dict(r) for r in active[offset:offset + limit]]

    def create_request(self, row):
        from .listings import utcnow
        rid = str(uuid.uuid4())
        rec = {
            "id": rid, "owner_uid": row["owner_uid"], "sitter_uid": row["sitter_uid"],
            "plant_count": row["plant_count"], "start_date": str(row["start_date"]),
            "end_date": str(row["end_date"]), "notes": row.get("notes", ""),
            "status": "requested", "created_at": utcnow().isoformat(),
        }
        self._requests[rid] = rec
        return dict(rec)

    def get_request(self, request_id):
        row = self._requests.get(request_id)
        return dict(row) if row else None

    def set_request_status(self, request_id, status, expected_status):
        rec = self._requests.get(request_id)
        if rec is None or rec["status"] != expected_status:
            raise _conflict("transition_conflict",
                            "This request changed while you were acting; "
                            "please refresh and retry")
        rec["status"] = status
        return dict(rec)

    def create_review(self, row):
        if row["sitting_id"] in self._review_by_sitting:
            raise _conflict("duplicate_review", "This sitting already has a review")
        from .listings import utcnow
        rid = str(uuid.uuid4())
        rec = {
            "id": rid, "sitting_id": row["sitting_id"], "reviewer_uid": row["reviewer_uid"],
            "rating": row["rating"], "comment": row.get("comment", ""),
            "created_at": utcnow().isoformat(),
        }
        self._reviews[rid] = rec
        self._review_by_sitting[row["sitting_id"]] = rid
        return dict(rec)

    def get_review_by_sitting(self, sitting_id):
        rid = self._review_by_sitting.get(sitting_id)
        return dict(self._reviews[rid]) if rid else None

    def list_reviews_for_sitter(self, sitter_uid):
        out = [r for r in self._reviews.values()
               if self._requests.get(r["sitting_id"], {}).get("sitter_uid") == sitter_uid]
        return [dict(r) for r in sorted(out, key=lambda r: r["created_at"])]


def get_sitter_repo(conn=Depends(get_db_conn)) -> SitterRepo:
    return PostgresSitterRepo(conn)


class SitterProfileIn(BaseModel):
    bio: str = Field(default="", max_length=2000)
    experience_years: int = Field(default=0, ge=0, le=60)
    service_radius_miles: float = Field(default=5, gt=0, le=100)
    active: bool = True

    @field_validator("service_radius_miles")
    @classmethod
    def _quantize_to_float32(cls, v: float) -> float:
        """Align the model with the REAL column (L6): Postgres stores only
        float32 precision, so quantize on the way in — the accepted value is
        exactly the value the DB will hold, with no silent drift."""
        return struct.unpack("f", struct.pack("f", v))[0]


class SittingRequestIn(BaseModel):
    sitter_uid: str = Field(min_length=1)
    plant_count: int = Field(gt=0, le=500)
    # M12: real dates, not strings — "2026-13-45" fails validation -> 422
    # instead of reaching the DATE column and 500ing.
    start_date: date  # YYYY-MM-DD
    end_date: date
    notes: str = Field(default="", max_length=2000)


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=2000)


def _display_name(user_repo: UserRepo, uid: str) -> str | None:
    row = user_repo.get(uid)
    return row.get("display_name") if row else None


@router.put("/sitters/me", tags=["sitting"])
def upsert_sitter_profile(
    data: SitterProfileIn,
    uid: str = Depends(get_current_uid),
    repo: SitterRepo = Depends(get_sitter_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Opt in (or update) as a plant sitter. Needs a user profile first."""
    if user_repo.get(uid) is None:
        raise HTTPException(400, {"code": "profile_required",
                                  "message": "Create a profile (POST /v1/users) first"})
    row = repo.upsert_profile(uid, data.model_dump())
    return _serialize_profile(row, _display_name(user_repo, uid))


@router.get("/sitters", tags=["sitting"])
def list_sitters(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repo: SitterRepo = Depends(get_sitter_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Public directory of active sitters. PII-safe. Page size is bounded
    (L6) — the sitter directory can no longer be dumped in one request."""
    return {"sitters": [_serialize_profile(r, _display_name(user_repo, r["uid"]))
                        for r in repo.list_active(limit=limit, offset=offset)]}


@router.get("/sitters/{sitter_uid}", tags=["sitting"])
def get_sitter(
    sitter_uid: str,
    repo: SitterRepo = Depends(get_sitter_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Single sitter profile by UID. Same shape as one item of GET /v1/sitters."""
    row = repo.get_profile(sitter_uid)
    if row is None:
        raise HTTPException(404, {"code": "sitter_not_found",
                                  "message": "No such sitter"})
    return _serialize_profile(row, _display_name(user_repo, sitter_uid))


@router.get("/bookings", tags=["sitting"])
def list_bookings(
    role: str = Query(default=""),
    completed: bool = Query(default=False),
    uid: str = Depends(get_current_uid),
    conn=Depends(get_db_conn),
) -> dict[str, Any]:
    """Sitting requests involving the current user ("bookings").

    role=giver → user is the sitter; role=seeker → user is the plant owner;
    anything else returns both sides. completed=true filters to completed
    requests only.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if role == "giver":
        clauses.append("sitter_uid = %s")
        params.append(uid)
    elif role == "seeker":
        clauses.append("owner_uid = %s")
        params.append(uid)
    else:
        clauses.append("(sitter_uid = %s OR owner_uid = %s)")
        params.extend([uid, uid])
    if completed:
        clauses.append("status = 'completed'")
    rows = conn.execute(
        "SELECT * FROM sitting_requests WHERE " + " AND ".join(clauses)
        + " ORDER BY created_at DESC LIMIT 100",
        tuple(params),
    ).fetchall()
    out = []
    for r in rows:
        out.append(_serialize_request(dict(r)))
    return {"bookings": out}


@router.post("/sitting-requests", status_code=201, tags=["sitting"])
def create_sitting_request(
    data: SittingRequestIn,
    uid: str = Depends(get_current_uid),
    repo: SitterRepo = Depends(get_sitter_repo),
) -> dict[str, Any]:
    """Request a sitter for your plants. Sitter must be active; not yourself."""
    if data.sitter_uid == uid:
        raise HTTPException(422, {"code": "cannot_request_self",
                                  "message": "You cannot request sitting from yourself"})
    if data.end_date < data.start_date:
        raise HTTPException(422, {"code": "invalid_dates",
                                  "message": "end_date must be on or after start_date"})
    profile = repo.get_profile(data.sitter_uid)
    if profile is None or not profile.get("active", True):
        raise HTTPException(422, {"code": "sitter_unavailable",
                                  "message": "That sitter is not offering sitting right now"})
    row = repo.create_request({
        "owner_uid": uid, "sitter_uid": data.sitter_uid,
        "plant_count": data.plant_count, "start_date": data.start_date,
        "end_date": data.end_date, "notes": data.notes.strip(),
    })
    return _serialize_request(row)


def _get_request_or_404(repo: SitterRepo, request_id: str) -> dict[str, Any]:
    row = repo.get_request(request_id)
    if row is None:
        raise HTTPException(404, {"code": "request_not_found",
                                  "message": "No such sitting request"})
    return row


def _transition(repo: SitterRepo, row: dict[str, Any], to: str) -> dict[str, Any]:
    if not can_transition_request(row["status"], to):
        raise HTTPException(422, {"code": "invalid_transition",
                                  "message": f"Cannot move a '{row['status']}' request to '{to}'"})
    # M7: pass the status we read — the repo only flips when it still holds,
    # so a raced accept/decline resolves as 409 for the loser, not commit order.
    return _serialize_request(repo.set_request_status(row["id"], to, row["status"]))


@router.post("/sitting-requests/{request_id}/accept", tags=["sitting"])
def accept_request(request_id: str, uid: str = Depends(get_current_uid),
                   repo: SitterRepo = Depends(get_sitter_repo)) -> dict[str, Any]:
    """Sitter accepts. Only the requested sitter."""
    row = _get_request_or_404(repo, request_id)
    if row["sitter_uid"] != uid:
        raise HTTPException(403, {"code": "not_the_sitter",
                                  "message": "Only the requested sitter can accept"})
    return _transition(repo, row, "accepted")


@router.post("/sitting-requests/{request_id}/decline", tags=["sitting"])
def decline_request(request_id: str, uid: str = Depends(get_current_uid),
                    repo: SitterRepo = Depends(get_sitter_repo)) -> dict[str, Any]:
    """Sitter declines. Only the requested sitter."""
    row = _get_request_or_404(repo, request_id)
    if row["sitter_uid"] != uid:
        raise HTTPException(403, {"code": "not_the_sitter",
                                  "message": "Only the requested sitter can decline"})
    return _transition(repo, row, "declined")


@router.post("/sitting-requests/{request_id}/complete", tags=["sitting"])
def complete_request(request_id: str, uid: str = Depends(get_current_uid),
                     repo: SitterRepo = Depends(get_sitter_repo)) -> dict[str, Any]:
    """Mark the sitting done. Either party, once accepted."""
    row = _get_request_or_404(repo, request_id)
    if uid not in (row["owner_uid"], row["sitter_uid"]):
        raise HTTPException(403, {"code": "not_a_party",
                                  "message": "Only the owner or sitter can complete"})
    return _transition(repo, row, "completed")


@router.post("/sitting-requests/{request_id}/cancel", tags=["sitting"])
def cancel_request(request_id: str, uid: str = Depends(get_current_uid),
                   repo: SitterRepo = Depends(get_sitter_repo)) -> dict[str, Any]:
    """Owner cancels a pending request."""
    row = _get_request_or_404(repo, request_id)
    if row["owner_uid"] != uid:
        raise HTTPException(403, {"code": "not_the_owner",
                                  "message": "Only the plant owner can cancel"})
    return _transition(repo, row, "cancelled")


@router.post("/sitting-requests/{request_id}/reviews", status_code=201, tags=["sitting"])
def create_review(
    request_id: str,
    data: ReviewIn,
    uid: str = Depends(get_current_uid),
    repo: SitterRepo = Depends(get_sitter_repo),
) -> dict[str, Any]:
    """One review per sitting, by the owner, only after completion."""
    row = _get_request_or_404(repo, request_id)
    if row["owner_uid"] != uid:
        raise HTTPException(403, {"code": "not_the_owner",
                                  "message": "Only the plant owner can review"})
    if row["status"] != "completed":
        raise HTTPException(422, {"code": "sitting_not_completed",
                                  "message": "Reviews open after the sitting is completed"})
    if repo.get_review_by_sitting(request_id) is not None:
        raise _conflict("duplicate_review", "This sitting already has a review")
    rec = repo.create_review({
        "sitting_id": request_id, "reviewer_uid": uid,
        "rating": data.rating, "comment": data.comment.strip(),
    })
    return _serialize_review(rec)


@router.get("/sitters/{sitter_uid}/reviews", tags=["sitting"])
def list_sitter_reviews(
    sitter_uid: str,
    repo: SitterRepo = Depends(get_sitter_repo),
) -> dict[str, Any]:
    """Public reviews for a sitter."""
    return {"reviews": [_serialize_review(r)
                        for r in repo.list_reviews_for_sitter(sitter_uid)]}

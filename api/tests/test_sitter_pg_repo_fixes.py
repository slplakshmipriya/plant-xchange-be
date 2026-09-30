"""W4 sitter fixes: Postgres repo race/SQL paths without a live database.

The real-Postgres suite (test_postgres_repos.py) is skipped without
DATABASE_URL; these tests exercise the concurrency-critical SQL in
PostgresSitterRepo / PostgresPaymentRepo against a scripted fake connection:
conditional status flips (M7), ON CONFLICT intent creates (M8), the batched
review listing (M10b), and structured duplicate-review detection (M15).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from psycopg import errors as pg_errors

from app import payments as payments_mod
from app import sitter as sitter_mod

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, script):
        self._script = script

    def fetchone(self):
        item = self._script.pop(0)
        if isinstance(item, list):
            return item[0] if item else None
        return item

    def fetchall(self):
        item = self._script.pop(0)
        if isinstance(item, list):
            return item
        return [item] if item is not None else []


class _FakeConn:
    """Scripted psycopg stand-in: one script entry per execute() call —
    a dict for fetchone(), a list of dicts for fetchall()."""

    def __init__(self, script):
        self._script = list(script)
        self.statements: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return _FakeCursor(self._script)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _request_row(**kw):
    row = {
        "id": "req-1", "owner_uid": "owner", "sitter_uid": "sitter",
        "plant_count": 2,
        "dates": [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)],
        "services": ["watering"], "notes": "",
        "status": "requested", "created_at": NOW,
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# M7 — conditional status flip, 409 on zero rows
# ---------------------------------------------------------------------------

def test_pg_set_request_status_is_conditional():
    conn = _FakeConn([{"id": "req-1"}, _request_row(status="accepted")])
    repo = sitter_mod.PostgresSitterRepo(conn)
    out = repo.set_request_status("req-1", "accepted", "requested")
    assert out["status"] == "accepted"
    # DATE[] -> sorted ISO through the re-read.
    assert out["dates"] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert out["services"] == ["watering"]
    sql, params = conn.statements[0]
    assert "WHERE id = %s AND status = %s" in " ".join(sql.split())
    assert params == ("accepted", "req-1", "requested")


def test_pg_set_request_status_conflict_on_raced_flip():
    # Loser of an accept+decline race: UPDATE matched zero rows -> 409.
    conn = _FakeConn([None])
    repo = sitter_mod.PostgresSitterRepo(conn)
    with pytest.raises(HTTPException) as ei:
        repo.set_request_status("req-1", "declined", "requested")
    assert ei.value.status_code == 409
    assert conn.rollbacks == 1


def test_memory_set_request_status_rejects_stale_expected():
    repo = sitter_mod.MemorySitterRepo()
    rec = repo.create_request({
        "owner_uid": "o", "sitter_uid": "s", "plant_count": 1,
        "dates": ["2026-10-01", "2026-10-02"], "services": []})
    repo.set_request_status(rec["id"], "accepted", "requested")
    # A second actor acting on the stale "requested" read loses.
    with pytest.raises(HTTPException) as ei:
        repo.set_request_status(rec["id"], "declined", "requested")
    assert ei.value.status_code == 409


# ---------------------------------------------------------------------------
# M8 — ON CONFLICT intent create, loser re-reads
# ---------------------------------------------------------------------------

def _intent_row(**kw):
    row = {
        "id": "pi_stub_b-1", "booking_id": "b-1", "amount_cents": 118,
        "fee_cents": 18, "client_secret": "pi_stub_b-1_118",
        "status": "created", "created_at": NOW,
    }
    row.update(kw)
    return row


def test_pg_create_intent_on_conflict_returns_existing():
    # Winner already inserted; our INSERT did nothing -> re-select.
    conn = _FakeConn([None, _intent_row()])
    repo = payments_mod.PostgresPaymentRepo(conn)
    out = repo.create_intent({
        "id": "pi_stub_b-1", "booking_id": "b-1", "amount_cents": 118,
        "fee_cents": 18, "client_secret": "pi_stub_b-1_118"})
    assert out["id"] == "pi_stub_b-1"
    assert out["fee_cents"] == 18
    sql = " ".join(conn.statements[0][0].split())
    assert "ON CONFLICT (booking_id) DO NOTHING" in sql
    assert len(conn.statements) == 2  # INSERT + re-select


def test_pg_create_intent_happy_path():
    row = _intent_row()
    conn = _FakeConn([{"id": "pi_stub_b-1"}, row])
    repo = payments_mod.PostgresPaymentRepo(conn)
    out = repo.create_intent({
        "id": "pi_stub_b-1", "booking_id": "b-1", "amount_cents": 118,
        "fee_cents": 18, "client_secret": "pi_stub_b-1_118"})
    assert out["id"] == "pi_stub_b-1"
    assert out["created_at"] == NOW.isoformat()


# ---------------------------------------------------------------------------
# M10b — one JOIN for the sitter's reviews, no per-review re-query
# ---------------------------------------------------------------------------

def test_pg_list_reviews_for_sitter_is_batched():
    rows = [
        {"id": "rev-1", "sitting_id": "sit-1", "reviewer_uid": "o",
         "rating": 5, "comment": "great", "created_at": NOW},
        {"id": "rev-2", "sitting_id": "sit-2", "reviewer_uid": "o",
         "rating": 4, "comment": "good", "created_at": NOW},
    ]
    conn = _FakeConn([rows])
    repo = sitter_mod.PostgresSitterRepo(conn)
    out = repo.list_reviews_for_sitter("sitter-uid")
    assert len(out) == 2
    assert len(conn.statements) == 1  # the JOIN only; was N+1 before
    assert "JOIN sitting_requests" in " ".join(conn.statements[0][0].split())
    assert out[0]["created_at"] == NOW.isoformat()
    assert out[1]["rating"] == 4


# ---------------------------------------------------------------------------
# M15 — UniqueViolation matched on diag.constraint_name, not text
# ---------------------------------------------------------------------------

class _FakeUniqueViolation(pg_errors.UniqueViolation):
    """UniqueViolation with a scripted constraint name (real PG diag is only
    populated by the C-level driver, unreachable in unit tests)."""

    def __init__(self, constraint_name):
        super().__init__("duplicate key value violates unique constraint")
        self._constraint_name = constraint_name

    @property
    def diag(self):
        return SimpleNamespace(constraint_name=self._constraint_name)


class _ExplodingConn(_FakeConn):
    def __init__(self, exc):
        super().__init__([])
        self._exc = exc

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        raise self._exc


def _review_payload():
    return {"sitting_id": "sit-1", "reviewer_uid": "o",
            "rating": 5, "comment": "thrived"}


def test_pg_create_review_duplicate_detected_via_diag():
    conn = _ExplodingConn(
        _FakeUniqueViolation("sitting_reviews_sitting_id_key"))
    repo = sitter_mod.PostgresSitterRepo(conn)
    with pytest.raises(HTTPException) as ei:
        repo.create_review(_review_payload())
    assert ei.value.status_code == 409
    assert conn.rollbacks == 1


def test_pg_create_review_other_unique_violation_reraises():
    conn = _ExplodingConn(_FakeUniqueViolation("some_other_constraint"))
    repo = sitter_mod.PostgresSitterRepo(conn)
    with pytest.raises(pg_errors.UniqueViolation):
        repo.create_review(_review_payload())
    assert conn.rollbacks == 1


def test_pg_create_review_non_unique_error_reraises():
    conn = _ExplodingConn(RuntimeError("connection lost"))
    repo = sitter_mod.PostgresSitterRepo(conn)
    with pytest.raises(RuntimeError):
        repo.create_review(_review_payload())
    assert conn.rollbacks == 1

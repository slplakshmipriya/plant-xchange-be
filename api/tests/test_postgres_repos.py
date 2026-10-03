"""Postgres-backed round-trip tests for every Postgres*Repo (C4 remediation).

Before C4, every Postgres moderation read path raised ``NameError`` and the
suite had zero coverage of any ``Postgres*`` class (all tests used memory
fakes). This module exercises the real repos against real Postgres, with the
real schema applied via ``db.run_migrations``.

Skip pattern: the whole module skips cleanly when ``DATABASE_URL`` is unset,
exactly like the existing real-Postgres test in test_foundation.py.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs DATABASE_URL (real Postgres) for integration",
)

import psycopg
from psycopg.rows import dict_row

from app import db
from app.claims import PostgresClaimRepo
from app.credits import PostgresCreditRepo
from app.crypto import MESSAGE_KEY_ENV, decrypt_text
from app.listings import PostgresListingRepo
from app.moderation import PostgresModerationRepo, PostgresModerationViewRepo
from app.msg import PostgresMessageRepo
from app.notify import PostgresNotificationRepo
from app.payments import PostgresPaymentRepo
from app.sitter import PostgresSitterRepo
from app.slots import PostgresSlotRepo
from app.users import PostgresUserRepo
from app.wantlist import PostgresWantRepo

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="session")
def migrated():
    """Apply migrations 0001-0022 to the test database once per session."""
    applied = db.run_migrations()
    assert db.database_url(), "DATABASE_URL vanished during test session"
    return applied


@pytest.fixture()
def pg_conn(migrated):
    """Freshly truncated Postgres connection; dict rows like the repos use."""
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
        )
        tables = [r["tablename"] for r in cur.fetchall()]
        if tables:
            cur.execute("TRUNCATE " + ", ".join(tables) + " CASCADE")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def users(pg_conn):
    repo = PostgresUserRepo(pg_conn)
    repo.upsert("pg-alice", display_name="PG Alice")
    repo.upsert("pg-bob", display_name="PG Bob")
    return repo


@pytest.fixture()
def listing(pg_conn, users):
    repo = PostgresListingRepo(pg_conn)
    return repo.create({
        "id": str(uuid.uuid4()),
        "owner_uid": "pg-alice",
        "type": "harvest",
        "photos": [],
        "credit_cost": 2,
        "spray_disclosure": "none",
    })


# --------------------------------------------------------------------------
# moderation (the previously-broken C4 paths)
# --------------------------------------------------------------------------


def test_moderation_add_report_roundtrip(pg_conn, users):
    repo = PostgresModerationRepo(pg_conn)
    row = repo.add_report("pg-alice", "LISTING", "some-listing", "spam", "spammy")
    assert row["reporter_uid"] == "pg-alice"
    assert row["category"] == "spam"
    assert isinstance(row["created_at"], str)  # C4: _row() must not NameError


def test_moderation_strikes_roundtrip(pg_conn, users):
    repo = PostgresModerationRepo(pg_conn)
    repo.add_strike("pg-bob", "safety", "bad behavior", verified=True)
    repo.add_strike("pg-bob", "safety", "unverified", verified=False)
    assert repo.count_verified_strikes("pg-bob", "safety") == 1
    assert repo.count_verified_strikes("pg-bob", "fraud") == 0


def test_moderation_active_ban_roundtrip(pg_conn, users):
    repo = PostgresModerationRepo(pg_conn)
    assert repo.active_ban("pg-bob") is None
    row = repo.add_enforcement("pg-bob", "*", "ban", None, "repeated fraud")
    assert isinstance(row["created_at"], str)  # C4: _row() must not NameError
    found = repo.active_ban("pg-bob")
    assert found is not None and found["kind"] == "ban"


def test_moderation_active_enforcement_roundtrip(pg_conn, users):
    repo = PostgresModerationRepo(pg_conn)
    future = NOW + timedelta(days=3)
    repo.add_enforcement("pg-bob", "safety", "suspension", future, "cooldown")
    active = repo.active_enforcement("pg-bob", "safety", NOW)
    assert active is not None and active["kind"] == "suspension"
    # C4: expires_at must survive _row() as an ISO string, not a NameError.
    assert active["expires_at"] == future.isoformat()
    assert repo.active_enforcement("pg-bob", "fraud", NOW) is None
    # Expired suspension no longer counts as active; the live one still wins.
    past = NOW - timedelta(days=1)
    repo.add_enforcement("pg-bob", "safety", "suspension", past, "old")
    still = repo.active_enforcement("pg-bob", "safety", NOW)
    assert still is not None and still["expires_at"] == future.isoformat()


def test_moderation_dispute_roundtrip(pg_conn, users):
    repo = PostgresModerationRepo(pg_conn)
    row = repo.add_dispute("exchange-1", "pg-alice", "no_show", "never showed up")
    assert row["status"] == "open"
    assert isinstance(row["created_at"], str)  # C4: _row() must not NameError
    assert repo.get_dispute("missing") is None
    fetched = repo.get_dispute(row["id"])
    assert fetched is not None and fetched["reporter_uid"] == "pg-alice"
    resolved_at = NOW
    resolved = repo.resolve_dispute(row["id"], "upheld", 2, "pg-mod", resolved_at)
    assert resolved["status"] == "resolved"
    assert resolved["outcome"] == "upheld"
    assert resolved["reversal_credits"] == 2
    assert resolved["resolved_at"] == resolved_at.isoformat()  # C4 key: resolved_at
    assert repo.resolve_dispute("missing", "upheld", 0, "pg-mod", resolved_at) is None


def test_moderation_views_roundtrip(pg_conn, users, listing):
    view_repo = PostgresModerationViewRepo(pg_conn)
    msg_repo = PostgresMessageRepo(pg_conn)
    thread = msg_repo.get_or_create_thread(listing["id"], "pg-bob")
    msg = msg_repo.add_message(thread["id"], "pg-bob", "hello")
    logged = view_repo.log_view("pg-alice", thread["id"], msg["id"], "report review")
    assert isinstance(logged["viewed_at"], str)
    views = view_repo.list_views_for_thread(thread["id"])
    assert len(views) == 1 and views[0]["viewer_uid"] == "pg-alice"


# --------------------------------------------------------------------------
# the other Postgres*Repo classes: one round-trip each
# --------------------------------------------------------------------------


def test_users_roundtrip(pg_conn, users):
    row = users.get("pg-alice")
    assert row is not None and row["display_name"] == "PG Alice"
    assert users.get_by_phone_hash("nope") is None
    users.upsert("pg-alice", phone_hash="hash-1")
    assert users.get_by_phone_hash("hash-1")["uid"] == "pg-alice"
    users.set_idv_status("pg-alice", "verified")
    assert users.get("pg-alice")["idv_status"] == "verified"
    assert users.get("pg-ghost") is None


def test_listings_roundtrip(pg_conn, users, listing):
    repo = PostgresListingRepo(pg_conn)
    assert listing["owner_uid"] == "pg-alice"
    assert listing["pickup_window"] is None
    updated = repo.update(listing["id"], {"status": "live"})
    assert updated["status"] == "live"
    assert repo.set_status(listing["id"], "draft")["status"] == "draft"


def test_claims_roundtrip(pg_conn, users, listing):
    repo = PostgresClaimRepo(pg_conn)
    claim = repo.create({
        "id": str(uuid.uuid4()),
        "listing_id": listing["id"],
        "claimer_uid": "pg-bob",
        "quantity": 1.5,
    })
    assert claim["status"] == "pending"
    assert claim["quantity"] == 1.5
    assert repo.get(claim["id"])["claimer_uid"] == "pg-bob"
    assert repo.set_status(claim["id"], "accepted")["status"] == "accepted"
    assert repo.active_claim_for(listing["id"], "pg-bob") is not None
    assert repo.get("missing") is None


def test_credits_roundtrip(pg_conn, users, listing):
    repo = PostgresCreditRepo(pg_conn)
    entry = repo.add_entry("pg-alice", 5, "starter")
    assert entry["delta"] == 5
    assert repo.balance("pg-alice") == 5
    # Idempotent replay returns the existing entry, no double-post.
    same = repo.add_entry("pg-alice", 5, "starter", idempotency_key="k1")
    dup = repo.add_entry("pg-alice", 5, "starter", idempotency_key="k1")
    assert dup["id"] == same["id"]
    assert repo.find_by_idempotency_key("k1")["id"] == same["id"]
    assert repo.find_by_idempotency_key("missing") is None
    assert repo.add_confirmation(listing["id"], "pg-alice") is True
    assert repo.add_confirmation(listing["id"], "pg-alice") is False  # repeat: no-op
    assert repo.confirmations(listing["id"]) == ["pg-alice"]


def test_slots_roundtrip(pg_conn, users, listing):
    repo = PostgresSlotRepo(pg_conn)
    slot = repo.create({
        "id": str(uuid.uuid4()),
        "tree_id": listing["id"],
        "owner_uid": "pg-alice",
        "day_ms": 1_800_000_000_000,
        "start_ms": 1_800_003_600_000,
        "end_ms": 1_800_007_200_000,
        "max_pickers": 2,
        "credit_cost": 1,
    })
    assert slot["claimed_count"] == 0
    claimed = repo.claim_slot(slot["id"])
    assert claimed["claimed_count"] == 1
    assert repo.get(slot["id"])["claimed_count"] == 1
    assert len(repo.list_by_tree(listing["id"])) == 1
    # Full slot: no more spots, returns None.
    repo.claim_slot(slot["id"])
    assert repo.claim_slot(slot["id"]) is None


def test_msg_roundtrip(pg_conn, users, listing):
    repo = PostgresMessageRepo(pg_conn)
    thread = repo.get_or_create_thread(listing["id"], "pg-bob")
    again = repo.get_or_create_thread(listing["id"], "pg-bob")
    assert again["id"] == thread["id"]
    assert repo.get_thread(thread["id"])["listing_id"] == listing["id"]
    msg = repo.add_message(thread["id"], "pg-bob", "hello there")
    assert msg["kind"] == "text"
    # Body is stored encrypted at rest; it decrypts with the message key.
    assert decrypt_text(msg["body"], MESSAGE_KEY_ENV) == "hello there"
    msgs = repo.list_messages(thread["id"], offset=0, limit=10)
    assert len(msgs) == 1
    assert repo.count_messages(thread["id"]) == 1
    assert repo.list_threads_for("pg-bob", [listing["id"]])


def test_notify_roundtrip(pg_conn, users):
    repo = PostgresNotificationRepo(pg_conn)
    assert repo.get_prefs("pg-alice") is None
    prefs = repo.set_prefs("pg-alice", {"match": True, "chat": False}, True,
                           "22:00", "07:00")
    assert prefs["categories"] == {"match": True, "chat": False}
    assert repo.get_prefs("pg-alice")["quiet_hours_start"] == "22:00"
    repo.register_token("pg-alice", "tok-1")
    repo.register_token("pg-alice", "tok-1")  # upsert: no dup
    assert repo.get_tokens("pg-alice") == ["tok-1"]
    since = NOW - timedelta(minutes=5)
    assert repo.has_recent("pg-alice", "match", "ref-1", since) is False
    repo.log("pg-alice", "match", "ref-1", "sent")
    assert repo.has_recent("pg-alice", "match", "ref-1", since) is True
    assert repo.count_since("pg-alice", since) == 1


def test_sitter_roundtrip(pg_conn, users):
    repo = PostgresSitterRepo(pg_conn)
    profile = repo.upsert_profile("pg-bob", {"bio": "plant lover", "active": True})
    assert profile["bio"] == "plant lover"
    assert repo.get_profile("pg-bob")["uid"] == "pg-bob"
    assert any(p["uid"] == "pg-bob" for p in repo.list_active())
    req = repo.create_request({
        "owner_uid": "pg-alice",
        "sitter_uid": "pg-bob",
        "plant_count": 3,
        "dates": [date(2026, 10, 5), date(2026, 10, 6),
                  date(2026, 10, 7), date(2026, 10, 8)],
        "services": ["watering"],
    })
    assert req["status"] == "requested"
    assert req["dates"] == ["2026-10-05", "2026-10-06",
                            "2026-10-07", "2026-10-08"]
    assert req["services"] == ["watering"]
    assert repo.get_request(req["id"])["plant_count"] == 3
    assert repo.set_request_status(req["id"], "accepted", "requested")["status"] == "accepted"


def test_payments_roundtrip(pg_conn, users):
    sitter_repo = PostgresSitterRepo(pg_conn)
    pay_repo = PostgresPaymentRepo(pg_conn)
    req = sitter_repo.create_request({
        "owner_uid": "pg-alice",
        "sitter_uid": "pg-bob",
        "plant_count": 2,
        "dates": [date(2026, 10, 5), date(2026, 10, 6)],
        "services": [],
    })
    intent = pay_repo.create_intent({
        "id": "pi_test_1",
        "booking_id": req["id"],
        "amount_cents": 1200,
        "fee_cents": 216,
        "client_secret": "secret_abc",
    })
    assert intent["status"] == "created"
    assert pay_repo.get_intent("pi_test_1")["fee_cents"] == 216
    assert pay_repo.get_by_booking_id(req["id"])["id"] == "pi_test_1"
    assert pay_repo.get_intent("missing") is None


def test_wantlist_roundtrip(pg_conn, users):
    repo = PostgresWantRepo(pg_conn)
    want = repo.create({"id": str(uuid.uuid4()), "user_uid": "pg-alice",
                        "variety": "Roma tomato", "types": ["produce"]})
    assert want["variety"] == "Roma tomato"
    assert repo.list_for_user("pg-alice")[0]["id"] == want["id"]
    assert repo.get(want["id"])["types"] == ["produce"]
    assert repo.update(want["id"], {"variety": "Cherry tomato"})["variety"] == "Cherry tomato"
    assert len(repo.list_all()) == 1
    assert repo.delete(want["id"]) is True
    assert repo.get(want["id"]) is None
    assert repo.delete(want["id"]) is False

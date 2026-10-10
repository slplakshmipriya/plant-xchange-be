"""Postgres-backed route regression for the PYO slot routes.

Pre-fix, ``PostgresSlotRepo`` handed ``uuid.UUID`` objects back for
``slots.tree_id``/``slots.id`` (psycopg's default for UUID columns)
while route handlers compare row fields against string path params
(``slot["tree_id"] != tree_id``) — always unequal, so slot claim and
confirm-visit 404'd ``slot_not_found`` on real Postgres even with the
row present. The memory repo stores strings, so the memory suite never
saw it. ``_row()`` now stringifies UUID columns at the repo boundary;
this e2e proves claim -> confirm-visit -> annotated list over HTTP,
plus the pre-existing claim path, against a migrated Postgres.

Skip pattern matches test_postgres_repos.py: the whole module skips
when ``DATABASE_URL`` is unset.
"""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs DATABASE_URL (real Postgres) for integration",
)

import psycopg
from psycopg.rows import dict_row
from fastapi.testclient import TestClient

from app import db
from app.credits import PostgresCreditRepo
from app.listings import PostgresListingRepo
from app.moderation import PostgresModerationRepo
from app.slots import PostgresSlotRepo
from app.users import PostgresUserRepo
from app.vertical import reset_vertical_cache

SLOT_KEYS = {"id", "treeId", "dayMs", "startMs", "endMs", "maxPickers",
             "claimedCount", "creditCost", "cashCents"}


@pytest.fixture(scope="session")
def migrated():
    applied = db.run_migrations()
    assert db.database_url(), "DATABASE_URL vanished during test session"
    return applied


@pytest.fixture()
def pg_conn(migrated):
    """Postgres connection with every public table truncated (repo pattern)."""
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


@pytest.fixture(autouse=True)
def _vertical_env(monkeypatch):
    monkeypatch.delenv("VERTICAL_CONFIG_PATH", raising=False)
    monkeypatch.delenv("VERTICAL_ID", raising=False)
    reset_vertical_cache()
    yield
    reset_vertical_cache()


def login_as(monkeypatch, uid: str):
    """Re-point the mock verifier at uid (tests default to pg-alice)."""
    import app.auth as auth_mod

    def fake(token: str):
        if token == "good-token":
            return {"uid": uid}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)


@pytest.fixture()
def pg_app(pg_conn, monkeypatch):
    """TestClient whose slot-route repos are the Postgres ones on pg_conn.

    Returns (client, slot_repo, credit_repo, listing_repo, tree_id).
    """
    from app import credits as credits_mod
    from app import listings as listings_mod
    from app import moderation as moderation_mod
    from app import slots as slots_mod
    from app import users as users_mod
    from app.main import create_app

    urepo = PostgresUserRepo(pg_conn)
    lrepo = PostgresListingRepo(pg_conn)
    srepo = PostgresSlotRepo(pg_conn)
    crepo = PostgresCreditRepo(pg_conn)
    mrepo = PostgresModerationRepo(pg_conn)

    urepo.upsert("pg-alice", display_name="PG Alice")
    urepo.upsert("pg-bob", display_name="PG Bob")
    tree = lrepo.create({
        "id": str(uuid.uuid4()),
        "owner_uid": "pg-alice",
        "type": "tree",
        "photos": [],
        "credit_cost": 1,
        "spray_disclosure": "unsprayed",
        "status": "live",
    })

    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "1000000")

    def fake_verify(token: str):
        if token == "good-token":
            return {"uid": "pg-alice"}
        raise ValueError("bad token")

    monkeypatch.setattr("app.auth.verify_id_token", fake_verify)
    client = TestClient(create_app())
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[slots_mod.get_slot_repo] = lambda: srepo
    client.app.dependency_overrides[credits_mod.get_credit_repo] = lambda: crepo
    client.app.dependency_overrides[moderation_mod.get_moderation_repo] = lambda: mrepo
    # PostgresListingRepo hands its id back as uuid.UUID; normalize once
    # here so tests compare against the string form used in URLs.
    return client, srepo, crepo, lrepo, str(tree["id"])


AUTH = {"Authorization": "Bearer good-token"}


def test_pg_slot_claim_confirm_and_listed_annotations(pg_app, monkeypatch):
    client, srepo, crepo, lrepo, tid = pg_app

    # Owner opens the slot over HTTP.
    r = client.post(f"/v1/trees/{tid}/slots", json={
        "dayMs": 1760000000000, "startMs": 1760010000000,
        "endMs": 1760020000000, "maxPickers": 2, "creditCost": 2,
    }, headers=AUTH)
    assert r.status_code == 201, r.text
    slot_id = r.json()["slot"]["id"]

    # Repo parity: PG rows now carry string id/tree_id like memory rows.
    row = srepo.get(slot_id)
    assert isinstance(row["id"], str) and row["id"] == slot_id
    assert isinstance(row["tree_id"], str) and row["tree_id"] == tid

    # Claim over HTTP — the pre-existing PG path that 404'd on the
    # UUID-vs-str tree_id comparison.
    crepo.add_entry("pg-bob", 5, "seed", ref_id="t")
    login_as(monkeypatch, "pg-bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["claimedCount"] == 1
    assert crepo.balance("pg-bob") == 3  # 5 seeded - 2 slot cost
    assert crepo.balance("pg-alice") == 2

    claim = srepo.get_claim(slot_id, "pg-bob")
    assert isinstance(claim["slot_id"], str) and claim["slot_id"] == slot_id

    # Confirm the visit; credits must not move (settled at claim time).
    bob_entries_before = len(crepo.entries("pg-bob"))
    alice_entries_before = len(crepo.entries("pg-alice"))
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 12.5}, headers=AUTH)
    assert r.status_code == 200, r.text
    slot = r.json()["slot"]
    assert slot["claimed_by_me"] is True
    assert slot["visit_confirmed"] is True
    assert slot["lbs_picked"] == 12.5
    assert crepo.balance("pg-bob") == 3
    assert crepo.balance("pg-alice") == 2
    assert len(crepo.entries("pg-bob")) == bob_entries_before
    assert len(crepo.entries("pg-alice")) == alice_entries_before

    # List annotations: claimer sees the visit; owner sees the bare shape.
    r = client.get(f"/v1/trees/{tid}/slots", headers=AUTH)
    assert r.status_code == 200, r.text
    mine = next(s for s in r.json()["slots"] if s["id"] == slot_id)
    assert mine["claimed_by_me"] is True
    assert mine["visit_confirmed"] is True
    assert mine["lbs_picked"] == 12.5

    login_as(monkeypatch, "pg-alice")
    r = client.get(f"/v1/trees/{tid}/slots", headers=AUTH)
    assert r.status_code == 200, r.text
    owners = next(s for s in r.json()["slots"] if s["id"] == slot_id)
    assert set(owners.keys()) == SLOT_KEYS

    # A slot addressed under a different (real) tree still 404s — the
    # tree_id comparison now matches genuinely, not by always-unequal
    # accident.
    other_tree = lrepo.create({
        "id": str(uuid.uuid4()),
        "owner_uid": "pg-alice",
        "type": "tree",
        "photos": [],
        "credit_cost": 1,
        "spray_disclosure": "unsprayed",
        "status": "live",
    })
    r = client.post(
        f"/v1/trees/{other_tree['id']}/slots/{slot_id}/confirm-visit",
        json={"lbs_picked": 1}, headers=AUTH)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "slot_not_found"


def test_pg_confirm_visit_unknown_claim_403(pg_app, monkeypatch):
    client, srepo, crepo, lrepo, tid = pg_app
    login_as(monkeypatch, "pg-alice")
    r = client.post(f"/v1/trees/{tid}/slots", json={
        "dayMs": 1760000000000, "startMs": 1760010000000,
        "endMs": 1760020000000, "maxPickers": 2, "creditCost": 0,
    }, headers=AUTH)
    assert r.status_code == 201, r.text
    slot_id = r.json()["slot"]["id"]
    # pg-bob never claimed: reaches the claim check (no 404), 403s.
    login_as(monkeypatch, "pg-bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 3}, headers=AUTH)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "not_claimed"

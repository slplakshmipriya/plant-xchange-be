"""Infra fixes: migration advisory lock (H9), UUID path-param 404 (M4),
connection pool (M13), startup migration retry (L5).

No real Postgres needed: the psycopg boundary is faked; the pool
constructor is monkeypatched. Memory fakes are untouched.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from fastapi import HTTPException
from psycopg import OperationalError
from psycopg.errors import InvalidTextRepresentation

from app import db as db_mod


# ---------------------------------------------------------------------------
# H9: advisory lock around run_migrations
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, script):
        self.script = script

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.script["statements"].append((sql, params))
        if "FAIL-ME" in sql:
            raise OperationalError("boom")

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, script):
        self.script = script

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self.script)

    def commit(self):
        self.script["statements"].append(("COMMIT", None))


def _patch_connect(monkeypatch, script):
    monkeypatch.setattr(db_mod, "database_url", lambda: "postgresql://fake")
    monkeypatch.setattr(
        "psycopg.connect", lambda url, **kw: _FakeConn(script)
    )


def _migration_dir(tmp_path, fail_second=False) -> Path:
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t1 (id INT);")
    second = "CREATE TABLE t2 (id INT); -- FAIL-ME" if fail_second else "CREATE TABLE t2 (id INT);"
    (tmp_path / "0002_b.sql").write_text(second)
    return tmp_path


def test_migrations_hold_advisory_lock(monkeypatch, tmp_path):
    """Lock acquired first, released last, around the whole migration run."""
    script = {"statements": []}
    _patch_connect(monkeypatch, script)
    db_mod._pool = None

    applied = db_mod.run_migrations(_migration_dir(tmp_path))
    assert applied == [1, 2]

    stmts = [s for s, _ in script["statements"]]
    params = [p for _, p in script["statements"]]
    assert stmts[0] == "SELECT pg_advisory_lock(%s)"
    assert params[0] == (db_mod.MIGRATION_ADVISORY_LOCK_KEY,)
    assert stmts[-1] == "SELECT pg_advisory_unlock(%s)"
    assert params[-1] == (db_mod.MIGRATION_ADVISORY_LOCK_KEY,)
    # Migration bodies and bookkeeping ran between lock and unlock.
    assert any("CREATE TABLE t1" in s for s in stmts)
    assert sum(s == "COMMIT" for s in stmts) == 2


def test_lock_released_when_migration_fails(monkeypatch, tmp_path):
    """A failing migration still releases the advisory lock (finally)."""
    script = {"statements": []}
    _patch_connect(monkeypatch, script)

    with pytest.raises(OperationalError):
        db_mod.run_migrations(_migration_dir(tmp_path, fail_second=True))

    stmts = [s for s, _ in script["statements"]]
    assert stmts[0] == "SELECT pg_advisory_lock(%s)"
    assert stmts[-1] == "SELECT pg_advisory_unlock(%s)"


def test_lock_key_is_a_valid_bigint():
    """The lock key must fit in a signed 64-bit integer (pg_advisory_lock)."""
    key = db_mod.MIGRATION_ADVISORY_LOCK_KEY
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63


def test_constraint_migrations_are_idempotent_guarded():
    """0017/0020 ADD CONSTRAINT lives inside a pg_constraint-guarded DO block."""
    cases = [
        ("0017_chat_attachments.sql", "messages_kind_check"),
        ("0020_message_encryption.sql", "messages_body_check"),
    ]
    for filename, constraint in cases:
        text = (db_mod.MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
        add_idx = text.index(f"ADD CONSTRAINT {constraint}")
        do_idx = text.index("DO $$")
        assert do_idx < add_idx, f"{filename}: ADD must be inside the DO block"
        assert "pg_constraint" in text, f"{filename}: must guard on pg_constraint"


# ---------------------------------------------------------------------------
# M4: non-UUID path params -> 404 envelope, not 500
# ---------------------------------------------------------------------------


def test_non_uuid_path_param_returns_404_envelope(client, mock_verify, auth_headers):
    """GET /v1/listings/not-a-uuid where the repo raises
    InvalidTextRepresentation (what the Postgres repo raises on UUID cast)
    must come back as a 404 in the uniform error envelope, not a 500."""
    from app import listings as listings_mod
    from app import users as users_mod

    class ExplodingRepo(listings_mod.MemoryListingRepo):
        def get(self, listing_id):
            raise InvalidTextRepresentation(
                'invalid input syntax for type uuid: "not-a-uuid"'
            )

    client.app.dependency_overrides[listings_mod.get_listing_repo] = (
        lambda: ExplodingRepo()
    )
    # get_listing also resolves the user repo (owner display name/avatar).
    client.app.dependency_overrides[users_mod.get_user_repo] = (
        lambda: users_mod.MemoryUserRepo()
    )
    r = client.get("/v1/listings/not-a-uuid", headers=auth_headers)
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "not_found"
    assert "request_id" in body


# ---------------------------------------------------------------------------
# M13: connection pool
# ---------------------------------------------------------------------------


class _FakePooledConn:
    """Stand-in for a pooled psycopg connection: records ping/close calls."""

    def __init__(self, pool, fail_ping_times=0, fail_checkout=False):
        self._pool = pool
        self.fail_ping_times = fail_ping_times
        self.fail_checkout = fail_checkout
        self.pings = 0
        self.closed = False

    def execute(self, query, *args):
        self.pings += 1
        if self.fail_ping_times > 0:
            self.fail_ping_times -= 1
            raise OperationalError("connection already closed")
        return []

    def close(self):
        self.closed = True


class _FakePool:
    def __init__(self, fail_ping_times=0, fail_checkout_times=0):
        self.checkouts = 0
        self.exits = 0
        self.closed = False
        self.conns = []
        # Scripted failures: first N checkouts hand out dead connections
        # (ping raises), next M checkouts raise at checkout time.
        self.fail_ping_times = fail_ping_times
        self.fail_checkout_times = fail_checkout_times

    def connection(self):
        self.checkouts += 1
        pool = self

        @contextlib.contextmanager
        def cm():
            if pool.fail_checkout_times > 0:
                pool.fail_checkout_times -= 1
                raise OperationalError("could not connect to server")
            conn = _FakePooledConn(pool)
            if pool.fail_ping_times > 0:
                pool.fail_ping_times -= 1
                conn.fail_ping_times = 1
            pool.conns.append(conn)
            try:
                yield conn
            finally:
                pool.exits += 1

        return cm()

    def close(self):
        self.closed = True


@pytest.fixture()
def fake_pool_setup(monkeypatch):
    """Swap the real pool constructor for a fake; returns (pool, created_urls)."""
    monkeypatch.setattr(db_mod, "database_url", lambda: "postgresql://fake")
    pool = _FakePool()
    created = []
    monkeypatch.setattr(
        db_mod, "_create_pool", lambda url: created.append(url) or pool
    )
    db_mod._pool = None
    yield pool, created
    db_mod._pool = None


def test_pool_created_once_and_reused(fake_pool_setup):
    pool, created = fake_pool_setup
    assert db_mod.get_pool() is pool
    assert db_mod.get_pool() is pool
    assert created == ["postgresql://fake"]


def test_get_db_conn_checks_out_and_returns_connection(fake_pool_setup):
    pool, _ = fake_pool_setup
    gen = db_mod.get_db_conn()
    conn = next(gen)
    assert isinstance(conn, _FakePooledConn)
    assert conn.pings == 1  # checkout validated with SELECT 1
    assert pool.checkouts == 1
    with pytest.raises(StopIteration):
        next(gen)
    assert pool.exits == 1  # returned to the pool on request teardown


def test_get_db_conn_discards_stale_connection_and_rechecks_out(monkeypatch):
    """First checkout hands out a dead connection (Neon suspended while
    idle): the ping fails, the conn is closed/discarded, and the route
    transparently gets the second, live checkout."""
    monkeypatch.setattr(db_mod, "database_url", lambda: "postgresql://fake")
    pool = _FakePool(fail_ping_times=1)
    monkeypatch.setattr(db_mod, "_create_pool", lambda url: pool)
    db_mod._pool = None
    try:
        gen = db_mod.get_db_conn()
        conn = next(gen)
        assert pool.checkouts == 2
        assert pool.conns[0].closed  # stale conn discarded, not returned live
        assert pool.conns[1].pings == 1
        assert conn is pool.conns[1]
        with pytest.raises(StopIteration):
            next(gen)
    finally:
        db_mod._pool = None


def test_get_db_conn_retries_failed_checkouts_then_503(monkeypatch):
    """Fresh checkouts themselves fail while Neon is still resuming:
    retried, then an honest 503 database_waking instead of 500."""
    monkeypatch.setattr(db_mod, "database_url", lambda: "postgresql://fake")
    pool = _FakePool(fail_checkout_times=3)
    monkeypatch.setattr(db_mod, "_create_pool", lambda url: pool)
    db_mod._pool = None
    try:
        with pytest.raises(HTTPException) as ei:
            next(db_mod.get_db_conn())
        assert ei.value.status_code == 503
        assert ei.value.detail["code"] == "database_waking"
        assert pool.checkouts == 3
    finally:
        db_mod._pool = None


def test_get_db_conn_does_not_retry_route_errors(monkeypatch):
    """An OperationalError raised by the route itself (after yield) must
    propagate — retrying would mistake a half-run route for a bad checkout."""
    monkeypatch.setattr(db_mod, "database_url", lambda: "postgresql://fake")
    pool = _FakePool()
    monkeypatch.setattr(db_mod, "_create_pool", lambda url: pool)
    db_mod._pool = None
    try:
        gen = db_mod.get_db_conn()
        next(gen)
        with pytest.raises(OperationalError):
            gen.throw(OperationalError("server went away mid-request"))
        assert pool.checkouts == 1  # no retry of the half-run route
    finally:
        db_mod._pool = None


def test_pool_503_when_database_url_unset(monkeypatch):
    monkeypatch.setattr(db_mod, "database_url", lambda: None)
    db_mod._pool = None
    try:
        with pytest.raises(HTTPException) as ei:
            db_mod.get_pool()
        assert ei.value.status_code == 503
        with pytest.raises(HTTPException) as ei2:
            next(db_mod.get_db_conn())
        assert ei2.value.status_code == 503
    finally:
        db_mod._pool = None


def test_close_pool_closes_and_resets(fake_pool_setup):
    pool, _ = fake_pool_setup
    db_mod.get_pool()
    db_mod.close_pool()
    assert pool.closed
    assert db_mod._pool is None
    db_mod.close_pool()  # no-op when never opened


# ---------------------------------------------------------------------------
# L5: startup migration retry with backoff
# ---------------------------------------------------------------------------


def _run_retry(monkeypatch, fake, **kwargs):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "run_migrations", fake)
    return asyncio.run(main_mod._run_migrations_with_retry(**kwargs))


def test_migration_retry_succeeds_after_transient_failures(monkeypatch):
    attempts = {"n": 0}

    def fake():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError("could not connect to server")
        return [1, 2]

    _run_retry(monkeypatch, fake, base_delay_s=0)
    assert attempts["n"] == 3


def test_migration_retry_gives_up_after_max_attempts(monkeypatch):
    attempts = {"n": 0}

    def fake():
        attempts["n"] += 1
        raise OperationalError("connection refused")

    with pytest.raises(OperationalError):
        _run_retry(monkeypatch, fake, max_attempts=3, base_delay_s=0)
    assert attempts["n"] == 3


def test_migration_retry_fails_fast_on_non_connection_errors(monkeypatch):
    attempts = {"n": 0}

    def fake():
        attempts["n"] += 1
        raise ValueError("broken migration")

    with pytest.raises(ValueError):
        _run_retry(monkeypatch, fake, base_delay_s=0)
    assert attempts["n"] == 1

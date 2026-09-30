"""Postgres connection + versioned migration runner.

Conventions:
- Migrations live in ``api/migrations/`` named ``NNNN_description.sql``
  (NNNN = zero-padded sequence, e.g. ``0001_init.sql``).
- Applied versions are recorded in the ``schema_migrations`` table.
- The runner applies pending migrations in version order inside a single
  transaction per file, then records the version. No manual SQL.
- When ``DATABASE_URL`` is unset, startup skips migrations with a warning
  so /healthz and the test suite work without Postgres.
- ``run_migrations`` holds a ``pg_advisory_lock`` so concurrent cold starts
  serialize instead of racing the migration DDL (H9).
- ``get_db_conn`` is the FastAPI dependency domains use for a request-scoped
  connection (dict rows), checked out from a process-wide
  ``psycopg_pool.ConnectionPool`` (M13). Tests override the repo factories instead of this,
  so most tests never need Postgres.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException

from .config import get_settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
FILENAME_RE = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")


def _migration_lock_key() -> int:
    """Stable session-lock key identifying the schema-migration runner.

    Derived from a fixed label so every process agrees on the value; fits
    in a signed 64-bit integer for ``pg_advisory_lock``.
    """
    digest = hashlib.sha256(b"garden-swap:schema-migrations").digest()
    return int.from_bytes(digest[:8], "big", signed=True)


# H9: advisory-lock key serializing concurrent migration runners (e.g. two
# Cloud Run instances cold-starting together). All processes must use the
# same key, so it is a fixed constant, not random per boot.
MIGRATION_ADVISORY_LOCK_KEY = _migration_lock_key()

# M13: bounds for the process-wide connection pool.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 20
_pool: Any = None
_pool_lock = threading.Lock()


def database_url() -> str | None:
    return get_settings().database_url


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Return (version, path) for every valid migration file, sorted by version."""
    found: list[tuple[int, Path]] = []
    if not migrations_dir.is_dir():
        return found
    for path in sorted(migrations_dir.glob("*.sql")):
        m = FILENAME_RE.match(path.name)
        if not m:
            logger.warning("ignoring non-conforming migration file: %s", path.name)
            continue
        found.append((int(m.group(1)), path))
    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise ValueError(f"duplicate migration versions in {migrations_dir}")
    return sorted(found, key=lambda t: t[0])


def pending_migrations(
    applied: set[int], migrations_dir: Path = MIGRATIONS_DIR
) -> list[tuple[int, Path]]:
    """Migrations not yet applied, in version order."""
    return [(v, p) for v, p in discover_migrations(migrations_dir) if v not in applied]


def run_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations. Returns the list of versions applied.

    H9: the whole run holds ``pg_advisory_lock(MIGRATION_ADVISORY_LOCK_KEY)``
    on its connection, released in a ``finally`` (and automatically on
    connection close). Without it, two Cloud Run instances booting together
    race ``CREATE TABLE schema_migrations`` and the constraint drop/add
    pairs — the loser dies with UniqueViolation and its instance fails to
    start.
    """
    url = database_url()
    if not url:
        logger.warning("DATABASE_URL unset — skipping migrations")
        return []
    import psycopg

    applied: list[int] = []
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,)
            )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                cur.execute("SELECT version FROM schema_migrations")
                done = {row[0] for row in cur.fetchall()}
            for version, path in pending_migrations(done, migrations_dir):
                sql = path.read_text(encoding="utf-8")
                logger.info("applying migration %s", path.name)
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                    )
                conn.commit()
                applied.append(version)
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,)
                )
    if applied:
        logger.info("migrations applied: %s", applied)
    return applied


def _create_pool(url: str) -> Any:
    """Construct the process-wide psycopg connection pool.

    Separated for testability: tests monkeypatch this instead of opening
    real connections.
    """
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    return ConnectionPool(
        url,
        kwargs={"row_factory": dict_row},
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        open=True,
    )


def _database_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "database_unavailable",
            "message": "DATABASE_URL is not configured",
        },
    )


def get_pool() -> Any:
    """Process-wide Postgres connection pool, created lazily on first use.

    M13: every request checks out a pooled connection instead of paying a
    fresh TCP+TLS handshake to Neon per request. Double-checked locking
    keeps exactly one pool per process. Raises 503 when DATABASE_URL is
    unset, mirroring ``get_db_conn``.
    """
    global _pool
    url = database_url()
    if not url:
        raise _database_unavailable()
    pool = _pool
    if pool is None:
        with _pool_lock:
            pool = _pool
            if pool is None:
                logger.info(
                    "opening Postgres connection pool (min_size=%d, max_size=%d)",
                    _POOL_MIN_SIZE,
                    _POOL_MAX_SIZE,
                )
                pool = _create_pool(url)
                _pool = pool
    return pool


def close_pool() -> None:
    """Close the process-wide pool if one was opened (shutdown / tests)."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        logger.info("closing Postgres connection pool")
        pool.close()


def get_db_conn() -> Iterator:
    """FastAPI dependency: request-scoped Postgres connection (dict rows).

    M13: the connection is checked out from the process-wide pool and
    returned to it when the request finishes, instead of opening a fresh
    ``psycopg.connect()`` (TCP+TLS to Neon) per request.

    Cold-start guard: Neon suspends idle databases, which kills the pooled
    TCP connections, and psycopg_pool does not validate on checkout — so
    the first request after a cold spell would check out a dead connection
    and die with an unhandled OperationalError (500 ``internal_error``).
    Ping each checkout with ``SELECT 1`` first; on failure close it (so the
    pool discards it) and re-check out, up to 3 attempts. A fresh checkout's
    TCP connect normally waits out Neon's resume server-side, so cold
    starts degrade to latency instead of errors. If the database stays
    unreachable, fail with 503 ``database_waking`` rather than 500.

    Only checkout/ping failures are retried: once the connection is
    yielded the route owns it, and its own errors propagate untouched
    (never retried here — a half-run route must not be mistaken for a
    bad checkout).

    Raises 503 when DATABASE_URL is unset so route handlers fail closed
    instead of crashing. Tests override the per-domain repo factories, so
    they exercise the Memory* repos and never touch this.
    """
    from psycopg import OperationalError

    pool = get_pool()
    for attempt in range(1, 4):
        in_route = False
        try:
            with pool.connection() as conn:
                try:
                    conn.execute("SELECT 1")
                except OperationalError:
                    logger.warning(
                        "db: stale pooled connection, discarding (attempt %d/3)",
                        attempt,
                    )
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                in_route = True
                yield conn
                return
        except OperationalError as exc:
            if in_route:
                # The route itself failed mid-flight — not a checkout
                # problem; never retry a half-run route.
                raise
            logger.warning("db: checkout failed (attempt %d/3): %s", attempt, exc)
    logger.error("db: unreachable after 3 checkout attempts")
    raise HTTPException(
        503,
        {
            "code": "database_waking",
            "message": "The database is waking up — please try again in a moment.",
        },
    )

"""Shared transaction helper for multi-repo money legs.

The Postgres repos share the request-scoped psycopg connection but each
method commits on its own, so a spend/earn/status-flip sequence would
land as separate commits. ``atomic`` suspends per-op commits on the
involved connections and lands a single commit at exit (rollback on
error). Memory repos expose no connection and are untouched — there,
idempotent ledger keys plus the routes' retry paths keep repeats
correct on a best-effort basis.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


@contextmanager
def atomic(*repos) -> Iterator[None]:
    """Collapse the repos' per-op commits into a single DB transaction."""
    conns = []
    for r in repos:
        c = getattr(r, "_conn", None)
        if c is not None and all(c is not other for other in conns):
            conns.append(c)
    originals = [c.commit for c in conns]
    try:
        for c in conns:
            c.commit = lambda: None
        yield
    except BaseException:
        for c, orig in zip(conns, originals):
            c.commit = orig
            try:
                c.rollback()
            except Exception:
                pass
        raise
    else:
        for c, orig in zip(conns, originals):
            c.commit = orig
            orig()
    finally:
        for c, orig in zip(conns, originals):
            c.commit = orig

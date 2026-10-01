"""Shared pytest fixtures for the GardenSwap API test suite."""
from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# Deterministic-at-import test keys for at-rest encryption (crypto.py).
# setdefault: a real env-provided key is never clobbered. get_cipher() reads
# the env fresh on every call, so tests can still monkeypatch.delenv to
# exercise the fail-closed paths.
os.environ.setdefault("MESSAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("GEO_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _clear_read_cache():
    """The process-global LRU read cache (app.cache) must not leak entries
    between tests — a stale entry from one test would poison the next."""
    from app.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    """Fresh app instance per test; DATABASE_URL unset so migrations are skipped."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "1000000")  # don't trip the limiter
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture()
def mock_verify(monkeypatch):
    """verify_id_token: 'good-token' -> uid alice (+ phone claim), else raises."""
    import app.auth as auth_mod

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "nophone-token":
            return {"uid": "nophone"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)


@pytest.fixture()
def auth_headers() -> dict:
    return {"Authorization": "Bearer good-token"}


def wire_credit_repo(client):
    """Override the credit-ledger repo with an in-memory one.

    Required by any fixture whose tests hit POST /v1/users or
    POST /v1/auth/verify (API-060 grants starter credits there).
    Returns the repo so tests can seed/inspect it.
    """
    from app import credits as credits_mod

    crepo = credits_mod.MemoryCreditRepo()
    client.app.dependency_overrides[credits_mod.get_credit_repo] = lambda: crepo
    return crepo


def wire_images_repo(client, blob_store=None):
    """Override the stored-images repo + GCS blob store with in-memory fakes.

    Required by any fixture whose tests hit routes that depend on the image
    library (uploads finalize, exchange confirm, harvest events, sweep,
    account deletion). ``blob_store`` defaults to None (image release
    no-ops); pass a fake to exercise GCS deletes. Also overrides the lazy
    ``users_mod._images_repo`` / ``users_mod._blob_store`` wrappers that
    DELETE /v1/users/me resolves through. Returns (images_repo, blob_store).
    """
    from app import images as images_mod
    from app import users as users_mod

    irepo = images_mod.MemoryStoredImagesRepo()
    client.app.dependency_overrides[images_mod.get_images_repo] = lambda: irepo
    client.app.dependency_overrides[images_mod.get_blob_store_or_none] = lambda: blob_store
    client.app.dependency_overrides[users_mod._images_repo] = lambda: irepo
    client.app.dependency_overrides[users_mod._blob_store] = lambda: blob_store
    return irepo, blob_store


@pytest.fixture()
def mem_users(client, monkeypatch):
    """Override the user repo with an in-memory one. Returns (client, repo)."""
    from app import users as users_mod
    from conftest import wire_images_repo

    repo = users_mod.MemoryUserRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: repo
    wire_credit_repo(client)
    wire_images_repo(client)
    return client, repo


@pytest.fixture()
def mem_listings(client, monkeypatch):
    """In-memory user + listing + want + notification + sitter repos.
    Returns (client, user_repo, listing_repo, want_repo, notify_repo)."""
    from app import listings as listings_mod
    from app import notify as notify_mod
    from app import sitter as sitter_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from conftest import wire_images_repo

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    wrepo = wantlist_mod.MemoryWantRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo
    client.app.dependency_overrides[sitter_mod.get_sitter_repo] = lambda: sitter_mod.MemorySitterRepo()
    wire_credit_repo(client)
    wire_images_repo(client)
    return client, urepo, lrepo, wrepo, nrepo


@pytest.fixture()
def mem_notify(client, monkeypatch):
    """In-memory notification repo. Returns (client, repo)."""
    from app import notify as notify_mod

    repo = notify_mod.MemoryNotificationRepo()
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: repo
    return client, repo

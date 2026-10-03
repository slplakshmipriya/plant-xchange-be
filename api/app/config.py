"""Central configuration (SEC-001).

Every setting comes from the environment. Nothing is read from files, and no
default in this module is a real credential. ``get_settings()`` reads the
environment fresh on each call so tests can monkeypatch env vars freely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (ValueError, TypeError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str | None       # Neon Postgres connection string
    firebase_project_id: str | None
    rate_limit_per_min: int        # per-instance token bucket (approximate)
    log_level: str
    port: int                      # honored from $PORT on Cloud Run
    # --- Wave 1 additions (all optional; None-safe defaults, no real creds) ---
    sweep_secret: str | None       # SWEEP_SECRET: shared secret for /v1/internal/sweep
    idv_provider: str              # IDV_PROVIDER: "stub" (dev) or real provider name
    idv_webhook_secret: str | None  # IDV_WEBHOOK_SECRET: HMAC secret for IDV webhooks
    idv_stub_enabled: bool         # ENABLE_IDV_STUB: explicit "1" to enable the
                                   # test-only stub IDV route/provider (default OFF)
    storage_backend: str           # STORAGE_BACKEND: "local" stub or "gcs"
    uploads_dir: str               # UPLOADS_DIR: local stub upload root
    gcs_bucket: str | None         # GCS_BUCKET: object-storage bucket for listing photos
    gcs_max_bytes: int             # GCS_MAX_BYTES: bucket quota cap (default 5 GiB Spark free tier)
    user_tz: str                   # USER_TZ: IANA tz for quiet-hours evaluation
    notify_daily_cap: int          # NOTIFY_DAILY_CAP: max notifications per user per day
    # --- Stripe Connect seam (API-071; stub until wired) ---
    payment_provider: str          # PAYMENT_PROVIDER: "stub" (dev) or "stripe"
    stripe_secret_key: str | None  # STRIPE_SECRET_KEY: platform secret key (sk_...)
    stripe_webhook_secret: str | None  # STRIPE_WEBHOOK_SECRET: whsec_... for webhooks
    # --- Phone hashing (M2) ---
    environment: str               # ENVIRONMENT: "local" (dev default), "staging", "production"
    phone_hash_secret: str | None  # PHONE_HASH_SECRET: HMAC key for phone_hash()
    # --- Pre-publish message moderation (NL moderateText) ---
    moderation_enabled: bool       # MODERATION_ENABLED: "1" (default) gates chat
                                   # sends through text_moderation before insert
    moderation_threshold: float    # MODERATION_THRESHOLD: block when a block-list
                                   # category scores >= this (default 0.8)


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL"),
        firebase_project_id=os.environ.get("FIREBASE_PROJECT_ID"),
        rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", 120),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        port=_int("PORT", 8080),
        sweep_secret=os.environ.get("SWEEP_SECRET"),
        idv_provider=os.environ.get("IDV_PROVIDER", "stub"),
        idv_webhook_secret=os.environ.get("IDV_WEBHOOK_SECRET"),
        idv_stub_enabled=os.environ.get("ENABLE_IDV_STUB", "0") == "1",
        storage_backend=os.environ.get("STORAGE_BACKEND", "local"),
        uploads_dir=os.environ.get("UPLOADS_DIR", "var/uploads"),
        gcs_bucket=os.environ.get("GCS_BUCKET"),
        gcs_max_bytes=_int("GCS_MAX_BYTES", 5_368_709_120),
        user_tz=os.environ.get("USER_TZ", "America/New_York"),
        notify_daily_cap=_int("NOTIFY_DAILY_CAP", 5),
        payment_provider=os.environ.get("PAYMENT_PROVIDER", "stub"),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY"),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET"),
        environment=os.environ.get("ENVIRONMENT", "local"),
        phone_hash_secret=os.environ.get("PHONE_HASH_SECRET"),
        moderation_enabled=os.environ.get("MODERATION_ENABLED", "1") == "1",
        moderation_threshold=_float("MODERATION_THRESHOLD", 0.8),
    )


def validate_idv_config(settings: Settings) -> None:
    """Fail-closed IDV deployment check (C1).

    Raises ``RuntimeError`` when the stub IDV provider is selected without the
    explicit ``ENABLE_IDV_STUB=1`` opt-in. Call at startup (e.g. in the app
    lifespan) so a production deploy that forgot ``IDV_PROVIDER`` — which
    defaults to ``"stub"`` — refuses to boot instead of shipping a live
    self-verification backdoor. The same check runs on every
    ``get_idv_provider()`` call in ``app/idv.py``, so the stub can never be
    instantiated without the opt-in even if startup validation is skipped.
    """
    if settings.idv_provider == "stub" and not settings.idv_stub_enabled:
        raise RuntimeError(
            "IDV stub provider selected without ENABLE_IDV_STUB=1. "
            "Set IDV_PROVIDER to a real vendor (and IDV_WEBHOOK_SECRET), "
            "or set ENABLE_IDV_STUB=1 for local dev/test only."
        )


def validate_phone_config(settings: Settings) -> None:
    """Fail-closed phone-hash check (M2).

    Without ``PHONE_HASH_SECRET`` the phone hash falls back to the legacy
    unsalted SHA-256, which is reversible from any DB leak (phone numbers
    have ~1e10 possibilities). Deployed environments must set the secret;
    local dev/test keep the legacy fallback so existing rows still verify
    (verify.py dual-reads and upgrades rows lazily)."""
    if settings.environment in ("staging", "production", "prod") \
            and not settings.phone_hash_secret:
        raise RuntimeError(
            "PHONE_HASH_SECRET is not set. Phone numbers would be stored "
            "as unsalted SHA-256 (reversible from a DB leak). Set "
            "PHONE_HASH_SECRET (e.g. from Secret Manager) for "
            "staging/production, or run with ENVIRONMENT=local for dev."
        )

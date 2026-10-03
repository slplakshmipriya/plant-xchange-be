"""Notification service (API-081) + R2 prefs / pipeline / FCM track.

``send_notification(uid, category, title, body, data, ...)`` is the single
choke point for every push the backend emits (harvest alerts, want-list
matches, expiry nudges, ...). Guards, in order:

1. Per-category opt-in (``notification_prefs``; a category absent from the
   map defaults to opted in). The five canonical categories are
   ``harvestAlerts``, ``wantMatches``, ``expiryNudges``, ``creditWarnings``
   and ``bookingReminders``; legacy snake_case names (``harvest_alerts``,
   ``ripe_alert``, ``match``, ...) alias onto them, and opting out under
   either spelling suppresses the whole group.
2. Quiet hours: a per-user window (default 21:00-08:00) evaluated in
   ``USER_TZ`` (default America/New_York). During quiet hours the send is
   LOGGED and SKIPPED — a real queue would defer delivery until the window
   ends; this MVP has no queue, so the event is dropped honestly.
3. Dedupe: same (category, ref) within 24h is skipped.
4. Per-user cap: ``NOTIFY_DAILY_CAP`` sends per rolling 24h (default 5).

Every attempt is written to ``notification_log``.

Pipeline hooks (``dispatch_event`` + the ``on_*`` wrappers) are standalone
functions: they resolve recipients, run the guard chain, and deliver via
registered FCM device tokens (``send_push``) with the legacy per-user topic
as fallback. They are unit-tested directly; the coordinator wires the call
sites (tree ripe-window job, listing create, expiry sweeps, credit sweeps)
after merge — this module is never imported for side effects by the tracks
that own those call sites.

FCM — going live:
- ``send_push`` uses ``firebase_admin.messaging`` when the SDK is
  initialized and degrades to structured logging (``would_send``) when it
  is not, so local dev and CI never touch the network.
- To go live, initialize the SDK once at startup, e.g.::

      firebase_admin.initialize_app()

  which reads ``GOOGLE_APPLICATION_CREDENTIALS`` (path to a service-account
  JSON) or falls back to Application Default Credentials. Set
  ``FIREBASE_PROJECT_ID`` when the credentials do not imply a project.
- Device tokens are registered by the Android client after sign-in via
  ``POST /v1/users/me/fcm-token`` (``FirebaseMessaging.getInstance().token``);
  tokens are stored per user in ``device_tokens`` (migration 0018) and used
  for token-targeted sends. Tokens are deregistered via
  ``DELETE /v1/users/me/fcm-token`` (logout / uninstall / rotation) and
  pruned automatically when FCM reports them unregistered.
- The old ``user_{uid}`` topic fallback stays for users with no registered
  token — **DEPRECATED** (H13): topics have no ACLs, so anyone can subscribe
  to ``user_{uid}`` and read push content. It remains ON by default so
  existing pushes keep working; set ``FCM_TOPIC_FALLBACK_ENABLED=0`` to
  disable it once token-registration coverage is high. Sensitive content
  must NEVER ride the topic path — ``send_notification`` /
  ``dispatch_event`` take ``sensitive=True``, which the topic sender
  rejects (code-level guard).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import get_current_uid
from .config import get_settings
from .db import get_db_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/users/me", tags=["notifications"])

DELIVERED = ("sent", "would_send")  # outcomes that count toward dedupe/cap
DEDUPE_WINDOW = timedelta(hours=24)
CAP_WINDOW = timedelta(hours=24)
QUIET_START_HOUR = 21  # default window 21:00 local ...
QUIET_END_HOUR = 8     # ... to 08:00 local
DEFAULT_QUIET_START = "21:00"
DEFAULT_QUIET_END = "08:00"
_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

#: Canonical notification categories (camelCase, as exposed in the prefs API).
CANONICAL_CATEGORIES = (
    "harvestAlerts",
    "wantMatches",
    "expiryNudges",
    "creditWarnings",
    "bookingReminders",
)

#: Legacy / internal snake_case names alias onto the canonical categories.
#: Opting out under ANY spelling in a group suppresses the whole group.
CATEGORY_GROUPS: dict[str, set[str]] = {
    "harvestAlerts": {"harvestAlerts", "harvest_alerts", "ripe_alert"},
    "wantMatches": {"wantMatches", "match", "wantlist_match"},
    "expiryNudges": {"expiryNudges", "expiry_nudge"},
    "creditWarnings": {"creditWarnings", "credit_warning"},
    "bookingReminders": {"bookingReminders", "booking_reminder"},
}
_GROUP_OF: dict[str, set[str]] = {
    alias: group for group in CATEGORY_GROUPS.values() for alias in group
}


def _now() -> datetime:
    """UTC now. Monkeypatchable seam for tests (freeze time)."""
    return datetime.now(timezone.utc)


def _parse_hhmm(value: str) -> tuple[int, int]:
    if not _HHMM_RE.match(value or ""):
        raise ValueError(f"not HH:MM 24h: {value!r}")
    hours, minutes = value.split(":")
    return int(hours), int(minutes)


def _resolve_user_tz() -> ZoneInfo:
    """Resolve ``USER_TZ`` (M14).

    A typo'd ``USER_TZ`` must not 500 every notification send: fall back to
    UTC with a logged warning.
    """
    tz_name = get_settings().user_tz
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError and friends
        logger.warning("invalid USER_TZ %r; falling back to UTC", tz_name)
        return ZoneInfo("UTC")


def validate_user_tz(settings) -> None:
    """Startup check for ``USER_TZ`` (M14).

    Loud warning, not fatal — the runtime path falls back to UTC — so a
    typo is noticed before it silently shifts every user's quiet-hours
    window. Call from lifespan alongside the other ``validate_*`` checks.
    """
    try:
        ZoneInfo(settings.user_tz)
    except Exception:  # noqa: BLE001 — bad IANA name
        logger.warning(
            "invalid USER_TZ %r at startup; quiet hours will evaluate in UTC",
            settings.user_tz,
        )


def in_quiet_hours(
    now: datetime | None = None,
    start: str = DEFAULT_QUIET_START,
    end: str = DEFAULT_QUIET_END,
) -> bool:
    """True when local time falls inside the [start, end) window (wraps midnight).

    A degenerate equal start/end disables the window. Unparseable bounds
    fail safe to "not quiet" and log. A bad ``USER_TZ`` falls back to UTC
    (M14) instead of raising.
    """
    try:
        start_min = _parse_hhmm(start)[0] * 60 + _parse_hhmm(start)[1]
        end_min = _parse_hhmm(end)[0] * 60 + _parse_hhmm(end)[1]
    except ValueError:
        logger.warning("invalid quiet-hours window %r-%r; treating as disabled", start, end)
        return False
    if start_min == end_min:
        return False
    local = (now or _now()).astimezone(_resolve_user_tz())
    minute = local.hour * 60 + local.minute
    if start_min > end_min:  # wraps midnight
        return minute >= start_min or minute < end_min
    return start_min <= minute < end_min


def _category_opted_in(categories: dict[str, bool], category: str) -> bool:
    group = _GROUP_OF.get(category, {category})
    return all(categories.get(name, True) for name in group)


def _default_prefs() -> dict[str, Any]:
    return {
        "categories": {c: True for c in CANONICAL_CATEGORIES},
        "quiet_hours": True,
        "quiet_hours_start": DEFAULT_QUIET_START,
        "quiet_hours_end": DEFAULT_QUIET_END,
    }


class NotificationRepo(Protocol):
    def get_prefs(self, uid: str) -> dict[str, Any] | None: ...
    def set_prefs(
        self,
        uid: str,
        categories: dict[str, bool],
        quiet_hours: bool,
        quiet_hours_start: str = DEFAULT_QUIET_START,
        quiet_hours_end: str = DEFAULT_QUIET_END,
    ) -> dict[str, Any]: ...
    def get_tokens(self, uid: str) -> list[str]: ...
    def register_token(self, uid: str, token: str, platform: str = "android") -> None: ...
    def unregister_token(self, uid: str, token: str) -> bool:
        """Remove one device token. Returns True when a row was removed."""
        ...
    def has_recent(self, uid: str, category: str, ref: str, since: datetime) -> bool: ...
    def count_since(self, uid: str, since: datetime) -> int: ...
    def log(self, uid: str, category: str, ref: str, outcome: str) -> None: ...


class PostgresNotificationRepo:
    def __init__(self, conn):
        self._conn = conn

    def get_prefs(self, uid: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT user_uid, categories, quiet_hours, quiet_hours_start, quiet_hours_end "
            "FROM notification_prefs WHERE user_uid = %s",
            (uid,),
        ).fetchone()
        return dict(row) if row else None

    def set_prefs(
        self,
        uid: str,
        categories: dict[str, bool],
        quiet_hours: bool,
        quiet_hours_start: str = DEFAULT_QUIET_START,
        quiet_hours_end: str = DEFAULT_QUIET_END,
    ) -> dict[str, Any]:
        import json as _json

        self._conn.execute(
            "INSERT INTO notification_prefs "
            "(user_uid, categories, quiet_hours, quiet_hours_start, quiet_hours_end) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_uid) DO UPDATE SET categories = EXCLUDED.categories, "
            "quiet_hours = EXCLUDED.quiet_hours, "
            "quiet_hours_start = EXCLUDED.quiet_hours_start, "
            "quiet_hours_end = EXCLUDED.quiet_hours_end",
            (uid, _json.dumps(categories), quiet_hours, quiet_hours_start, quiet_hours_end),
        )
        self._conn.commit()
        return self.get_prefs(uid)

    def get_tokens(self, uid: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT token FROM device_tokens WHERE user_uid = %s ORDER BY last_seen_at DESC",
            (uid,),
        ).fetchall()
        return [r["token"] for r in rows]

    def register_token(self, uid: str, token: str, platform: str = "android") -> None:
        # One owner per token (0042): re-registering a token reassigns it
        # to the latest account instead of fanning pushes out to all of
        # them (L4).
        self._conn.execute(
            "INSERT INTO device_tokens (user_uid, token, platform) VALUES (%s,%s,%s) "
            "ON CONFLICT (token) DO UPDATE SET "
            "user_uid = EXCLUDED.user_uid, "
            "platform = EXCLUDED.platform, last_seen_at = now()",
            (uid, token, platform),
        )
        self._conn.commit()

    def unregister_token(self, uid: str, token: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM device_tokens WHERE user_uid = %s AND token = %s",
            (uid, token),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def has_recent(self, uid: str, category: str, ref: str, since: datetime) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM notification_log WHERE user_uid = %s AND category = %s "
            "AND ref = %s AND sent_at > %s AND outcome = ANY(%s) LIMIT 1",
            (uid, category, ref, since, list(DELIVERED)),
        ).fetchone()
        return row is not None

    def count_since(self, uid: str, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM notification_log WHERE user_uid = %s "
            "AND sent_at > %s AND outcome = ANY(%s)",
            (uid, since, list(DELIVERED)),
        ).fetchone()
        return int(row["n"])

    def log(self, uid: str, category: str, ref: str, outcome: str) -> None:
        self._conn.execute(
            "INSERT INTO notification_log (user_uid, category, ref, outcome) VALUES (%s,%s,%s,%s)",
            (uid, category, ref, outcome),
        )
        self._conn.commit()


class MemoryNotificationRepo:
    def __init__(self):
        self._prefs: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, list[str]] = {}
        self._log: list[dict[str, Any]] = []

    def get_prefs(self, uid: str) -> dict[str, Any] | None:
        prefs = self._prefs.get(uid)
        return dict(prefs) if prefs else None

    def set_prefs(
        self,
        uid: str,
        categories: dict[str, bool],
        quiet_hours: bool,
        quiet_hours_start: str = DEFAULT_QUIET_START,
        quiet_hours_end: str = DEFAULT_QUIET_END,
    ) -> dict[str, Any]:
        self._prefs[uid] = {
            "user_uid": uid,
            "categories": dict(categories),
            "quiet_hours": quiet_hours,
            "quiet_hours_start": quiet_hours_start,
            "quiet_hours_end": quiet_hours_end,
        }
        return dict(self._prefs[uid])

    def get_tokens(self, uid: str) -> list[str]:
        return list(self._tokens.get(uid, []))

    def register_token(self, uid: str, token: str, platform: str = "android") -> None:
        # Mirror the Postgres semantics (0042): a token belongs to exactly
        # one account — registering it elsewhere moves it.
        for other_uid, toks in self._tokens.items():
            if other_uid != uid and token in toks:
                toks.remove(token)
        tokens = self._tokens.setdefault(uid, [])
        if token not in tokens:
            tokens.append(token)

    def unregister_token(self, uid: str, token: str) -> bool:
        tokens = self._tokens.get(uid, [])
        if token in tokens:
            tokens.remove(token)
            return True
        return False

    def has_recent(self, uid: str, category: str, ref: str, since: datetime) -> bool:
        return any(
            e["user_uid"] == uid and e["category"] == category and e["ref"] == ref
            and e["sent_at"] > since and e["outcome"] in DELIVERED
            for e in self._log
        )

    def count_since(self, uid: str, since: datetime) -> int:
        return sum(
            1 for e in self._log
            if e["user_uid"] == uid and e["sent_at"] > since and e["outcome"] in DELIVERED
        )

    def log(self, uid: str, category: str, ref: str, outcome: str) -> None:
        self._log.append({"user_uid": uid, "category": category, "ref": ref,
                          "outcome": outcome, "sent_at": _now()})


def get_notification_repo(conn=Depends(get_db_conn)) -> NotificationRepo:
    return PostgresNotificationRepo(conn)


def _check_guards(
    uid: str, category: str, ref: str, repo: NotificationRepo, now: datetime
) -> str | None:
    """Run the guard chain. Returns the skip reason, or None when delivery may proceed."""
    prefs = repo.get_prefs(uid) or _default_prefs()
    categories = prefs.get("categories") or {}

    if not _category_opted_in(categories, category):
        return "opted_out"

    if prefs.get("quiet_hours", True) and in_quiet_hours(
        now,
        prefs.get("quiet_hours_start") or DEFAULT_QUIET_START,
        prefs.get("quiet_hours_end") or DEFAULT_QUIET_END,
    ):
        return "quiet_hours"

    if ref and repo.has_recent(uid, category, ref, now - DEDUPE_WINDOW):
        return "duplicate"

    cap = get_settings().notify_daily_cap
    if repo.count_since(uid, now - CAP_WINDOW) >= cap:
        return "rate_capped"

    return None


def _topic_fallback_enabled() -> bool:
    """Kill-switch for the legacy per-user topic fallback (H13).

    Default True (current behavior — existing pushes keep working). Set
    ``FCM_TOPIC_FALLBACK_ENABLED=0`` to disable once token-registration
    coverage is high. The topic path is deprecated: topics have no ACLs, so
    anyone who subscribes to ``user_{uid}`` can read the push content.
    """
    return os.environ.get("FCM_TOPIC_FALLBACK_ENABLED", "1") == "1"


def default_fcm_sender(uid: str, title: str, body: str, data: dict[str, str]) -> None:
    """Send via firebase-admin to the per-user topic.

    .. deprecated::
        The ``user_{uid}`` topic path has no ACLs — anyone can subscribe and
        read push content. It stays as the fallback for users with no
        registered device token (disable via ``FCM_TOPIC_FALLBACK_ENABLED=0``),
        and it REJECTS sensitive sends: ``send_notification`` /
        ``dispatch_event`` with ``sensitive=True`` never reach this function.

    The Android client MUST subscribe to topic ``user_{uid}`` after
    sign-in for this path to deliver. Token-targeted sends (``send_push``)
    are the preferred path.
    Raises when credentials/app are unavailable.
    """
    import firebase_admin
    from firebase_admin import messaging

    firebase_admin.get_app()  # raises ValueError when uninitialized
    messaging.send(messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in (data or {}).items()},
        topic=f"user_{uid}",
    ))


def _is_token_unregistered(exc: BaseException) -> bool:
    """True when FCM says the registration token is dead (M21 prune signal).

    Duck-types on the firebase-admin exception classes when the SDK is
    present; falls back to class-name matching so fakes/stubs can drive
    pruning in tests.
    """
    try:
        from firebase_admin import messaging

        return isinstance(
            exc, (messaging.UnregisteredError, messaging.SenderIdMismatchError)
        )
    except Exception:  # noqa: BLE001 — SDK absent in dev/CI
        return type(exc).__name__ in ("UnregisteredError", "SenderIdMismatchError")


def send_push(
    token: str, title: str, body: str, data: dict[str, str] | None = None
) -> dict[str, str]:
    """Token-targeted FCM send. The seam tests monkeypatch — never hits the
    network when firebase-admin is uninitialized (local dev / CI).

    Returns ``{"status": "sent"}`` on delivery, ``{"status": "would_send"}``
    when the SDK is unavailable (structured-logged, no exception escapes),
    ``{"status": "failed"}`` when FCM rejects the send. On unregistered-token
    errors the result also carries ``"prune_token": True`` so the caller can
    drop the dead token (M21).
    """
    try:
        import firebase_admin
        from firebase_admin import messaging

        firebase_admin.get_app()  # raises ValueError when uninitialized
    except Exception as exc:  # noqa: BLE001 — no creds in dev/CI is expected
        # Structured log only: never log a full device token.
        logger.info(
            "fcm send deferred (sdk unavailable: %s)",
            type(exc).__name__,
            extra={"token_suffix": token[-6:], "title": title},
        )
        return {"status": "would_send", "reason": "fcm_uninitialized"}
    try:
        messaging.send(messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
        ))
    except Exception as exc:  # noqa: BLE001 — FCM-side failure, recorded honestly
        logger.warning(
            "fcm send failed (%s)",
            type(exc).__name__,
            extra={"token_suffix": token[-6:], "title": title},
        )
        return {
            "status": "failed",
            "reason": "fcm_error",
            "prune_token": _is_token_unregistered(exc),
        }
    return {"status": "sent", "reason": "delivered"}


def send_notification(
    uid: str,
    category: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
    *,
    ref: str = "",
    repo: NotificationRepo,
    sender=default_fcm_sender,
    sensitive: bool = False,
) -> dict[str, str]:
    """Run the guard chain and deliver (or honestly record why not).

    ``sensitive=True`` is the H13 code-level guard: personal content (chat
    snippets, addresses, names) must never ride the ACL-less ``user_{uid}``
    topic path, so a sensitive send through the default topic sender is
    refused outright. Callers with their own token-targeted sender pass it
    explicitly.
    """
    now = _now()
    skip = _check_guards(uid, category, ref, repo, now)
    if skip is not None:
        repo.log(uid, category, ref, f"skipped_{skip}")
        return {"status": "skipped", "reason": skip}

    if sensitive and sender is default_fcm_sender:
        logger.warning("refusing sensitive send via topic fallback for %s", uid)
        repo.log(uid, category, ref, "skipped_sensitive_not_routed_via_topic")
        return {"status": "skipped", "reason": "sensitive_not_routed_via_topic"}

    try:
        sender(uid, title, body, data or {})
    except Exception as exc:  # noqa: BLE001 — no creds in dev/CI is expected
        logger.info("FCM unavailable, recording would-send: %s", type(exc).__name__)
        repo.log(uid, category, ref, "would_send")
        return {"status": "would_send", "reason": "fcm_unavailable"}

    repo.log(uid, category, ref, "sent")
    return {"status": "sent", "reason": "delivered"}


# ---------------------------------------------------------------------------
# Pipeline hooks. Standalone functions, unit-tested directly. The coordinator
# wires the call sites after merge; do not import track-owned modules here
# (wantlist imports this module — its matcher is imported lazily below).
# ---------------------------------------------------------------------------

def _want_recipients(row: dict[str, Any], want_repo: Any, payload: dict[str, Any]) -> list[str]:
    """Users whose want-list matches a listing/tree row.

    Mirrors ``wantlist.notify_matches`` recipient semantics (excludes the
    owner, substring variety match, type filter). When no want repo is
    supplied (pure unit tests), falls back to ``payload["user_uids"]``.
    """
    if want_repo is not None:
        from .wantlist import find_matches  # deferred: wantlist imports notify

        return [e["user_uid"] for e in find_matches(row, want_repo.list_all())]
    return [str(u) for u in payload.get("user_uids", [])]


def _build_ripe_window(
    payload: dict[str, Any], want_repo: Any
) -> tuple[list[str], str, str, str, dict[str, str], str]:
    tree = payload.get("tree", {}) or {}
    tid = str(tree.get("id", ""))
    variety = tree.get("variety")
    recipients = _want_recipients(
        {"owner_uid": tree.get("owner_uid"), "variety": variety, "type": "tree"},
        want_repo,
        payload,
    )
    return (
        recipients,
        "harvestAlerts",
        "Fruit is ripe near you",
        f"{variety or 'A tree'} you want entered its ripe window.",
        {"tree_id": tid, "variety": str(variety or "")},
        f"tree:{tid}:ripe",
    )


def _build_new_listing(
    payload: dict[str, Any], want_repo: Any
) -> tuple[list[str], str, str, str, dict[str, str], str]:
    listing = payload.get("listing", {}) or {}
    lid = str(listing.get("id", ""))
    variety = listing.get("variety")
    recipients = _want_recipients(listing, want_repo, payload)
    return (
        recipients,
        "wantMatches",
        "A seedling you want is nearby",
        f"{variety or 'A plant'} you want was just listed.",
        {"listing_id": lid, "variety": str(variety or "")},
        f"listing:{lid}:match",
    )


def _build_expiry_nudge(
    payload: dict[str, Any], want_repo: Any
) -> tuple[list[str], str, str, str, dict[str, str], str]:
    listing = payload.get("listing", {}) or {}
    lid = str(listing.get("id", ""))
    hours_left = int(payload.get("hours_left", 48))
    owner = listing.get("owner_uid")
    recipients = [str(owner)] if owner else []
    return (
        recipients,
        "expiryNudges",
        "Your listing expires soon",
        f"Your listing for {listing.get('variety') or 'your plant'} expires in "
        f"{hours_left}h — renew it or mark it exchanged.",
        {"listing_id": lid, "hours_left": str(hours_left)},
        f"listing:{lid}:expiry:{hours_left}h",
    )


def _build_credit_warning(
    payload: dict[str, Any], want_repo: Any
) -> tuple[list[str], str, str, str, dict[str, str], str]:
    user_id = str(payload.get("user_id", ""))
    days_left = int(payload.get("days_left", 7))
    credits = payload.get("credits", 0)
    return (
        [user_id] if user_id else [],
        "creditWarnings",
        "Credits expiring soon",
        f"{credits} of your credits expire in {days_left} days — spend them on a swap!",
        {"days_left": str(days_left), "credits": str(credits)},
        f"credits:{user_id}:expiry:{days_left}d",
    )


# H13 audit (2026-09-28): every builder below emits generic titles/bodies
# ("Fruit is ripe near you", "A seedling you want is nearby", "Your listing
# expires soon", "Credits expiring soon"). None carries names, addresses,
# chat snippets, or other personal content, so all four events are safe for
# the legacy topic fallback. Mark sensitive=True on dispatch_event for any
# future event that carries personal content — the topic path refuses it.
_EVENT_BUILDERS = {
    "ripe_window_entry": _build_ripe_window,
    "new_listing": _build_new_listing,
    "listing_expiry_nudge": _build_expiry_nudge,
    "credit_expiry_warning": _build_credit_warning,
}


def _deliver_to_recipient(
    uid: str,
    category: str,
    title: str,
    body: str,
    data: dict[str, str],
    ref: str,
    repo: NotificationRepo,
    now: datetime,
    *,
    sensitive: bool = False,
) -> dict[str, Any]:
    """Guard chain + token-targeted delivery for one recipient.

    Prefers registered device tokens (``send_push``); falls back to the
    legacy per-user topic when the user has no token — unless the topic
    fallback is disabled (``FCM_TOPIC_FALLBACK_ENABLED=0``) or the send is
    ``sensitive`` (H13: sensitive content is never routed through the
    ACL-less topic path). Dead tokens reported by FCM are pruned (M21).
    Every outcome is logged to ``notification_log``.
    """
    skip = _check_guards(uid, category, ref, repo, now)
    if skip is not None:
        repo.log(uid, category, ref, f"skipped_{skip}")
        logger.info("notification skipped: %s for %s (%s)", category, uid, skip)
        return {"uid": uid, "status": "skipped", "reason": skip}

    tokens = repo.get_tokens(uid)
    if tokens:
        results = [send_push(token, title, body, data) for token in tokens]
        for token, result in zip(tokens, results):
            if result.get("prune_token") and repo.unregister_token(uid, token):
                logger.info(
                    "pruned dead FCM token",
                    extra={"token_suffix": token[-6:]},
                )
        outcome = "sent" if any(r["status"] == "sent" for r in results) else "would_send"
        via = f"{len(tokens)}_token(s)"
    elif sensitive:
        # H13 code-level guard: the topic path rejects sensitive content.
        logger.warning("refusing sensitive send via topic fallback for %s", uid)
        repo.log(uid, category, ref, "skipped_sensitive_not_routed_via_topic")
        return {
            "uid": uid,
            "status": "skipped",
            "reason": "sensitive_not_routed_via_topic",
        }
    elif not _topic_fallback_enabled():
        repo.log(uid, category, ref, "skipped_topic_fallback_disabled")
        logger.info("notification skipped: %s for %s (topic_fallback_disabled)",
                    category, uid)
        return {
            "uid": uid,
            "status": "skipped",
            "reason": "topic_fallback_disabled",
        }
    else:
        try:
            default_fcm_sender(uid, title, body, data)
            outcome = "sent"
        except Exception as exc:  # noqa: BLE001 — no creds in dev/CI is expected
            logger.info("FCM unavailable, recording would-send: %s", type(exc).__name__)
            outcome = "would_send"
        via = "topic_fallback"

    repo.log(uid, category, ref, outcome)
    return {
        "uid": uid,
        "status": outcome,
        "reason": "delivered" if outcome == "sent" else "fcm_unavailable",
        "via": via,
    }


def dispatch_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    notify_repo: NotificationRepo,
    want_repo: Any = None,
    now: datetime | None = None,
    sensitive: bool = False,
) -> dict[str, Any]:
    """Central pipeline entry point: resolve recipients, run guards, deliver.

    ``payload`` shapes per event type (see the ``on_*`` wrappers). Returns a
    summary with per-recipient results; raises ``ValueError`` for an unknown
    event type. ``sensitive=True`` (H13) refuses the ACL-less topic fallback
    for sends carrying personal content.
    """
    builder = _EVENT_BUILDERS.get(event_type)
    if builder is None:
        raise ValueError(f"unknown event_type: {event_type!r}")
    recipients, category, title, body, data, ref = builder(payload or {}, want_repo)
    ts = now or _now()
    results = [
        _deliver_to_recipient(
            uid, category, title, body, data, ref, notify_repo, ts,
            sensitive=sensitive,
        )
        for uid in recipients
    ]
    by_reason: dict[str, int] = {}
    for r in results:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    return {
        "event_type": event_type,
        "category": category,
        "recipients": len(recipients),
        "delivered": sum(1 for r in results if r["status"] in DELIVERED),
        "by_reason": by_reason,
        "results": results,
    }


def on_ripe_window_entry(
    tree: dict[str, Any],
    *,
    notify_repo: NotificationRepo,
    want_repo: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A tree entered its ripe window → ``harvestAlerts`` to opted-in wanters.

    ``tree`` carries at least ``id``/``variety`` (and ``owner_uid`` so the
    owner is not notified about their own tree).
    """
    return dispatch_event(
        "ripe_window_entry", {"tree": tree},
        notify_repo=notify_repo, want_repo=want_repo, now=now,
    )


def on_new_listing(
    listing: dict[str, Any],
    *,
    notify_repo: NotificationRepo,
    want_repo: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A listing was created → ``wantMatches`` to opted-in wanters.

    Recipient matching mirrors ``wantlist.notify_matches`` (variety substring
    + type filter, owner excluded); delivery goes through the token-aware
    pipeline instead of the legacy topic sender.
    """
    return dispatch_event(
        "new_listing", {"listing": listing},
        notify_repo=notify_repo, want_repo=want_repo, now=now,
    )


def on_listing_expiry_nudge(
    listing: dict[str, Any],
    hours_left: int,
    *,
    notify_repo: NotificationRepo,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Listing expiry nudge → ``expiryNudges`` to the listing owner.

    Callers schedule the 48h and 12h marks; each mark gets its own dedupe
    ref so both fire.
    """
    return dispatch_event(
        "listing_expiry_nudge", {"listing": listing, "hours_left": hours_left},
        notify_repo=notify_repo, now=now,
    )


def on_credit_expiry_warning(
    user_id: str,
    days_left: int,
    credits: int,
    *,
    notify_repo: NotificationRepo,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Credit expiry warning → ``creditWarnings``.

    Callers schedule the 30d and 7d marks; each mark gets its own dedupe ref.
    """
    return dispatch_event(
        "credit_expiry_warning",
        {"user_id": user_id, "days_left": days_left, "credits": credits},
        notify_repo=notify_repo, now=now,
    )


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class QuietHoursWindow(BaseModel):
    start: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class PrefsIn(BaseModel):
    """PUT body. ``categories`` REPLACES the stored map (absent categories
    default to opted-in at send time). ``quiet_hours`` is the legacy
    snake_case toggle; ``quietHours`` is the new window object — an explicit
    ``null`` disables quiet hours, an object enables them with that window.
    When both are present, ``quietHours`` wins.
    """

    categories: dict[str, bool] = Field(default_factory=dict)
    quiet_hours: bool | None = None
    quietHours: QuietHoursWindow | None = None


class FcmTokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    platform: str = Field(default="android", max_length=32)


def _prefs_response(uid: str, prefs: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(prefs.get("quiet_hours", True))
    return {
        "user_uid": uid,
        "categories": prefs.get("categories") or {},
        "quiet_hours": enabled,  # legacy snake_case toggle, kept for compat
        "quietHours": (
            {
                "start": prefs.get("quiet_hours_start") or DEFAULT_QUIET_START,
                "end": prefs.get("quiet_hours_end") or DEFAULT_QUIET_END,
            }
            if enabled
            else None
        ),
    }


@router.put("/notification-prefs")
def put_notification_prefs(
    data: PrefsIn,
    uid: str = Depends(get_current_uid),
    repo: NotificationRepo = Depends(get_notification_repo),
) -> dict[str, Any]:
    existing = repo.get_prefs(uid) or {}
    enabled = bool(existing.get("quiet_hours", True))
    start = existing.get("quiet_hours_start") or DEFAULT_QUIET_START
    end = existing.get("quiet_hours_end") or DEFAULT_QUIET_END

    if data.quiet_hours is not None:
        enabled = data.quiet_hours
    if "quietHours" in data.model_fields_set:
        # Explicit null disables; an object enables with that window.
        if data.quietHours is None:
            enabled = False
        else:
            enabled, start, end = True, data.quietHours.start, data.quietHours.end

    prefs = repo.set_prefs(uid, dict(data.categories), enabled, start, end)
    return _prefs_response(uid, prefs)


@router.get("/notification-prefs")
def get_notification_prefs(
    uid: str = Depends(get_current_uid),
    repo: NotificationRepo = Depends(get_notification_repo),
) -> dict[str, Any]:
    prefs = repo.get_prefs(uid)
    if prefs is None:
        prefs = {"user_uid": uid, **_default_prefs()}
    return _prefs_response(uid, prefs)


@router.post("/fcm-token", status_code=201)
def register_fcm_token(
    data: FcmTokenIn,
    uid: str = Depends(get_current_uid),
    repo: NotificationRepo = Depends(get_notification_repo),
) -> dict[str, Any]:
    """Register (or refresh) a device FCM registration token for push.

    Called by the Android client after sign-in and whenever
    ``FirebaseMessaging.getInstance().token`` rotates. Idempotent per
    (user, token). Returns a success flag — the token itself is a secret
    and is never echoed back (L4d).
    """
    token = data.token.strip()
    if not token:
        raise HTTPException(422, {"code": "empty_token", "message": "token must not be blank"})
    repo.register_token(uid, token, data.platform)
    return {"user_uid": uid, "registered": True, "platform": data.platform}


class FcmTokenDeleteIn(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


@router.delete("/fcm-token")
def delete_fcm_token(
    data: FcmTokenDeleteIn,
    uid: str = Depends(get_current_uid),
    repo: NotificationRepo = Depends(get_notification_repo),
) -> dict[str, Any]:
    """Deregister a device FCM registration token (M21).

    Called by the Android client on sign-out / uninstall / token rotation.
    Idempotent: removing a token that isn't registered returns
    ``removed: false`` (200), never 404.
    """
    token = data.token.strip()
    if not token:
        raise HTTPException(422, {"code": "empty_token", "message": "token must not be blank"})
    removed = repo.unregister_token(uid, token)
    return {"user_uid": uid, "removed": removed}

"""Credit ledger (API-060) + seasonal credit economy (R2).

Ledger:
- The ledger is append-only and is the source of truth. Reasons:
  ``starter`` / ``exchange_spend`` / ``exchange_earn``.
- New users get 3 starter credits (PRD bootstrap) via ``ensure_starter_credits``
  — idempotent, safe to call from every user-creation path; the 0009 migration
  backfills pre-existing users.

Seasonal expiry:
- Two credit seasons per year (UTC): Mar 1–Sep 30 and Oct 1–Feb 28/29.
  Credits expire at the end of the season they were issued in. Expiry is a
  DERIVED view over ``created_at`` (FIFO lots via ``_remaining_lots``) — the
  ledger is never mutated or deleted for expiry, so it stays append-only.
- ``season_end_ms`` / ``expiry_warnings`` are pure helpers; the 30-day and
  7-day warning windows feed ``GET /v1/users/me/credit-expiry``, which stays
  surfaced as the canonical expiry outlook (M22).

Starter grant (M22 reconciliation):
- PRD.md:169 mentions a "seasonal starter refresh" for users at 0 credits.
  That refresh does NOT exist: ``ensure_starter_credits`` grants the
  3-credit bootstrap exactly once per user, ever. Until a refresh is
  designed and implemented, this docstring — not the PRD line — is the
  source of truth; the PRD wording still needs reconciling.

Anti-gaming earn cap:
- Max 10 credits of new ISSUANCE per user per rolling 7 days, enforced
  inside ``add_entry`` — the single choke point every issuance flow goes
  through. "Issuance" excludes transfers: ``claim_earn`` /
  ``exchange_earn`` / ``slot_earn`` move existing credits between users
  and ``claim_reversal`` / ``dispute_reversal`` hand a user's own credits
  back, so none of them can inflate the supply or be "gamed" by earning
  — and since migration 0039 prices run 1–100, capping transfer earns
  would make most listings un-completable and (before the money legs
  were made atomic) silently destroyed claimers' credits. Breaches on
  genuine issuance raise ``EarnCapExceededError`` (HTTP 409
  ``earn_cap_exceeded``). The starter bootstrap is exempt.
- H12: the cap check and the INSERT run inside a per-uid locked transaction
  (``pg_advisory_xact_lock`` on Postgres; per-uid ``threading.Lock`` in the
  memory repo), so concurrent earns can't both slip under the cap.

- This module imports nothing from the users domain (users.py imports from
  here) so the dependency direction stays one-way.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException

from .auth import get_current_uid
from .db import get_db_conn
from .vertical import get_vertical

# DEPRECATED as a source of truth: these are the built-in "garden"
# defaults (vertical.py GARDEN_DEFAULT) kept only so existing imports
# keep working. Consumers must read get_vertical().economy — never
# these constants — or a non-garden vertical silently gets garden tuning.
STARTER_CREDITS = 3

# Anti-gaming: max credits of new issuance per rolling window (transfers
# between users are exempt — see TRANSFER_REASONS).
# DEPRECATED as a source of truth (garden defaults, import compat only):
# consumers must read get_vertical().economy.earn_cap_* — see the note
# on STARTER_CREDITS above.
EARN_CAP_PER_7D = 10
EARN_CAP_WINDOW_MS = 7 * 24 * 60 * 60 * 1000

# Reasons that MOVE credits rather than mint them: the giver-side leg of a
# claim/exchange/slot, and refund legs that hand a user their own credits
# back. Exempt from the earn cap and never counted toward it.
TRANSFER_REASONS = frozenset({
    "claim_earn", "exchange_earn", "slot_earn",
    "claim_reversal", "dispute_reversal",
})

# Expiry warning windows (both visible on the credit-expiry endpoint).
WARNING_30D_MS = 30 * 24 * 60 * 60 * 1000
WARNING_7D_MS = 7 * 24 * 60 * 60 * 1000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_ms() -> int:
    return int(_utcnow().timestamp() * 1000)


# ---------------------------------------------------------------------------
# Seasonal expiry (pure helpers)
# ---------------------------------------------------------------------------

def season_end_ms(ts_ms: int) -> int:
    """End of the credit season containing ``ts_ms`` (UTC), as epoch ms.

    Seasons: Mar 1–Sep 30 and Oct 1–Feb 28/29. The returned instant is the
    last millisecond of the season — credits issued at ``ts_ms`` expire then.
    """
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    if 3 <= dt.month <= 9:
        end = datetime(dt.year, 9, 30, 23, 59, 59, 999000, tzinfo=timezone.utc)
    else:
        # Oct–Dec -> next February; Jan–Feb -> this February.
        year = dt.year + 1 if dt.month >= 10 else dt.year
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        last = 29 if leap else 28
        end = datetime(year, 2, last, 23, 59, 59, 999000, tzinfo=timezone.utc)
    return int(end.timestamp() * 1000)


def _entry_ms(entry: dict[str, Any]) -> int:
    """Ledger ``created_at`` -> epoch ms. Tolerates ISO strings, datetimes
    (naive assumed UTC), and raw epoch ms."""
    c = entry.get("created_at")
    if c is None:
        return 0
    if isinstance(c, (int, float)):
        return int(c)
    if isinstance(c, datetime):
        if c.tzinfo is None:
            c = c.replace(tzinfo=timezone.utc)
        return int(c.timestamp() * 1000)
    if isinstance(c, str):
        s = c.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return 0


def _remaining_lots(entries: list[dict[str, Any]], now_ms: int) -> list[dict[str, int]]:
    """Unexpired, unspent credit lots (FIFO) as ``{"credits", "expires_at_ms"}``.

    Positive deltas open lots expiring at the end of their issue season;
    negative deltas consume the oldest live lots first. Expired lots are
    simply dropped — the ledger rows are never touched (append-only).
    """
    lots: list[dict[str, int]] = []
    for e in sorted(entries, key=_entry_ms):  # stable: ties keep ledger order
        delta = int(e.get("delta", 0))
        if delta > 0:
            lots.append({"credits": delta,
                         "expires_at_ms": season_end_ms(_entry_ms(e))})
        elif delta < 0:
            need = -delta
            for lot in lots:
                if need <= 0:
                    break
                if lot["expires_at_ms"] <= now_ms:
                    continue  # expired lots are already gone; spends can't touch them
                take = min(lot["credits"], need)
                lot["credits"] -= take
                need -= take
            # Leftover spend beyond live lots is dropped: the balance gate at
            # claim/confirm time makes this unreachable in practice.
    return [lot for lot in lots
            if lot["credits"] > 0 and lot["expires_at_ms"] > now_ms]


def _tranche_totals(entries: list[dict[str, Any]], now_ms: int) -> dict[int, int]:
    """Live credits grouped by expiry instant: ``{expires_at_ms: credits}``."""
    totals: dict[int, int] = {}
    for lot in _remaining_lots(entries, now_ms):
        totals[lot["expires_at_ms"]] = totals.get(lot["expires_at_ms"], 0) + lot["credits"]
    return totals


def expiry_warnings(balance_entries: list[dict[str, Any]],
                    now_ms: int) -> dict[str, list[dict[str, int]]]:
    """Warning tranches for credits nearing expiry.

    Returns ``{"warn_30d": [...], "warn_7d": [...]}`` where each tranche is
    ``{"credits": int, "expiresAtMs": int}``, ordered by expiry. ``warn_7d``
    is the subset of ``warn_30d`` inside the 7-day window. Expired tranches
    are never listed — they are already gone.
    """
    totals = _tranche_totals(balance_entries, now_ms)

    def _within(window_ms: int) -> list[dict[str, int]]:
        horizon = now_ms + window_ms
        return [{"credits": c, "expiresAtMs": e}
                for e, c in sorted(totals.items()) if e <= horizon]

    return {"warn_30d": _within(WARNING_30D_MS), "warn_7d": _within(WARNING_7D_MS)}


# ---------------------------------------------------------------------------
# Anti-gaming earn cap
# ---------------------------------------------------------------------------

class EarnCapExceededError(HTTPException):
    """409: rolling 7-day earn cap hit. Raised by the issuance choke point;
    the app's HTTPException handler renders the standard error envelope."""

    def __init__(self, earned: int, cap: int | None = None):
        economy = get_vertical().economy
        if cap is None:
            cap = economy.earn_cap_amount
        days = economy.earn_cap_window_days
        super().__init__(
            status_code=409,
            detail={
                "code": "earn_cap_exceeded",
                "message": (
                    f"Earn cap reached: {earned}/{cap} "
                    f"{economy.credit_name}s earned in the last {days} days"
                ),
            },
        )


def _earned_in_window(entries: list[dict[str, Any]], now_ms: int,
                      window_ms: int | None = None) -> int:
    """Credits ISSUED inside the rolling window. The starter bootstrap,
    spends, and transfers between users do not count."""
    if window_ms is None:
        window_ms = get_vertical().economy.earn_cap_window_days * 86_400_000
    cutoff = now_ms - window_ms
    total = 0
    for e in entries:
        if e.get("reason") in ("starter", *TRANSFER_REASONS):
            continue
        delta = int(e.get("delta", 0))
        if delta > 0 and _entry_ms(e) > cutoff:
            total += delta
    return total


def _check_earn_cap(uid: str, delta: int, reason: str,
                    credit_repo: "CreditRepo", now_ms: int) -> None:
    """Enforce the rolling 7-day issuance cap. Called by every
    ``add_entry`` — the single choke point all earn paths flow through.
    Transfers and refunds are exempt: they conserve the credit supply,
    and capping them breaks the 1–100 price band (migration 0039)."""
    if delta <= 0 or reason in ("starter", *TRANSFER_REASONS):
        return
    earned = _earned_in_window(credit_repo.entries(uid), now_ms)
    if earned + delta > get_vertical().economy.earn_cap_amount:
        raise EarnCapExceededError(earned)


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------

class CreditRepo(Protocol):
    def add_entry(self, uid: str, delta: int, reason: str,
                  ref_id: str | None = None,
                  idempotency_key: str | None = None) -> dict[str, Any]:
        """Append a ledger entry. Idempotent on idempotency_key: repeats
        return the existing entry instead of double-posting. Positive
        issuance is subject to the rolling 7-day earn cap (409)."""
        ...
    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None: ...
    def balance(self, uid: str) -> int:
        """Effective (spendable) balance: derived SUM excluding expired lots."""
        ...
    def entries(self, uid: str) -> list[dict[str, Any]]: ...
    def add_confirmation(self, listing_id: str, uid: str) -> bool:
        """Record an exchange confirmation. Returns True when newly added."""
        ...
    def confirmations(self, listing_id: str) -> list[str]: ...


class PostgresCreditRepo:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        c = d.get("created_at")
        d["created_at"] = c.isoformat() if hasattr(c, "isoformat") else c
        return d

    def add_entry(self, uid, delta, reason, ref_id=None, idempotency_key=None):
        # Idempotent replay first: a repeat is not new issuance, so it must
        # not be cap-checked.
        if idempotency_key:
            existing = self.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
        # H12: serialize cap-check + insert per uid. pg_advisory_xact_lock is
        # transaction-scoped — it releases automatically at the commit below,
        # so a crashed request can't leave the lock held.
        self._conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"credits:{uid}",)
        )
        _check_earn_cap(uid, delta, reason, self, _now_ms())
        # Concurrent same-key inserts: exactly one wins; the loser re-reads.
        row = self._conn.execute(
            "INSERT INTO credit_ledger (id, uid, delta, reason, ref_id, idempotency_key) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
            (str(uuid.uuid4()), uid, delta, reason, ref_id, idempotency_key),
        ).fetchone()
        self._conn.commit()
        if row is None and idempotency_key:
            row = self._conn.execute(
                "SELECT * FROM credit_ledger WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
        return self._row(row) if row else None

    def find_by_idempotency_key(self, key):
        row = self._conn.execute(
            "SELECT * FROM credit_ledger WHERE idempotency_key = %s", (key,)
        ).fetchone()
        return self._row(row) if row else None

    def balance(self, uid):
        now_ms = _now_ms()
        return sum(lot["credits"] for lot in _remaining_lots(self.entries(uid), now_ms))

    def entries(self, uid):
        rows = self._conn.execute(
            "SELECT * FROM credit_ledger WHERE uid = %s ORDER BY created_at", (uid,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def add_confirmation(self, listing_id, uid):
        cur = self._conn.execute(
            "INSERT INTO exchange_confirmations (listing_id, uid) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (listing_id, uid),
        )
        self._conn.commit()
        return (cur.rowcount or 0) > 0

    def confirmations(self, listing_id):
        rows = self._conn.execute(
            "SELECT uid FROM exchange_confirmations WHERE listing_id = %s", (listing_id,)
        ).fetchall()
        return [r["uid"] for r in rows]


class MemoryCreditRepo:
    def __init__(self):
        self._entries: list[dict[str, Any]] = []
        self._by_key: dict[str, dict[str, Any]] = {}
        self._confirmations: dict[str, set[str]] = {}
        # H12: per-uid locks — the memory equivalent of the Postgres
        # advisory-lock serialization around cap-check + insert.
        self._uid_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, uid: str) -> threading.Lock:
        with self._locks_guard:
            return self._uid_locks.setdefault(uid, threading.Lock())

    def add_entry(self, uid, delta, reason, ref_id=None, idempotency_key=None):
        # The idempotency check lives INSIDE the lock: two threads racing the
        # same key must not both slip past the check and double-insert.
        with self._lock_for(uid):
            if idempotency_key and idempotency_key in self._by_key:
                return dict(self._by_key[idempotency_key])
            _check_earn_cap(uid, delta, reason, self, _now_ms())
            row = {
                "id": str(uuid.uuid4()), "uid": uid, "delta": delta, "reason": reason,
                "ref_id": ref_id, "idempotency_key": idempotency_key,
                "created_at": _utcnow().isoformat(),
            }
            self._entries.append(row)
            if idempotency_key:
                self._by_key[idempotency_key] = row
        return dict(row)

    def find_by_idempotency_key(self, key):
        row = self._by_key.get(key)
        return dict(row) if row else None

    def balance(self, uid):
        now_ms = _now_ms()
        return sum(lot["credits"] for lot in _remaining_lots(self.entries(uid), now_ms))

    def entries(self, uid):
        return [dict(e) for e in self._entries if e["uid"] == uid]

    def add_confirmation(self, listing_id, uid):
        s = self._confirmations.setdefault(listing_id, set())
        if uid in s:
            return False
        s.add(uid)
        return True

    def confirmations(self, listing_id):
        return sorted(self._confirmations.get(listing_id, set()))


def get_credit_repo(conn=Depends(get_db_conn)) -> CreditRepo:
    return PostgresCreditRepo(conn)


def ensure_starter_credits(uid: str, credit_repo: CreditRepo) -> None:
    """Grant the 3-credit bootstrap once per user. Idempotent — safe to call
    from every user-creation path (profile upsert, phone verify).

    H12: the grant carries the deterministic idempotency key
    ``f"starter:{uid}"``, so concurrent calls collapse to one row via
    ``ON CONFLICT DO NOTHING`` (Postgres) / key dedup (memory). Migration
    0028 adds a unique partial index on ``(uid) WHERE reason='starter'`` as
    defense-in-depth behind the key.
    """
    if not any(e["reason"] == "starter" for e in credit_repo.entries(uid)):
        credit_repo.add_entry(
            uid, get_vertical().economy.starter_credits, "starter", ref_id=uid,
            idempotency_key=f"starter:{uid}",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/v1/users", tags=["credits"])


@router.get("/me/credit-expiry")
def credit_expiry(
    uid: str = Depends(get_current_uid),
    credit_repo: CreditRepo = Depends(get_credit_repo),
) -> dict[str, Any]:
    """Credit expiry outlook: effective balance, tranches expiring within the
    next 30 days (covers both the 30-day and 7-day warning windows), and the
    end of the current season."""
    now_ms = _now_ms()
    warnings = expiry_warnings(credit_repo.entries(uid), now_ms)
    return {
        "balance": credit_repo.balance(uid),
        "expiring": warnings["warn_30d"],
        "seasonEndMs": season_end_ms(now_ms),
    }

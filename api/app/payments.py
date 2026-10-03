"""Stripe Connect seam + sitting-intent endpoint (API-071).

``POST /v1/payments/sitting-intent``: the plant owner of an *accepted* sitting
request asks for a payment hold. The endpoint quotes the booking (the customer
is charged the subtotal; the 18% platform fee is deducted from the sitter's
payout), creates a PaymentIntent through the configured
:class:`PaymentGateway`, persists the intent record, and returns the client
secret the mobile client confirms with the Stripe SDK.

STATUS TODAY: stub only. No ``stripe`` import, no network calls. The response
is deliberately shaped so it can never be mistaken for a real Stripe object:
``clientSecret`` starts with ``pi_stub_`` and ``livemode`` is always ``false``.

GOING LIVE — everything needed, in one place:

1. ``STRIPE_SECRET_KEY`` (new env var, server-side only — never ship it to the
   client): the platform's Stripe secret key (``sk_test_...`` / ``sk_live_...``).
   Used by :class:`StripeConnectGateway` to call the Stripe API.
2. Connected account id (destination charge): the *sitter's* Stripe Express
   account id (``acct_...``), one per sitter. It is NOT an env var — it must be
   stored per sitter (planned: ``sitter_profiles.stripe_account_id``, migration
   owned by the sitter track) and passed as ``transfer_data.destination`` so
   the charge lands on the sitter's connected account with the platform fee
   taken as ``application_fee_amount``.
3. ``STRIPE_WEBHOOK_SECRET`` (new env var): the ``whsec_...`` signing secret for
   the future ``POST /v1/payments/webhook`` endpoint, which will verify the
   ``Stripe-Signature`` header before trusting payment events.
4. Frontend publishable key: ``pk_test_...`` / ``pk_live_...`` is NOT a backend
   setting — it is baked into the Android build (BuildConfig / local.properties)
   and used with stripe-android's PaymentSheet to confirm the PaymentIntent
   with the ``clientSecret`` returned here. The backend never needs it.
5. ``PAYMENT_PROVIDER``: ``"stub"`` (default) or ``"stripe"``. Flip to
   ``"stripe"`` once :class:`StripeConnectGateway` is implemented; until then
   requests fail closed with 501 ``payments_not_configured``.

WHERE THE REAL IMPLEMENTATION PLUGS IN: implement
``StripeConnectGateway.create_payment_intent`` in this module (skeleton below)
with ``stripe.PaymentIntent.create(amount=..., currency="usd",
application_fee_amount=fee_cents, transfer_data={"destination": <sitter acct>},
idempotency_key=f"sitting-intent:{booking_id}")`` using ``STRIPE_SECRET_KEY``,
then set ``PAYMENT_PROVIDER=stripe``. No endpoint changes are needed — the
endpoint only talks to the ``PaymentGateway`` protocol.

Fee math: platform fee is 18% of the booking subtotal, rounded half-up to the
cent (Decimal — Python's banker's ``round()`` is deliberately NOT used for
money). The fee is borne by the sitter: the customer is charged the subtotal
only, and the sitter's payout is subtotal − fee. ``quote_booking`` reads
``subtotal_cents`` off the booking mapping; sitting_requests carry no price
yet (per-visit pricing lands with the sitter track), so it defaults to 0 until
a priced booking is passed in.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import get_current_uid
from .config import get_settings
from .db import get_db_conn
from .sitter import SitterRepo, get_sitter_repo
from .users import UserRepo, get_user_repo
from .vertical import get_vertical

router = APIRouter(prefix="/v1", tags=["payments"])

# DEPRECATED as a source of truth: the built-in "garden" default, kept
# only for import compatibility. Consumers must read
# get_vertical().fees.sitter_platform_fee_pct at quote time — never
# this constant — or a non-garden vertical silently gets garden's 18%.
PLATFORM_FEE_RATE = Decimal("0.18")

# Statuses of a sitting request for which the owner may create a payment hold.
# sitter.py lifecycle: requested -> accepted | declined, accepted -> completed |
# cancelled. "accepted" is the confirm step the backlog (API-070) puts the
# payment hold on; capture on completion is a later endpoint.
PAYABLE_STATUSES = frozenset({"accepted"})


# ---------------------------------------------------------------------------
# Pure fee math
# ---------------------------------------------------------------------------

def quote_booking(booking: dict[str, Any]) -> dict[str, int]:
    """Quote a sitting booking: the customer pays the subtotal; the 18%
    platform fee is deducted from the sitter's payout.

    ``booking`` is any mapping carrying ``subtotal_cents`` (the sitter's quoted
    price in cents; defaults to 0 when the booking is not priced yet).
    Raises ``ValueError`` on a negative or non-integer subtotal.
    """
    subtotal = booking.get("subtotal_cents", 0)
    if isinstance(subtotal, bool) or not isinstance(subtotal, int):
        raise ValueError(f"subtotal_cents must be an int, got {subtotal!r}")
    if subtotal < 0:
        raise ValueError(f"subtotal_cents must be >= 0, got {subtotal}")
    fee_rate = Decimal(str(get_vertical().fees.sitter_platform_fee_pct)) / 100
    fee = (Decimal(subtotal) * fee_rate).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP)
    fee_cents = int(fee)
    return {
        "subtotal_cents": subtotal,
        "fee_cents": fee_cents,
        "customer_total_cents": subtotal,
        "sitter_payout_cents": subtotal - fee_cents,
    }


# ---------------------------------------------------------------------------
# Payment gateway seam
# ---------------------------------------------------------------------------

@runtime_checkable
class PaymentGateway(Protocol):
    """Stripe Connect seam. The endpoint only ever talks to this protocol."""

    def create_payment_intent(
        self, amount_cents: int, fee_cents: int, booking_id: str
    ) -> dict[str, str]:
        """Create a hold for the booking. Returns {"id", "client_secret"}."""
        ...


class StubPaymentGateway:
    """Dev/test gateway: no network, no stripe import.

    The ``pi_stub_`` prefix and ``livemode: false`` in the response make it
    unmistakable that this is not a real Stripe object.
    """

    def create_payment_intent(
        self, amount_cents: int, fee_cents: int, booking_id: str
    ) -> dict[str, str]:
        return {
            "id": f"pi_stub_{booking_id}",
            "client_secret": f"pi_stub_{booking_id}_{amount_cents}",
        }


class StripeConnectGateway:
    """Production Stripe Connect gateway — NOT YET IMPLEMENTED.

    Plug-in point: implement ``create_payment_intent`` with
    ``stripe.PaymentIntent.create`` (see module docstring), then set
    ``PAYMENT_PROVIDER=stripe``.
    """

    def create_payment_intent(
        self, amount_cents: int, fee_cents: int, booking_id: str
    ) -> dict[str, str]:
        raise NotImplementedError(
            "Stripe Connect gateway is not wired yet: set STRIPE_SECRET_KEY, "
            "implement StripeConnectGateway.create_payment_intent, and set "
            "PAYMENT_PROVIDER=stripe"
        )


def get_payment_gateway() -> PaymentGateway:
    """Gateway factory: "stub" (default) or "stripe" (fails closed until wired)."""
    if get_settings().payment_provider == "stripe":
        return StripeConnectGateway()
    return StubPaymentGateway()


# ---------------------------------------------------------------------------
# Intent persistence
# ---------------------------------------------------------------------------

class PaymentRepo(Protocol):
    def create_intent(self, row: dict[str, Any]) -> dict[str, Any]: ...
    def get_intent(self, intent_id: str) -> dict[str, Any] | None: ...
    def get_by_booking_id(self, booking_id: str) -> dict[str, Any] | None: ...


def _serialize_intent(row: dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    c = d.get("created_at")
    d["created_at"] = c.isoformat() if hasattr(c, "isoformat") else c
    return d


class PostgresPaymentRepo:
    def __init__(self, conn):
        self._conn = conn

    def create_intent(self, row):
        """Idempotent per booking (M8): two concurrent creates race past the
        read-then-write check; the loser lands on ON CONFLICT DO NOTHING and
        re-reads the winner's row instead of 500ing on the unique constraint."""
        inserted = self._conn.execute(
            """INSERT INTO payment_intents
               (id, booking_id, amount_cents, fee_cents, client_secret, status)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (booking_id) DO NOTHING
               RETURNING id""",
            (row["id"], row["booking_id"], row["amount_cents"],
             row["fee_cents"], row["client_secret"], row.get("status", "created")),
        ).fetchone()
        self._conn.commit()
        if inserted is None:
            return self.get_by_booking_id(row["booking_id"])
        return self.get_intent(row["id"])

    def get_intent(self, intent_id):
        row = self._conn.execute(
            "SELECT * FROM payment_intents WHERE id = %s", (intent_id,)).fetchone()
        return _serialize_intent(row) if row else None

    def get_by_booking_id(self, booking_id):
        row = self._conn.execute(
            "SELECT * FROM payment_intents WHERE booking_id = %s",
            (booking_id,)).fetchone()
        return _serialize_intent(row) if row else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryPaymentRepo:
    def __init__(self):
        self._intents: dict[str, dict[str, Any]] = {}
        self._by_booking: dict[str, str] = {}

    def create_intent(self, row):
        # Mirror the Postgres ON CONFLICT DO NOTHING semantics: one intent
        # per booking, re-read on a race.
        if row["booking_id"] in self._by_booking:
            return self.get_by_booking_id(row["booking_id"])
        rec = {
            "id": row["id"],
            "booking_id": row["booking_id"],
            "amount_cents": row["amount_cents"],
            "fee_cents": row["fee_cents"],
            "client_secret": row["client_secret"],
            "status": row.get("status", "created"),
            "created_at": _utcnow().isoformat(),
        }
        self._intents[rec["id"]] = rec
        self._by_booking[rec["booking_id"]] = rec["id"]
        return dict(rec)

    def get_intent(self, intent_id):
        row = self._intents.get(intent_id)
        return dict(row) if row else None

    def get_by_booking_id(self, booking_id):
        intent_id = self._by_booking.get(booking_id)
        return self.get_intent(intent_id) if intent_id else None


def get_payment_repo(conn=Depends(get_db_conn)) -> PaymentRepo:
    return PostgresPaymentRepo(conn)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

class SittingIntentIn(BaseModel):
    bookingId: str = Field(min_length=1)


def _not_found() -> HTTPException:
    return HTTPException(404, {"code": "booking_not_found",
                               "message": "No such sitting booking"})


@router.post("/payments/sitting-intent", tags=["payments"])
def create_sitting_intent(
    data: SittingIntentIn,
    uid: str = Depends(get_current_uid),
    sitter_repo: SitterRepo = Depends(get_sitter_repo),
    payment_repo: PaymentRepo = Depends(get_payment_repo),
    user_repo: UserRepo = Depends(get_user_repo),
    gateway: PaymentGateway = Depends(get_payment_gateway),
) -> dict[str, Any]:
    """Create a payment hold for an accepted sitting booking.

    The caller must be the booking owner; the booking must be in a payable
    state (``accepted``). Idempotent per booking: a retry returns the existing
    intent's client secret instead of creating a second hold.

    Fee semantics: the customer is charged the subtotal only
    (``customer_total_cents``); the 18% fee is deducted from the sitter's
    payout and recorded as the sitter-borne fee.
    """
    booking = sitter_repo.get_request(data.bookingId)
    if booking is None:
        raise _not_found()
    if booking["owner_uid"] != uid:
        raise HTTPException(403, {"code": "not_the_booking_owner",
                                  "message": "Only the booking owner can pay"})
    # M20a: PRD requires IDV for paid sitting bookings — the person creating
    # the hold must be identity-verified.
    owner = user_repo.get(uid)
    if owner is None or owner.get("idv_status", "unverified") != "verified":
        raise HTTPException(403, {"code": "idv_not_verified",
                                  "message": "Paid sitting bookings require identity "
                                             "verification; complete IDV first"})
    status = booking.get("status")
    if status not in PAYABLE_STATUSES:
        raise HTTPException(409, {"code": "booking_not_payable",
                                  "message": f"A '{status}' booking cannot be "
                                             "paid; only accepted bookings can"})

    try:
        quote = quote_booking(booking)
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_booking_amount",
                                  "message": str(exc)})

    existing = payment_repo.get_by_booking_id(data.bookingId)
    if existing is not None:
        return _serialize_response(existing)

    try:
        pi = gateway.create_payment_intent(
            amount_cents=quote["customer_total_cents"],
            fee_cents=quote["fee_cents"],
            booking_id=data.bookingId,
        )
    except NotImplementedError as exc:
        raise HTTPException(501, {"code": "payments_not_configured",
                                  "message": str(exc)})

    record = payment_repo.create_intent({
        "id": pi["id"],
        "booking_id": data.bookingId,
        # The hold charges the customer the subtotal only; the fee is the
        # sitter-borne cut of the payout.
        "amount_cents": quote["customer_total_cents"],
        "fee_cents": quote["fee_cents"],
        "client_secret": pi["client_secret"],
    })
    return _serialize_response(record)


def _serialize_response(record: dict[str, Any]) -> dict[str, Any]:
    # livemode:false + pi_stub_ prefix: never a real Stripe object.
    return {
        "clientSecret": record["client_secret"],
        "amountCents": record["amount_cents"],
        "feeCents": record["fee_cents"],
        "livemode": False,
    }

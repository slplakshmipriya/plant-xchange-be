"""API-071: payments track — fee math, Stripe seam, sitting-intent endpoint."""
from __future__ import annotations

import pytest

from app import payments as payments_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mem_payments(client, monkeypatch):
    """client + in-memory users/sitters/requests/payment repos.

    Tokens: good-token -> alice, bob-token -> bob, mallory-token -> mallory.
    Returns (client, user_repo, sitter_repo, payment_repo).
    """
    from app import sitter as sitter_mod
    from app import users as users_mod
    from conftest import wire_credit_repo
    import app.auth as auth_mod

    urepo = users_mod.MemoryUserRepo()
    srepo = sitter_mod.MemorySitterRepo()
    prepo = payments_mod.MemoryPaymentRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[sitter_mod.get_sitter_repo] = lambda: srepo
    client.app.dependency_overrides[payments_mod.get_payment_repo] = lambda: prepo
    wire_credit_repo(client)

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice"}
        if token == "bob-token":
            return {"uid": "bob"}
        if token == "mallory-token":
            return {"uid": "mallory"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    return client, urepo, srepo, prepo


ALICE = {"Authorization": "Bearer good-token"}
BOB = {"Authorization": "Bearer bob-token"}
MALLORY = {"Authorization": "Bearer mallory-token"}


def _setup_booking(client, urepo, srepo, price_cents: int | None = 10000,
                   idv_verified: bool = True):
    """alice (owner) + bob (sitter) + accepted sitting request. Returns request.

    Paid sitting bookings require IDV (M20a): alice is verified by default;
    pass ``idv_verified=False`` to exercise the 403 path.
    """
    from datetime import date, timedelta
    days = [(date.today() + timedelta(days=10 + i)).isoformat()
            for i in range(6)]
    for headers, name in ((ALICE, "Alice"), (BOB, "Bob")):
        r = client.post("/v1/users", json={"display_name": name, "age_attestation": True}, headers=headers)
        assert r.status_code == 200, r.text
    if idv_verified:
        urepo.set_idv_status("alice", "verified")
    r = client.put("/v1/sitters/me", json={"bio": "tomato whisperer"}, headers=BOB)
    assert r.status_code == 200, r.text
    r = client.put("/v1/sitters/me/availability",
                   json={"available_dates": days}, headers=BOB)
    assert r.status_code == 200, r.text
    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": "bob", "plant_count": 4,
        "dates": days[:3], "services": []}, headers=ALICE)
    assert r.status_code == 201, r.text
    req = r.json()
    if price_cents is not None:
        # sitting_requests carry no price yet (pricing lands with the sitter
        # track); seed the quote the payments seam reads.
        srepo._requests[req["id"]]["subtotal_cents"] = price_cents
    r = client.post(f"/v1/sitting-requests/{req['id']}/accept", headers=BOB)
    assert r.json()["status"] == "accepted"
    return r.json()


# ---------------------------------------------------------------------------
# Pure fee math
# ---------------------------------------------------------------------------

def test_quote_exact():
    # The customer is charged the subtotal only; the 18% fee is deducted
    # from the sitter's payout.
    assert payments_mod.quote_booking({"subtotal_cents": 10000}) == {
        "subtotal_cents": 10000, "fee_cents": 1800,
        "customer_total_cents": 10000, "sitter_payout_cents": 8200}


def test_quote_zero_and_missing():
    assert payments_mod.quote_booking({"subtotal_cents": 0}) == {
        "subtotal_cents": 0, "fee_cents": 0,
        "customer_total_cents": 0, "sitter_payout_cents": 0}
    assert payments_mod.quote_booking({})["customer_total_cents"] == 0


def test_quote_rounds_half_up_not_bankers():
    # 25 * 0.18 = 4.5 -> half-up gives 5; Python's banker's round() gives 4.
    assert payments_mod.quote_booking({"subtotal_cents": 25})["fee_cents"] == 5


def test_quote_rounding_edges():
    assert payments_mod.quote_booking({"subtotal_cents": 1})["fee_cents"] == 0   # 0.18
    assert payments_mod.quote_booking({"subtotal_cents": 3})["fee_cents"] == 1   # 0.54
    assert payments_mod.quote_booking({"subtotal_cents": 167})["fee_cents"] == 30  # 30.06
    # 199 * 0.18 = 35.82 exactly; float math gives 35.81999... -> Decimal wins.
    assert payments_mod.quote_booking({"subtotal_cents": 199})["fee_cents"] == 36
    assert payments_mod.quote_booking({"subtotal_cents": 99999999}) == {
        "subtotal_cents": 99999999, "fee_cents": 18000000,
        "customer_total_cents": 99999999, "sitter_payout_cents": 81999999}


def test_quote_customer_total_is_subtotal():
    # The fee flip: customer_total == subtotal, sitter_payout == subtotal - fee.
    for subtotal in (0, 1, 25, 100, 10000, 99999999):
        q = payments_mod.quote_booking({"subtotal_cents": subtotal})
        assert q["customer_total_cents"] == subtotal
        assert q["sitter_payout_cents"] == subtotal - q["fee_cents"]
        assert "total_cents" not in q  # old customer-subtotal+fee semantics gone


@pytest.mark.parametrize("bad", [-1, -100, "100", None, 10.5, True])
def test_quote_rejects_bad_subtotal(bad):
    with pytest.raises(ValueError):
        payments_mod.quote_booking({"subtotal_cents": bad})


# ---------------------------------------------------------------------------
# Gateway seam
# ---------------------------------------------------------------------------

def test_stub_gateway_satisfies_protocol():
    gw = payments_mod.StubPaymentGateway()
    assert isinstance(gw, payments_mod.PaymentGateway)
    pi = gw.create_payment_intent(amount_cents=11800, fee_cents=1800,
                                  booking_id="b123")
    assert pi["client_secret"] == "pi_stub_b123_11800"
    assert pi["id"].startswith("pi_stub_")


def test_default_gateway_is_stub():
    assert isinstance(payments_mod.get_payment_gateway(),
                      payments_mod.StubPaymentGateway)


def test_stripe_gateway_skeleton_fails_closed():
    with pytest.raises(NotImplementedError):
        payments_mod.StripeConnectGateway().create_payment_intent(
            amount_cents=1, fee_cents=0, booking_id="b")


# ---------------------------------------------------------------------------
# Endpoint validation
# ---------------------------------------------------------------------------

def test_intent_404_unknown_booking(mem_payments):
    client, *_ = mem_payments
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": "no-such-booking"}, headers=ALICE)
    assert r.status_code == 404
    assert r.json()["code"] == "booking_not_found"


def test_intent_401_unauthenticated(mem_payments):
    client, *_ = mem_payments
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": "x"},
                    headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401


def test_intent_403_idv_not_verified(mem_payments):
    # M20a: PRD requires IDV for paid sitting bookings.
    client, urepo, srepo, _ = mem_payments
    req = _setup_booking(client, urepo, srepo, idv_verified=False)
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": req["id"]}, headers=ALICE)
    assert r.status_code == 403
    assert r.json()["code"] == "idv_not_verified"


def test_intent_403_idv_verified_owner_passes(mem_payments):
    client, urepo, srepo, _ = mem_payments
    req = _setup_booking(client, urepo, srepo)  # alice verified by default
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": req["id"]}, headers=ALICE)
    assert r.status_code == 200, r.text


def test_intent_403_not_owner(mem_payments):
    client, urepo, srepo, _ = mem_payments
    req = _setup_booking(client, urepo, srepo)
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": req["id"]}, headers=MALLORY)
    assert r.status_code == 403
    assert r.json()["code"] == "not_the_booking_owner"


@pytest.mark.parametrize("end_state", ["requested", "declined", "completed", "cancelled"])
def test_intent_409_not_payable(mem_payments, end_state):
    client, urepo, srepo, _ = mem_payments
    for headers, name in ((ALICE, "Alice"), (BOB, "Bob")):
        assert client.post("/v1/users", json={"display_name": name, "age_attestation": True},
                           headers=headers).status_code == 200
    urepo.set_idv_status("alice", "verified")  # M20a gate: verify before 409 check
    assert client.put("/v1/sitters/me", json={}, headers=BOB).status_code == 200
    from datetime import date, timedelta
    days = [(date.today() + timedelta(days=10 + i)).isoformat()
            for i in range(6)]
    r = client.put("/v1/sitters/me/availability",
                   json={"available_dates": days}, headers=BOB)
    assert r.status_code == 200, r.text
    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": "bob", "plant_count": 2,
        "dates": days[:3], "services": []}, headers=ALICE)
    req = r.json()
    if end_state in ("declined",):
        client.post(f"/v1/sitting-requests/{req['id']}/decline", headers=BOB)
    elif end_state in ("completed",):
        client.post(f"/v1/sitting-requests/{req['id']}/accept", headers=BOB)
        client.post(f"/v1/sitting-requests/{req['id']}/complete", headers=ALICE)
    elif end_state in ("cancelled",):
        client.post(f"/v1/sitting-requests/{req['id']}/cancel", headers=ALICE)
    # "requested": leave untouched.

    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": req["id"]}, headers=ALICE)
    assert r.status_code == 409
    assert r.json()["code"] == "booking_not_payable"


# ---------------------------------------------------------------------------
# Happy path + persistence + idempotency
# ---------------------------------------------------------------------------

def test_sitting_intent_stub_shape(mem_payments):
    client, urepo, srepo, prepo = mem_payments
    req = _setup_booking(client, urepo, srepo, price_cents=10000)

    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": req["id"]}, headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    # Fee flip: the hold charges the customer the subtotal only (10000),
    # with the 18% fee recorded as the sitter-borne fee.
    assert body["clientSecret"] == f"pi_stub_{req['id']}_10000"
    assert body["clientSecret"].startswith("pi_stub_")
    assert body["amountCents"] == 10000
    assert body["feeCents"] == 1800
    assert body["livemode"] is False

    rec = prepo.get_by_booking_id(req["id"])
    assert rec is not None
    assert rec["client_secret"] == body["clientSecret"]
    assert rec["amount_cents"] == 10000   # customer total == subtotal
    assert rec["fee_cents"] == 1800      # sitter-borne fee
    assert rec["status"] == "created"
    assert rec["created_at"]


def test_sitting_intent_records_sitter_borne_fee(mem_payments):
    # End-to-end fee semantics: customer_total == subtotal, sitter payout ==
    # subtotal - fee, intent holds the customer total.
    client, urepo, srepo, prepo = mem_payments
    req = _setup_booking(client, urepo, srepo, price_cents=5000)
    quote = payments_mod.quote_booking({"subtotal_cents": 5000})
    assert quote["customer_total_cents"] == 5000
    assert quote["sitter_payout_cents"] == 5000 - quote["fee_cents"]

    body = client.post("/v1/payments/sitting-intent",
                       json={"bookingId": req["id"]}, headers=ALICE).json()
    assert body["amountCents"] == quote["customer_total_cents"]
    assert body["feeCents"] == quote["fee_cents"]
    rec = prepo.get_by_booking_id(req["id"])
    assert rec["amount_cents"] == 5000
    assert rec["fee_cents"] == quote["fee_cents"]


def test_sitting_intent_idempotent_per_booking(mem_payments):
    client, urepo, srepo, prepo = mem_payments
    req = _setup_booking(client, urepo, srepo, price_cents=5000)

    first = client.post("/v1/payments/sitting-intent",
                        json={"bookingId": req["id"]}, headers=ALICE).json()
    second = client.post("/v1/payments/sitting-intent",
                         json={"bookingId": req["id"]}, headers=ALICE).json()
    assert first == second
    assert first["clientSecret"] == f"pi_stub_{req['id']}_5000"
    assert len(prepo._intents) == 1


def test_sitting_intent_unpriced_booking_quotes_zero(mem_payments):
    client, urepo, srepo, _ = mem_payments
    req = _setup_booking(client, urepo, srepo, price_cents=None)
    body = client.post("/v1/payments/sitting-intent",
                       json={"bookingId": req["id"]}, headers=ALICE).json()
    assert body["amountCents"] == 0
    assert body["feeCents"] == 0
    assert body["livemode"] is False


def test_stripe_provider_fails_closed_501(mem_payments, monkeypatch):
    client, urepo, srepo, _ = mem_payments
    req = _setup_booking(client, urepo, srepo)
    monkeypatch.setenv("PAYMENT_PROVIDER", "stripe")
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": req["id"]}, headers=ALICE)
    assert r.status_code == 501
    assert r.json()["code"] == "payments_not_configured"


# ---------------------------------------------------------------------------
# Memory repo unit tests
# ---------------------------------------------------------------------------

def test_memory_payment_repo_roundtrip():
    repo = payments_mod.MemoryPaymentRepo()
    assert repo.get_by_booking_id("b1") is None
    rec = repo.create_intent({
        "id": "pi_stub_b1", "booking_id": "b1", "amount_cents": 118,
        "fee_cents": 18, "client_secret": "pi_stub_b1_118"})
    assert repo.get_intent("pi_stub_b1") == rec
    assert repo.get_by_booking_id("b1") == rec
    assert repo.get_intent("missing") is None
    # M8: a second create for the same booking returns the existing row
    # (Postgres path: ON CONFLICT DO NOTHING + re-select).
    again = repo.create_intent({
        "id": "pi_stub_b1", "booking_id": "b1", "amount_cents": 999,
        "fee_cents": 1, "client_secret": "pi_stub_b1_999"})
    assert again == rec

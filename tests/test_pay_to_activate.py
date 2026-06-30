"""Tests for the pay-to-activate gate.

Covers:
  * core.solana_pay.verify_usdc_payment — valid / wrong-amount / wrong-recipient
    / malformed-signature / failed / too-old / no-address (all fail CLOSED).
  * replay protection (used_payments).
  * /paid handler — activates on success, rejects reused tx, rejects bad arg,
    does not activate on verify failure.
  * entitlement gate — paid runs, unpaid blocked, first /scan free then blocked,
    expired paid_until re-gates; correct handlers are/aren't decorated.

Run: pytest tests/test_pay_to_activate.py
"""
import sys
import time
import types
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import config
from storage import database as db
import core.solana_pay as solana_pay
import core.entitlements as ent

RECV = "Recipient1111111111111111111111111111111111"   # our receiving address (test)
SIG = "1" * 87                                          # well-formed base58-ish signature
PRICE_UNITS = 3_000_000                                 # $3.00 in USDC base units (6 dp)


# --------------------------- fixtures ---------------------------
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(config, "PAYMENT_RECEIVING_ADDRESS", RECV)
    monkeypatch.setattr(config, "PAYMENT_PRICE_USD", 3.00)
    monkeypatch.setattr(config, "PAYMENT_VALIDITY_DAYS", 3)
    monkeypatch.setattr(config, "SOLANA_RPC_URL", "http://mock")
    yield


# --------------------------- fake aiohttp ---------------------------
class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload, status=200):
        self._payload, self._status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None):
        return _FakeResp(self._payload, self._status)


def _patch_rpc(monkeypatch, payload, status=200):
    monkeypatch.setattr(
        solana_pay.aiohttp, "ClientSession",
        lambda *a, **k: _FakeSession(payload, status),
    )


def _result(post_units, *, owner=RECV, mint=None, decimals=6, err=None,
            block_time=None, pre_units=0):
    mint = solana_pay.USDC_MINT if mint is None else mint
    block_time = time.time() if block_time is None else block_time
    return {"result": {
        "blockTime": block_time,
        "meta": {
            "err": err,
            "preTokenBalances": [{
                "accountIndex": 1, "mint": mint, "owner": owner,
                "uiTokenAmount": {"amount": str(pre_units), "decimals": decimals},
            }],
            "postTokenBalances": [{
                "accountIndex": 1, "mint": mint, "owner": owner,
                "uiTokenAmount": {"amount": str(post_units), "decimals": decimals},
            }],
        },
    }}


# --------------------------- solana_pay unit tests ---------------------------
def test_valid_payment_ok(tmp_db, monkeypatch):
    _patch_rpc(monkeypatch, _result(PRICE_UNITS))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is True, out


def test_overpayment_ok(tmp_db, monkeypatch):
    _patch_rpc(monkeypatch, _result(PRICE_UNITS + 500_000))
    assert asyncio.run(solana_pay.verify_usdc_payment(SIG))["ok"] is True


def test_wrong_amount_rejected(tmp_db, monkeypatch):
    _patch_rpc(monkeypatch, _result(2_000_000))  # $2.00 < $3.00
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is False and out["reason"] == "amount_too_low"


def test_wrong_recipient_rejected(tmp_db, monkeypatch):
    _patch_rpc(monkeypatch, _result(PRICE_UNITS, owner="SomeoneElse2222222222222222222222222222222"))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is False and out["reason"] == "no_usdc_to_recipient"


def test_wrong_mint_rejected(tmp_db, monkeypatch):
    _patch_rpc(monkeypatch, _result(PRICE_UNITS, mint="NotUsdcMint00000000000000000000000000000000"))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is False and out["reason"] == "no_usdc_to_recipient"


def test_malformed_signature_rejected(tmp_db, monkeypatch):
    # Should never even hit the RPC.
    _patch_rpc(monkeypatch, _result(PRICE_UNITS))
    for bad in ["", "short", "has space!!", "0OIl" * 22]:
        out = asyncio.run(solana_pay.verify_usdc_payment(bad))
        assert out["ok"] is False and out["reason"] == "malformed_signature", bad


def test_failed_tx_rejected(tmp_db, monkeypatch):
    _patch_rpc(monkeypatch, _result(PRICE_UNITS, err={"InstructionError": [0, "x"]}))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is False and out["reason"] == "tx_failed"


def test_too_old_rejected(tmp_db, monkeypatch):
    old = time.time() - (config.PAYMENT_VALIDITY_DAYS * 86400 + 3600)
    _patch_rpc(monkeypatch, _result(PRICE_UNITS, block_time=old))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is False and out["reason"] == "tx_too_old"


def test_tx_not_found_rejected(tmp_db, monkeypatch):
    _patch_rpc(monkeypatch, {"result": None})
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is False and out["reason"] == "tx_not_found"


def test_rpc_error_fails_closed(tmp_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(solana_pay.aiohttp, "ClientSession", boom)
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is False and out["reason"] == "rpc_error"


def test_no_receiving_address_fails_closed(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_RECEIVING_ADDRESS", "")
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG))
    assert out["ok"] is False and out["reason"] == "receiving_address_not_configured"


# --------------------------- replay protection ---------------------------
def test_replay_protection(tmp_db):
    assert db.is_payment_used(SIG) is False
    db.mark_payment_used(SIG, 42)
    assert db.is_payment_used(SIG) is True
    db.mark_payment_used(SIG, 99)  # idempotent, no error
    assert db.is_payment_used(SIG) is True


def test_config_validate_requires_address(monkeypatch):
    # validate() reads config.settings module globals.
    import config.settings as settings
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setattr(settings, "PAYMENT_RECEIVING_ADDRESS", "")
    assert any("PAYMENT_RECEIVING_ADDRESS" in p for p in settings.validate())


# --------------------------- handler /paid + gate ---------------------------
class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, chat_id):
        self.effective_chat = types.SimpleNamespace(id=chat_id)
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []
        self.job_queue = None


def _stub_cycles(monkeypatch):
    """Avoid importing the heavy services.cycles in the /paid success path."""
    stub = types.ModuleType("services.cycles")
    stub.wallet_seed_job = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "services.cycles", stub)
    import services
    monkeypatch.setattr(services, "cycles", stub, raising=False)


def test_paid_activates(tmp_db, monkeypatch):
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    monkeypatch.setattr(h, "verify_usdc_payment",
                        lambda tx, expected_units=None: _async({"ok": True, "reason": "payment_verified"}))
    upd, ctx = FakeUpdate(7), FakeContext(args=[SIG])
    asyncio.run(h.paid_cmd(upd, ctx))
    assert db.is_payment_used(SIG) is True
    assert ent.is_paid(7) is True
    assert 7 in db.get_active_chats()
    assert any("verified" in r.lower() for r in upd.message.replies)


def test_paid_reused_rejected(tmp_db, monkeypatch):
    import bot.handlers as h
    db.mark_payment_used(SIG, 1)  # already redeemed by someone
    called = {"v": False}

    def _verify(tx, expected_units=None):
        called["v"] = True
        return _async({"ok": True})
    monkeypatch.setattr(h, "verify_usdc_payment", _verify)
    upd, ctx = FakeUpdate(7), FakeContext(args=[SIG])
    asyncio.run(h.paid_cmd(upd, ctx))
    assert called["v"] is False                      # short-circuited before RPC
    assert ent.is_paid(7) is False
    assert any("already" in r.lower() for r in upd.message.replies)


def test_paid_no_arg(tmp_db):
    import bot.handlers as h
    upd, ctx = FakeUpdate(7), FakeContext(args=[])
    asyncio.run(h.paid_cmd(upd, ctx))
    assert ent.is_paid(7) is False
    assert any("usage" in r.lower() for r in upd.message.replies)


def test_paid_verify_failure_no_activation(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(h, "verify_usdc_payment",
                        lambda tx, expected_units=None: _async({"ok": False, "reason": "amount_too_low"}))
    upd, ctx = FakeUpdate(7), FakeContext(args=[SIG])
    asyncio.run(h.paid_cmd(upd, ctx))
    assert ent.is_paid(7) is False
    assert db.is_payment_used(SIG) is False          # failed tx not burned
    assert 7 not in db.get_active_chats()


# ---- entitlement gate ----
def _make_gated(free_taste=False):
    ran = {"v": False}

    @ent.require_paid(free_taste=free_taste)
    async def handler(update, context):
        ran["v"] = True
        await update.message.reply_text("VALUE")
    return handler, ran


def test_gate_paid_runs(tmp_db):
    db.set_paid_until(5, (datetime.now(timezone.utc) + timedelta(days=3)).isoformat())
    handler, ran = _make_gated()
    asyncio.run(handler(FakeUpdate(5), FakeContext()))
    assert ran["v"] is True


def test_gate_unpaid_blocked(tmp_db):
    handler, ran = _make_gated()
    upd = FakeUpdate(5)
    asyncio.run(handler(upd, FakeContext()))
    assert ran["v"] is False
    assert any("pass" in r.lower() or "usdc" in r.lower() for r in upd.message.replies)


def test_first_scan_free_then_blocked(tmp_db):
    handler, ran = _make_gated(free_taste=True)
    # 1st call: free taste
    u1 = FakeUpdate(9)
    asyncio.run(handler(u1, FakeContext()))
    assert ran["v"] is True
    assert db.get_free_used(9) is True
    # 2nd call: blocked
    ran["v"] = False
    u2 = FakeUpdate(9)
    asyncio.run(handler(u2, FakeContext()))
    assert ran["v"] is False
    assert any("pass" in r.lower() or "usdc" in r.lower() for r in u2.message.replies)


def test_expired_paid_regates(tmp_db):
    db.set_paid_until(5, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert ent.is_paid(5) is False
    handler, ran = _make_gated()  # no free taste (not /scan)
    asyncio.run(handler(FakeUpdate(5), FakeContext()))
    assert ran["v"] is False


def test_owner_bypass(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    # Owner is always paid without any payment...
    assert ent.is_paid(777) is True
    assert db.get_paid_until(777) is None
    # ...and a free-taste handler never burns the owner's freebie.
    handler, ran = _make_gated(free_taste=True)
    asyncio.run(handler(FakeUpdate(777), FakeContext()))
    assert ran["v"] is True
    assert db.get_free_used(777) is False
    # A non-owner unpaid chat is still gated.
    assert ent.is_paid(778) is False


def test_owner_bypass_disabled_when_zero(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 0)
    assert ent.is_paid(0) is False        # default 0 must not grant access
    assert ent.is_paid(123) is False


def test_correct_handlers_gated(tmp_db):
    import bot.handlers as h
    gated = ["scan", "coin_cmd", "wallets_cmd", "confluence_cmd", "dexs_cmd", "scores_cmd"]
    free = ["start", "paid_cmd", "stop_cmd", "toggle_alerts", "status_cmd", "help_cmd"]
    for name in gated:
        assert hasattr(getattr(h, name), "__wrapped__"), f"{name} should be gated"
    for name in free:
        assert not hasattr(getattr(h, name), "__wrapped__"), f"{name} should be free"


def test_expired_paid_drops_from_active(tmp_db):
    db.activate_chat(3)
    db.set_paid_until(3, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert 3 not in db.get_active_chats()      # expired → not active
    assert 3 not in db.get_alert_chats()


# --------------------------- C1: payment bound to payer ---------------------------
def test_amount_matches_reference_ok(tmp_db, monkeypatch):
    """A tx whose USDC amount equals the chat's bound reference verifies."""
    _patch_rpc(monkeypatch, _result(3_020_000))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=3_020_000))
    assert out["ok"] is True, out


def test_amount_mismatch_rejected(tmp_db, monkeypatch):
    """A tx paying a DIFFERENT chat's reference (a full nonce-spacing away) is
    rejected — the binding still holds with the one-cent window."""
    _patch_rpc(monkeypatch, _result(3_040_000))   # another chat's slot (+0.02)
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=3_020_000))
    assert out["ok"] is False and out["reason"] == "amount_mismatch", out


def test_overpay_within_window_accepted(tmp_db, monkeypatch):
    """A slight over/round-up (under a cent) still lands in this chat's window."""
    _patch_rpc(monkeypatch, _result(3_020_000 + 5_000))   # +0.005 USDC
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=3_020_000))
    assert out["ok"] is True, out


def test_overpay_beyond_window_rejected(tmp_db, monkeypatch):
    """An over-pay of >= 0.01 USDC is rejected so it can't collide into the next
    chat's nonce slot (the window is half-open at reference + 0.01)."""
    _patch_rpc(monkeypatch, _result(3_020_000 + 10_000))   # exactly +0.01 USDC
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=3_020_000))
    assert out["ok"] is False and out["reason"] == "amount_mismatch", out


def test_below_reference_rejected(tmp_db, monkeypatch):
    """Anything below the reference is still rejected (under-payment)."""
    _patch_rpc(monkeypatch, _result(3_020_000 - 1))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=3_020_000))
    assert out["ok"] is False and out["reason"] == "amount_too_low", out


def test_window_smaller_than_nonce_spacing():
    """The accept window must be strictly smaller than the nonce spacing, so two
    chats' windows can never overlap."""
    from core.solana_pay import PAYMENT_AMOUNT_WINDOW_UNITS
    from storage.database import PAYMENT_REF_STEP_UNITS
    assert PAYMENT_AMOUNT_WINDOW_UNITS < PAYMENT_REF_STEP_UNITS


def test_payment_reference_is_unique_and_stable(tmp_db):
    a1 = db.assign_payment_reference(1)
    a2 = db.assign_payment_reference(1)   # stable for the same chat
    b = db.assign_payment_reference(2)
    assert a1 == a2
    assert a1 != b                        # unique across chats
    base = round(config.PAYMENT_PRICE_USD * 1_000_000)
    assert a1 > base
    assert (a1 - base) % db.PAYMENT_REF_STEP_UNITS == 0          # on the spacing grid
    assert abs(b - a1) >= db.PAYMENT_REF_STEP_UNITS              # spacing >= window
    assert db.get_payment_reference(1) == a1


def test_payment_bound_to_payer(tmp_db, monkeypatch):
    """A different chat cannot redeem another chat's payment (the C1 exploit)."""
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    a_units = db.assign_payment_reference(1)
    b_units = db.assign_payment_reference(2)
    assert a_units != b_units

    # The real on-chain tx paid chat A's unique amount; verify enforces the match.
    def fake_verify(tx, expected_units=None):
        received = a_units
        ok = expected_units is not None and received == expected_units
        return _async({"ok": ok,
                       "reason": "payment_verified" if ok else "amount_mismatch",
                       "received": received})
    monkeypatch.setattr(h, "verify_usdc_payment", fake_verify)

    # Attacker chat B submits chat A's signature -> rejected, tx not burned.
    ub = FakeUpdate(2)
    asyncio.run(h.paid_cmd(ub, FakeContext(args=[SIG])))
    assert ent.is_paid(2) is False
    assert db.is_payment_used(SIG) is False
    assert any("match" in r.lower() for r in ub.message.replies), ub.message.replies

    # Rightful chat A redeems its own tx -> granted.
    ua = FakeUpdate(1)
    asyncio.run(h.paid_cmd(ua, FakeContext(args=[SIG])))
    assert ent.is_paid(1) is True
    assert db.is_payment_used(SIG) is True


# --------------------------- H1/H2: /start rendering ---------------------------
def test_start_unpaid_shows_only_paywall(tmp_db):
    import bot.handlers as h
    upd = FakeUpdate(50)
    asyncio.run(h.start(upd, FakeContext()))
    body = " ".join(upd.message.replies).lower()
    assert "active pass" in body            # paywall present
    assert "you're active" not in body      # no active confirmation


def test_start_paid_shows_only_active(tmp_db):
    import bot.handlers as h
    db.set_paid_until(51, (datetime.now(timezone.utc) + timedelta(days=3)).isoformat())
    upd = FakeUpdate(51)
    asyncio.run(h.start(upd, FakeContext()))
    body = " ".join(upd.message.replies).lower()
    assert "you're active" in body
    assert "active pass" not in body        # paywall must NOT also render


def test_start_owner_no_expiry(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 999)
    upd = FakeUpdate(999)
    asyncio.run(h.start(upd, FakeContext()))
    body = " ".join(upd.message.replies)
    assert "operator" in body.lower()       # owner-specific copy
    assert "until —" not in body            # never the null dash
    assert "active pass" not in body.lower()


# --------------------------- M1: global seed flag ---------------------------
def test_stop_does_not_reset_seed_or_pause_others(tmp_db):
    import bot.handlers as h
    from services import cycles
    db.set_state("wallet_seeded", "1")
    fut = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    db.set_paid_until(1, fut)
    db.set_paid_until(2, fut)
    asyncio.run(h.stop_cmd(FakeUpdate(2), FakeContext()))   # chat 2 stops
    assert db.get_state("wallet_seeded") == "1"             # not reset
    assert cycles._should_run() is True                     # chat 1 still active


def test_paid_does_not_reset_seed(tmp_db, monkeypatch):
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    monkeypatch.setattr(h, "verify_usdc_payment",
                        lambda tx, expected_units=None: _async(
                            {"ok": True, "reason": "payment_verified", "received": expected_units}))
    db.set_state("wallet_seeded", "1")
    asyncio.run(h.paid_cmd(FakeUpdate(8), FakeContext(args=[SIG])))
    assert db.get_state("wallet_seeded") == "1"             # already seeded -> untouched
    assert ent.is_paid(8) is True


def test_seed_job_does_not_broadcast(tmp_db, monkeypatch):
    from services import cycles
    called = {"b": False}

    async def fake_cycle(*a, **k):
        return None

    async def fake_broadcast(*a, **k):
        called["b"] = True
        return True

    monkeypatch.setattr(cycles, "_wallet_cycle", fake_cycle)
    monkeypatch.setattr(cycles.tg, "broadcast", fake_broadcast)
    asyncio.run(cycles.wallet_seed_job(None))
    assert db.get_state("wallet_seeded") == "1"
    assert called["b"] is False             # no broadcast to unrelated chats


# --------------------------- helper ---------------------------
def _async(value):
    async def _coro():
        return value
    return _coro()

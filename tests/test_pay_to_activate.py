"""Tests for the pay-to-activate gate.

Covers:
  * core.solana_pay.verify_usdc_payment — valid / wrong-amount / wrong-recipient
    / malformed-signature / failed / too-old / no-address (all fail CLOSED).
  * replay protection (used_payments).
  * /paid handler — activates on success, rejects reused tx, rejects bad arg,
    does not activate on verify failure.
  * entitlement gate — paid runs, first gated use auto-starts the trial, blocked
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
PRICE_UNITS = 10_000_000                                # $10.00 (cheapest plan) in base units (6 dp)
# Chat #1's bound week amount: $10.00 + slot 1 * $0.02 = $10.02.
REF_UNITS = 10_020_000

_PLANS = {
    "week": {"label": "1 week", "price_usd": 10.00, "days": 7},
    "month": {"label": "1 month", "price_usd": 30.00, "days": 30},
}


# --------------------------- fixtures ---------------------------
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(config, "PAYMENT_RECEIVING_ADDRESS", RECV)
    monkeypatch.setattr(config, "PAYMENT_PLANS", {k: dict(v) for k, v in _PLANS.items()})
    monkeypatch.setattr(config, "PAYMENT_PLAN_ORDER", ["week", "month"])
    monkeypatch.setattr(config, "PAYMENT_TX_MAX_AGE_DAYS", 3)
    monkeypatch.setattr(config, "TRIAL_HOURS", 12)
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
    _patch_rpc(monkeypatch, _result(2_000_000))  # $2.00 < $10.00 floor
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
    old = time.time() - (config.PAYMENT_TX_MAX_AGE_DAYS * 86400 + 3600)
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


class FakeJobQueue:
    """Records run_once schedules so tests can assert the wallet seed is queued."""
    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when=None, **kw):
        self.jobs.append((callback, when))


class FakeContext:
    def __init__(self, args=None, job_queue=None):
        self.args = args or []
        self.job_queue = job_queue


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
    upd, ctx = FakeUpdate(7), FakeContext(args=["week", SIG])
    asyncio.run(h.paid_cmd(upd, ctx))
    assert db.is_payment_used(SIG) is True
    assert ent.is_paid(7) is True
    assert 7 in db.get_active_chats()
    assert any("verified" in r.lower() for r in upd.message.replies)


def test_paid_month_grants_30_days(tmp_db, monkeypatch):
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    monkeypatch.setattr(h, "verify_usdc_payment",
                        lambda tx, expected_units=None: _async({"ok": True, "reason": "payment_verified"}))
    asyncio.run(h.paid_cmd(FakeUpdate(7), FakeContext(args=["month", SIG])))
    assert ent.is_paid(7) is True
    until = datetime.fromisoformat(db.get_paid_until(7))
    remaining = until - datetime.now(timezone.utc)
    assert timedelta(days=29) < remaining <= timedelta(days=30)


def test_paid_requires_plan(tmp_db, monkeypatch):
    """A tx with no plan keyword is usage-rejected and never verified."""
    import bot.handlers as h
    called = {"v": False}

    def _verify(tx, expected_units=None):
        called["v"] = True
        return _async({"ok": True})
    monkeypatch.setattr(h, "verify_usdc_payment", _verify)
    upd = FakeUpdate(7)
    asyncio.run(h.paid_cmd(upd, FakeContext(args=[SIG])))   # tx but no plan
    assert called["v"] is False
    assert ent.is_paid(7) is False
    assert any("usage" in r.lower() for r in upd.message.replies)


def test_paid_refill_extends_not_resets(tmp_db, monkeypatch):
    """Buying a shorter plan while a longer one is live extends from the
    remaining time — it never shortens access."""
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    monkeypatch.setattr(h, "verify_usdc_payment",
                        lambda tx, expected_units=None: _async({"ok": True, "reason": "payment_verified"}))
    far = datetime.now(timezone.utc) + timedelta(days=20)
    db.set_paid_until(7, far.isoformat())
    asyncio.run(h.paid_cmd(FakeUpdate(7), FakeContext(args=["week", SIG])))
    until = datetime.fromisoformat(db.get_paid_until(7))
    assert until > far          # extended beyond the existing window, not reset to now+7d


def test_paid_reused_rejected(tmp_db, monkeypatch):
    import bot.handlers as h
    db.mark_payment_used(SIG, 1)  # already redeemed by someone
    called = {"v": False}

    def _verify(tx, expected_units=None):
        called["v"] = True
        return _async({"ok": True})
    monkeypatch.setattr(h, "verify_usdc_payment", _verify)
    upd, ctx = FakeUpdate(7), FakeContext(args=["week", SIG])
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
    upd, ctx = FakeUpdate(7), FakeContext(args=["week", SIG])
    asyncio.run(h.paid_cmd(upd, ctx))
    assert ent.is_paid(7) is False
    assert db.is_payment_used(SIG) is False          # failed tx not burned
    assert 7 not in db.get_active_chats()


# --------------------------- one-time free trial ---------------------------
def test_trial_grants_access_and_activates(tmp_db, monkeypatch):
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    upd = FakeUpdate(70)
    asyncio.run(h.trial_cmd(upd, FakeContext(job_queue=FakeJobQueue())))
    assert ent.is_paid(70) is True                 # full access granted
    assert db.get_trial_used(70) is True           # consumed
    assert 70 in db.get_alert_chats()              # proactive alerts flow
    until = datetime.fromisoformat(db.get_paid_until(70))
    remaining = until - datetime.now(timezone.utc)
    assert timedelta(hours=11) < remaining <= timedelta(hours=12)
    assert any("trial started" in r.lower() for r in upd.message.replies)


def test_trial_is_one_time_only(tmp_db, monkeypatch):
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    asyncio.run(h.trial_cmd(FakeUpdate(71), FakeContext()))
    # Simulate the 12h window elapsing.
    db.set_paid_until(71, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert ent.is_paid(71) is False
    upd2 = FakeUpdate(71)
    asyncio.run(h.trial_cmd(upd2, FakeContext()))
    assert ent.is_paid(71) is False                # not re-granted
    assert any("already used" in r.lower() for r in upd2.message.replies)


def test_trial_not_consumed_when_already_active(tmp_db, monkeypatch):
    import bot.handlers as h
    db.set_paid_until(72, (datetime.now(timezone.utc) + timedelta(days=5)).isoformat())
    upd = FakeUpdate(72)
    asyncio.run(h.trial_cmd(upd, FakeContext()))
    assert db.get_trial_used(72) is False          # paid users keep their trial
    assert any("already have active access" in r.lower() for r in upd.message.replies)


def test_trial_disabled_when_zero_hours(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "TRIAL_HOURS", 0)
    upd = FakeUpdate(73)
    asyncio.run(h.trial_cmd(upd, FakeContext()))
    assert ent.is_paid(73) is False
    assert db.get_trial_used(73) is False
    assert any("aren't available" in r.lower() for r in upd.message.replies)


def test_start_offers_trial_to_new_chat(tmp_db):
    import bot.handlers as h
    upd = FakeUpdate(74)
    asyncio.run(h.start(upd, FakeContext()))
    body = " ".join(upd.message.replies).lower()
    assert "/trial" in body and "free trial" in body


def test_start_hides_trial_after_used(tmp_db):
    import bot.handlers as h
    db.mark_trial_used(75)
    upd = FakeUpdate(75)
    asyncio.run(h.start(upd, FakeContext()))
    body = " ".join(upd.message.replies).lower()
    assert "/trial" not in body                     # no longer offered
    assert "active pass" in body                     # still shows the paywall


# ---- entitlement gate ----
def _make_gated():
    ran = {"v": False}

    @ent.require_paid()
    async def handler(update, context):
        ran["v"] = True
        await update.message.reply_text("VALUE")
    return handler, ran


def test_gate_paid_runs(tmp_db):
    db.set_paid_until(5, (datetime.now(timezone.utc) + timedelta(days=3)).isoformat())
    handler, ran = _make_gated()
    asyncio.run(handler(FakeUpdate(5), FakeContext()))
    assert ran["v"] is True


def test_gate_unpaid_blocked_after_trial_used(tmp_db):
    """A chat that already used its trial is gated (paywall, handler not run)."""
    db.mark_trial_used(5)
    handler, ran = _make_gated()
    upd = FakeUpdate(5)
    asyncio.run(handler(upd, FakeContext()))
    assert ran["v"] is False
    assert any("pass" in r.lower() or "usdc" in r.lower() for r in upd.message.replies)


def test_gate_blocked_when_trials_disabled(tmp_db, monkeypatch):
    """With trials off, a fresh unpaid chat is gated immediately."""
    monkeypatch.setattr(config, "TRIAL_HOURS", 0)
    handler, ran = _make_gated()
    upd = FakeUpdate(6)
    asyncio.run(handler(upd, FakeContext()))
    assert ran["v"] is False
    assert db.get_trial_used(6) is False


def test_first_gated_use_auto_starts_trial(tmp_db, monkeypatch):
    _stub_cycles(monkeypatch)
    handler, ran = _make_gated()
    # 1st call: auto-starts the trial and runs the handler.
    u1 = FakeUpdate(9)
    asyncio.run(handler(u1, FakeContext(job_queue=FakeJobQueue())))
    assert ran["v"] is True
    assert db.get_trial_used(9) is True
    assert ent.is_paid(9) is True                       # trial window is live
    assert any("trial started" in r.lower() for r in u1.message.replies)
    # Still works during the trial window (no second grant needed).
    ran["v"] = False
    asyncio.run(handler(FakeUpdate(9), FakeContext()))
    assert ran["v"] is True
    # After the window elapses, the used trial no longer unlocks — blocked.
    db.set_paid_until(9, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    ran["v"] = False
    u3 = FakeUpdate(9)
    asyncio.run(handler(u3, FakeContext()))
    assert ran["v"] is False
    assert any("pass" in r.lower() or "usdc" in r.lower() for r in u3.message.replies)


def test_expired_paid_regates(tmp_db):
    db.set_paid_until(5, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    db.mark_trial_used(5)              # trial already spent, so no auto-trial rescue
    assert ent.is_paid(5) is False
    handler, ran = _make_gated()
    asyncio.run(handler(FakeUpdate(5), FakeContext()))
    assert ran["v"] is False


def test_owner_bypass(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    # Owner is always paid without any payment...
    assert ent.is_paid(777) is True
    assert db.get_paid_until(777) is None
    # ...and the gate never burns the owner's trial.
    handler, ran = _make_gated()
    asyncio.run(handler(FakeUpdate(777), FakeContext()))
    assert ran["v"] is True
    assert db.get_trial_used(777) is False
    # A non-owner unpaid chat is still gated.
    assert ent.is_paid(778) is False


def test_owner_bypass_disabled_when_zero(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 0)
    assert ent.is_paid(0) is False        # default 0 must not grant access
    assert ent.is_paid(123) is False


def test_correct_handlers_gated(tmp_db):
    import bot.handlers as h
    gated = ["scan", "coin_cmd", "wallets_cmd", "confluence_cmd", "dexs_cmd", "scores_cmd"]
    free = ["start", "paid_cmd", "trial_cmd", "stop_cmd", "toggle_alerts", "status_cmd", "help_cmd"]
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
    _patch_rpc(monkeypatch, _result(REF_UNITS))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=REF_UNITS))
    assert out["ok"] is True, out


def test_amount_mismatch_rejected(tmp_db, monkeypatch):
    """A tx paying a DIFFERENT chat's reference (a full nonce-spacing away) is
    rejected — the binding still holds with the one-cent window."""
    _patch_rpc(monkeypatch, _result(REF_UNITS + 20_000))   # another chat's slot (+0.02)
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=REF_UNITS))
    assert out["ok"] is False and out["reason"] == "amount_mismatch", out


def test_overpay_within_window_accepted(tmp_db, monkeypatch):
    """A slight over/round-up (under a cent) still lands in this chat's window."""
    _patch_rpc(monkeypatch, _result(REF_UNITS + 5_000))   # +0.005 USDC
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=REF_UNITS))
    assert out["ok"] is True, out


def test_overpay_beyond_window_rejected(tmp_db, monkeypatch):
    """An over-pay of >= 0.01 USDC is rejected so it can't collide into the next
    chat's nonce slot (the window is half-open at reference + 0.01)."""
    _patch_rpc(monkeypatch, _result(REF_UNITS + 10_000))   # exactly +0.01 USDC
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=REF_UNITS))
    assert out["ok"] is False and out["reason"] == "amount_mismatch", out


def test_below_reference_rejected(tmp_db, monkeypatch):
    """Anything below the reference is still rejected (under-payment)."""
    _patch_rpc(monkeypatch, _result(REF_UNITS - 1))
    out = asyncio.run(solana_pay.verify_usdc_payment(SIG, expected_units=REF_UNITS))
    assert out["ok"] is False and out["reason"] == "amount_too_low", out


def test_window_smaller_than_nonce_spacing():
    """The accept window must be strictly smaller than the nonce spacing, so two
    chats' windows can never overlap."""
    from core.solana_pay import PAYMENT_AMOUNT_WINDOW_UNITS
    from storage.database import PAYMENT_REF_STEP_UNITS
    assert PAYMENT_AMOUNT_WINDOW_UNITS < PAYMENT_REF_STEP_UNITS


def test_payment_reference_is_unique_and_stable(tmp_db):
    a1 = db.payment_reference(1, "week")
    a2 = db.payment_reference(1, "week")   # stable for the same chat + plan
    b = db.payment_reference(2, "week")
    assert a1 == a2
    assert a1 != b                        # unique across chats
    base = round(config.PAYMENT_PLANS["week"]["price_usd"] * 1_000_000)
    assert a1 > base
    assert (a1 - base) % db.PAYMENT_REF_STEP_UNITS == 0          # on the spacing grid
    assert abs(b - a1) >= db.PAYMENT_REF_STEP_UNITS              # spacing >= window


def test_payment_reference_plan_shifts_base_same_slot(tmp_db):
    """A chat keeps one stable slot across plans; only the whole-dollar base
    differs, so its week and month amounts sit $20 apart and never collide."""
    week = db.payment_reference(1, "week")
    month = db.payment_reference(1, "month")
    week_base = round(config.PAYMENT_PLANS["week"]["price_usd"] * 1_000_000)
    month_base = round(config.PAYMENT_PLANS["month"]["price_usd"] * 1_000_000)
    assert week - week_base == month - month_base      # same per-chat nonce
    assert month - week == month_base - week_base       # exactly $20.00 apart


def test_plan_amount_windows_never_overlap(tmp_db):
    """No accepted amount for a cheaper plan can reach the next plan's lowest
    reference — the multi-tier extension of the C1 binding. Guards against an
    env override (price/slot count) that would let a week payment match a month
    reference."""
    from core.solana_pay import PAYMENT_AMOUNT_WINDOW_UNITS
    step = db.PAYMENT_REF_STEP_UNITS
    max_slot = db._PAYMENT_REF_MAX_SLOTS
    prices = sorted(round(p["price_usd"] * 1_000_000) for p in config.PAYMENT_PLANS.values())
    for lo, hi in zip(prices, prices[1:]):
        lo_top = lo + max_slot * step + PAYMENT_AMOUNT_WINDOW_UNITS  # cheaper plan's ceiling
        hi_bottom = hi + step                                        # dearer plan's floor
        assert lo_top <= hi_bottom, (lo, hi)


def test_payment_bound_to_payer(tmp_db, monkeypatch):
    """A different chat cannot redeem another chat's payment (the C1 exploit)."""
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    a_units = db.payment_reference(1, "week")
    b_units = db.payment_reference(2, "week")
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
    asyncio.run(h.paid_cmd(ub, FakeContext(args=["week", SIG])))
    assert ent.is_paid(2) is False
    assert db.is_payment_used(SIG) is False
    assert any("match" in r.lower() for r in ub.message.replies), ub.message.replies

    # Rightful chat A redeems its own tx -> granted.
    ua = FakeUpdate(1)
    asyncio.run(h.paid_cmd(ua, FakeContext(args=["week", SIG])))
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


# --------------------------- /start actually activates entitled chats ---------------------------
def test_start_paid_lands_in_alert_chats_and_seeds(tmp_db, monkeypatch):
    """An entitled (paid) chat that runs /start enters get_alert_chats so the
    proactive pushes flow, and the wallet seed is scheduled for the cycle."""
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    from services import cycles
    db.set_paid_until(60, (datetime.now(timezone.utc) + timedelta(days=3)).isoformat())
    db.deactivate_chat(60)                    # prove /start (not set_paid_until) activates
    assert 60 not in db.get_alert_chats()
    ctx = FakeContext(job_queue=FakeJobQueue())
    asyncio.run(h.start(FakeUpdate(60), ctx))
    assert 60 in db.get_alert_chats()         # now receives proactive alerts
    assert db.get_alerts_enabled(60) is True
    # the wallet baseline is scheduled so the proactive cycle can run
    assert any(cb is cycles.wallet_seed_job for cb, _ in ctx.job_queue.jobs)


def test_start_owner_active_and_alert_enabled(tmp_db, monkeypatch):
    """The operator (no paid_until) is genuinely activated by /start and lands in
    both get_active_chats and get_alert_chats — without a fake entitlement."""
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 888)
    _stub_cycles(monkeypatch)
    assert 888 not in db.get_active_chats()   # not activated before /start
    ctx = FakeContext(job_queue=FakeJobQueue())
    asyncio.run(h.start(FakeUpdate(888), ctx))
    assert 888 in db.get_active_chats()       # active despite no paid_until
    assert db.get_alerts_enabled(888) is True
    assert 888 in db.get_alert_chats()        # operator receives proactive alerts
    assert db.get_paid_until(888) is None     # still operator (no expiry), no fake entitlement


def test_start_unpaid_not_activated(tmp_db):
    """A non-paid, non-owner chat is NOT activated by /start — only the paywall."""
    import bot.handlers as h
    ctx = FakeContext(job_queue=FakeJobQueue())
    asyncio.run(h.start(FakeUpdate(61), ctx))
    assert 61 not in db.get_active_chats()
    assert 61 not in db.get_alert_chats()
    assert db.get_alerts_enabled(61) is False
    assert ctx.job_queue.jobs == []           # no wallet seed for an unpaid chat


def test_paid_lands_in_alert_chats(tmp_db, monkeypatch):
    """A successful /paid activates for proactive alerts without a second /start."""
    import bot.handlers as h
    _stub_cycles(monkeypatch)
    monkeypatch.setattr(h, "verify_usdc_payment",
                        lambda tx, expected_units=None: _async(
                            {"ok": True, "reason": "payment_verified", "received": expected_units}))
    asyncio.run(h.paid_cmd(FakeUpdate(62), FakeContext(args=["week", SIG], job_queue=FakeJobQueue())))
    assert ent.is_paid(62) is True
    assert 62 in db.get_alert_chats()


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
    asyncio.run(h.paid_cmd(FakeUpdate(8), FakeContext(args=["week", SIG])))
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

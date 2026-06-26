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
                        lambda tx: _async({"ok": True, "reason": "payment_verified"}))
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

    def _verify(tx):
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
                        lambda tx: _async({"ok": False, "reason": "amount_too_low"}))
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


# --------------------------- helper ---------------------------
def _async(value):
    async def _coro():
        return value
    return _coro()

"""Tests for the persistent wallet identity & profile layer.

  * identity.codename_for — deterministic & address-tied.
  * skill_tier cutoffs (env-configurable) and derive_state priority.
  * update_profile — accumulates behavioral stats across cycles (adds-to-losers,
    closes win/loss + hold, flips) rather than recomputing each cycle.
  * profile_line / format_dossier presentation.
  * with_identity alert injection.
  * /wallet command resolves by codename and by address.

Run: pytest tests/test_wallet_profile.py
"""
import types
import asyncio

import pytest

import config
from storage import database as db
from core import identity
from services import wallet_profile as wp
from bot import formatting_wallet as fw


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(config, "WALLET_TIER_SHARP", 25.0)
    monkeypatch.setattr(config, "WALLET_TIER_SOLID", 10.0)
    monkeypatch.setattr(config, "WALLET_TIER_AVERAGE", 0.0)
    monkeypatch.setattr(config, "WALLET_STATE_ROI_EPS", 0.0)
    monkeypatch.setattr(config, "WALLET_FLAIL_WINDOW_MIN", 60)
    yield


ADDR = "0x" + "a" * 38 + "1ab2"
ROW = {"windowPerformances": {
    "day": {"roi": "0.012", "pnl": "1200"},
    "week": {"roi": "0.18", "pnl": "18000"},
    "month": {"roi": "0.30", "pnl": "30000"},
}}


def _pos(coin, side, size, entry=100.0, upnl=0.0):
    return {"coin": coin, "side": side, "size": float(size),
            "notional_usd": float(size) * entry, "entry_px": entry,
            "liq_px": 0.0, "unrealized_pnl": float(upnl)}


def _save_perf(addr, *, smart=20.0, state="stable", lev=3.0, open_upnl=0.0, acct=1e6, n=1):
    db.save_wallet_performance_snapshot(
        address=addr, account_value=acct, exposure_total=acct * lev, open_upnl=open_upnl,
        negative_upnl=min(0.0, open_upnl), open_positions=n, book_leverage=lev,
        state=state, health_score=50.0, smart_score=smart)


# --------------------------- identity ---------------------------
def test_codename_deterministic_and_address_tied():
    assert identity.codename_for(ADDR) == identity.codename_for(ADDR.upper())
    assert identity.codename_for(ADDR).endswith("-1ab2")          # last 4 of address
    assert identity.codename_for(ADDR) != identity.codename_for("0x" + "b" * 40)


# --------------------------- tiers / state ---------------------------
def test_skill_tier_cutoffs(tmp_db):
    assert wp.skill_tier(30) == "Sharp"
    assert wp.skill_tier(25) == "Sharp"
    assert wp.skill_tier(15) == "Solid"
    assert wp.skill_tier(5) == "Average"
    assert wp.skill_tier(-1) == "Sloppy"


def test_derive_state_priority(tmp_db):
    assert wp.derive_state("self_imploding", 0.1, 0.1, False) == "imploding"  # imploding wins
    assert wp.derive_state("stable", -0.1, 0.1, True) == "stress"            # stress over mixed
    assert wp.derive_state("stable", 0.01, 0.02, False) == "hot"
    assert wp.derive_state("stable", -0.01, -0.02, False) == "cold"
    assert wp.derive_state("stable", -0.01, 0.02, False) == "neutral"        # mixed = neutral


# --------------------------- accumulation ---------------------------
def test_profile_accumulates_across_cycles(tmp_db):
    # Cycle 1 (seed): open BTC long -> baseline lot, no behavioral counts.
    _save_perf(ADDR, smart=28.0, state="hot_streak", lev=4.0, open_upnl=5000)
    wp.update_profile(ADDR, ROW, {}, {"BTC": _pos("BTC", "long", 10, upnl=5000)},
                      [], day_pnl=1200, stress_add=False, seed_mode=True)
    p = db.get_wallet_profile(ADDR)
    assert p["cycles_observed"] == 1 and p["adds_total"] == 0
    assert db.get_open_lot(ADDR, "BTC", "long") is not None
    assert p["skill_tier"] == "Sharp"

    # Cycle 2: add to BTC (+30%) while the day is red -> adds_to_losers + event.
    _save_perf(ADDR, smart=28.0, state="implosion_watch", lev=6.0, open_upnl=-8000)
    prev = {"BTC": _pos("BTC", "long", 10)}
    curr = {"BTC": _pos("BTC", "long", 13, upnl=-8000)}
    wp.update_profile(ADDR, ROW, prev, curr, [], day_pnl=-500, stress_add=True, seed_mode=False)
    p = db.get_wallet_profile(ADDR)
    assert p["adds_total"] == 1 and p["adds_to_losers"] == 1
    assert wp.flails_per_hr(ADDR) >= 1

    # Cycle 3: BTC fully closed at a loss -> loss + cut-in-loss + hold sample.
    _save_perf(ADDR, smart=28.0, state="stable", lev=0.0, open_upnl=0.0, n=0)
    close_ev = [{"type": "close", "coin": "BTC",
                 "prev": _pos("BTC", "long", 13, upnl=-8000), "curr": None,
                 "reduction_pct": 100.0, "full": True}]
    wp.update_profile(ADDR, ROW, {"BTC": _pos("BTC", "long", 13, upnl=-8000)}, {},
                      close_ev, day_pnl=-500, stress_add=False, seed_mode=False)
    p = db.get_wallet_profile(ADDR)
    assert p["closes_observed"] == 1 and p["losses"] == 1 and p["cuts_in_loss"] == 1
    assert p["hold_samples"] == 1
    assert db.get_open_lot(ADDR, "BTC", "long") is None       # lot consumed
    assert p["cycles_observed"] == 3
    assert p["max_drawdown_usd"] <= -8000                     # worst open uPnL recorded


def test_flip_counts_and_relots(tmp_db):
    _save_perf(ADDR)
    wp.update_profile(ADDR, ROW, {}, {"SOL": _pos("SOL", "long", 50, upnl=1000)},
                      [], day_pnl=100, stress_add=False, seed_mode=True)
    _save_perf(ADDR)
    flip_ev = [{"type": "flip", "coin": "SOL",
                "prev": _pos("SOL", "long", 50, upnl=1000),
                "curr": _pos("SOL", "short", 50, upnl=0)}]
    wp.update_profile(ADDR, ROW, {"SOL": _pos("SOL", "long", 50, upnl=1000)},
                      {"SOL": _pos("SOL", "short", 50)}, flip_ev,
                      day_pnl=100, stress_add=False, seed_mode=False)
    p = db.get_wallet_profile(ADDR)
    assert p["flips_total"] == 1 and p["closes_observed"] == 1 and p["wins"] == 1
    assert db.get_open_lot(ADDR, "SOL", "long") is None
    assert db.get_open_lot(ADDR, "SOL", "short") is not None  # re-lotted on new side


# --------------------------- presentation ---------------------------
def test_profile_line_and_dossier(tmp_db):
    _save_perf(ADDR, smart=28.0, state="hot_streak", lev=4.0, open_upnl=5000)
    db.save_positions(ADDR, [_pos("BTC", "long", 10, entry=65000, upnl=5000)])
    wp.update_profile(ADDR, ROW, {}, {"BTC": _pos("BTC", "long", 10, upnl=5000)},
                      [], day_pnl=1200, stress_add=False, seed_mode=True)
    line = wp.profile_line(ADDR)
    assert "Sharp" in line and "hot" in line and "week" in line

    dossier = wp.format_dossier(ADDR)
    assert identity.codename_for(ADDR) in dossier
    assert "Trailing performance" in dossier and "Behavior" in dossier
    assert "BTC LONG" in dossier
    assert "e+0" not in dossier        # price rendered sanely, no scientific notation


def test_with_identity_injection():
    cap = "🐋 <b>WHALE MOVE</b>\n━━━━━━━━━━━━━━━━\nbody line\n"
    out = fw.with_identity(cap, "SilentOrca-12ab", "Sharp · 🔥 hot")
    assert "SilentOrca-12ab" in out and "Sharp · 🔥 hot" in out
    assert out.index("SilentOrca") < out.index("body line")   # injected above body
    # no codename -> unchanged
    assert fw.with_identity(cap, "") == cap


# --------------------------- /wallet command ---------------------------
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


def _seed_profile():
    _save_perf(ADDR, smart=28.0, state="hot_streak", lev=4.0, open_upnl=5000)
    wp.update_profile(ADDR, ROW, {}, {"BTC": _pos("BTC", "long", 10, upnl=5000)},
                      [], day_pnl=1200, stress_add=False, seed_mode=True)


def test_wallet_cmd_by_codename_and_address(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)   # owner bypass -> passes paywall
    _seed_profile()
    code = identity.codename_for(ADDR)

    upd = FakeUpdate(777)
    asyncio.run(h.wallet_cmd(upd, FakeContext(args=[code])))
    assert any(code in r for r in upd.message.replies)

    upd2 = FakeUpdate(777)
    asyncio.run(h.wallet_cmd(upd2, FakeContext(args=[ADDR])))
    assert any("Trailing performance" in r for r in upd2.message.replies)


def test_wallet_cmd_unknown(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    upd = FakeUpdate(777)
    asyncio.run(h.wallet_cmd(upd, FakeContext(args=["NoSuchName-9999"])))
    assert any("No tracked wallet" in r for r in upd.message.replies)

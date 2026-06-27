"""Tests for whale exit / flip / trim detection and the tiny-base add fix.

Covers:
  * diff_positions — full close, reduced close, flip, trim (on/off), no-op.
  * is_tiny_base — grow-from-nothing relabel guard.
  * fmt_price — no scientific notation.
  * get_last_snapshot_positions — returns only the previous cycle's batch.
  * check_whale_positions end to end — a baseline cycle then a close + flip +
    new-open, asserting one alert each and no flip/new double-fire, plus the
    tiny-base OPENED NEW relabel.

Run: pytest tests/test_whale_exits.py
"""
import asyncio

import pytest

import config
from storage import database as db
from trackers import wallet_tracker as wt
from utils.fmt import fmt_price


# --------------------------- fixtures ---------------------------
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


def _pos(coin, side, size, entry=100.0, upnl=0.0):
    return {
        "coin": coin, "side": side, "size": float(size),
        "notional_usd": float(size) * entry, "entry_px": entry,
        "liq_px": 0.0, "unrealized_pnl": upnl,
    }


# --------------------------- diff_positions ---------------------------
def test_diff_full_close():
    prev = {"BTC": _pos("BTC", "long", 10)}
    ev = wt.diff_positions(prev, {}, close_pct=80, trim_pct=30, trim_enabled=False)
    assert len(ev) == 1
    assert ev[0]["type"] == "close" and ev[0]["full"] is True and ev[0]["reduction_pct"] == 100.0


def test_diff_reduced_close():
    prev = {"BTC": _pos("BTC", "long", 10)}
    curr = {"BTC": _pos("BTC", "long", 1.5)}   # -85%
    ev = wt.diff_positions(prev, curr, close_pct=80, trim_pct=30, trim_enabled=False)
    assert ev[0]["type"] == "close" and ev[0]["full"] is False
    assert round(ev[0]["reduction_pct"]) == 85


def test_diff_flip():
    prev = {"SOL": _pos("SOL", "long", 50)}
    curr = {"SOL": _pos("SOL", "short", 50)}
    ev = wt.diff_positions(prev, curr, close_pct=80, trim_pct=30, trim_enabled=False)
    assert ev[0]["type"] == "flip"
    assert ev[0]["prev"]["side"] == "long" and ev[0]["curr"]["side"] == "short"


def test_diff_trim_gated_by_flag():
    prev = {"ETH": _pos("ETH", "long", 10)}
    curr = {"ETH": _pos("ETH", "long", 6)}     # -40%, in trim band
    assert wt.diff_positions(prev, curr, close_pct=80, trim_pct=30, trim_enabled=False) == []
    ev = wt.diff_positions(prev, curr, close_pct=80, trim_pct=30, trim_enabled=True)
    assert ev[0]["type"] == "trim" and round(ev[0]["reduction_pct"]) == 40


def test_diff_small_change_is_noop():
    prev = {"ETH": _pos("ETH", "long", 10)}
    curr = {"ETH": _pos("ETH", "long", 9)}     # -10%, below trim band
    assert wt.diff_positions(prev, curr, close_pct=80, trim_pct=30, trim_enabled=True) == []


# --------------------------- tiny-base + price fmt ---------------------------
def test_is_tiny_base():
    assert wt.is_tiny_base(100, 1_000_000, 5000, 5) is True       # under absolute floor
    assert wt.is_tiny_base(40_000, 1_000_000, 5000, 5) is True    # under 5% of new
    assert wt.is_tiny_base(200_000, 1_000_000, 5000, 5) is False  # real prior position


def test_fmt_price_no_scientific():
    assert fmt_price(60280.0) == "60,280"
    assert fmt_price(1.2345) == "1.23"
    assert "e" not in fmt_price(0.00012345).lower()


# --------------------------- previous-cycle batch ---------------------------
def test_get_last_snapshot_positions_is_one_batch(tmp_db):
    addr = "0x" + "a" * 40
    db.save_positions(addr, [_pos("BTC", "long", 10), _pos("SOL", "long", 5)])
    # later cycle: BTC gone, only SOL — must reflect just this batch
    import time
    time.sleep(1.05)  # snapshot_at is second-resolution datetime('now')
    db.save_positions(addr, [_pos("SOL", "long", 5)])
    rows = db.get_last_snapshot_positions(addr)
    coins = {r["coin"] for r in rows}
    assert coins == {"SOL"}      # not BTC from the earlier batch


# --------------------------- end-to-end whale scan ---------------------------
def _patch_send(monkeypatch, sent):
    async def fake_send_alert(msg, paid_only=False):
        sent.append(msg)
    async def fake_chart(**kw):
        return None  # force text path
    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(wt, "send_alert", fake_send_alert)
    monkeypatch.setattr(wt, "generate_whale_chart", fake_chart)
    monkeypatch.setattr(wt.asyncio, "sleep", no_sleep)


def _lb_row(addr):
    return {"ethAddress": addr, "accountValue": "2000000",
            "windowPerformances": {"day": {"pnl": "0"}}}


def test_close_flip_newopen_end_to_end(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "WHALE_POSITION_THRESHOLD_USD", 500_000)
    monkeypatch.setattr(config, "WHALE_TRIM_ENABLED", False)
    addr = "0x" + "b" * 40
    assets = [{"name": c, "mark_px": 100.0} for c in ("BTC", "SOL", "ETH")]
    sent = []
    _patch_send(monkeypatch, sent)

    # Seed cycle: BTC long $1M, SOL long $600k.
    seed_positions = {addr: [_pos("BTC", "long", 10_000), _pos("SOL", "long", 6_000)]}
    asyncio.run(wt.check_whale_positions([_lb_row(addr)], assets, seed_positions, seed_mode=True))
    assert sent == []   # seed is silent

    import time
    time.sleep(1.05)
    # Live cycle: BTC closed, SOL flipped to short, ETH opened new $1M.
    live_positions = {addr: [_pos("SOL", "short", 6_000), _pos("ETH", "long", 10_000)]}
    asyncio.run(wt.check_whale_positions([_lb_row(addr)], assets, live_positions, seed_mode=False))

    blob = "\n".join(sent)
    assert "WHALE CLOSED" in blob          # BTC exit detected
    assert "WHALE FLIPPED" in blob         # SOL reversal as its own alert
    assert "LONG → SHORT" in blob
    assert "WHALE MOVE" in blob            # ETH genuine new open
    # SOL must not also fire a plain new-open: exactly one SOL-related alert (flip)
    assert sum("SOL-PERP" in m for m in sent) == 1


def test_tiny_base_relabels_opened_new(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "WHALE_POSITION_THRESHOLD_USD", 500_000)
    monkeypatch.setattr(config, "WHALE_TINY_BASE_USD", 5_000.0)
    addr = "0x" + "c" * 40
    assets = [{"name": "BTC", "mark_px": 100.0}]
    sent = []
    _patch_send(monkeypatch, sent)

    # Seed: a dust BTC long ($100), below the tiny-base floor.
    asyncio.run(wt.check_whale_positions(
        [_lb_row(addr)], assets, {addr: [_pos("BTC", "long", 1)]}, seed_mode=True))
    import time
    time.sleep(1.05)
    # Live: grows to a real $1M position → should read OPENED NEW, not "+99900%".
    asyncio.run(wt.check_whale_positions(
        [_lb_row(addr)], assets, {addr: [_pos("BTC", "long", 10_000)]}, seed_mode=False))

    blob = "\n".join(sent)
    assert "WHALE OPENED NEW" in blob
    # The meaningless add percentage is gone: no "➕ Added: ... (+X%)" line.
    assert "Added" not in blob
    assert "%)" not in blob

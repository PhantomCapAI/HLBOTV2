"""Tests for the STRONG-CONFLUENCE overlap fix.

  * _setup_directions — robust to multiple inner setups + top-level direction.
  * find_confluence — fires when a crypto setup aligns with whale positioning,
    handles multiple setups per coin, and stays silent when sides oppose.
  * scan_specific — always scans the crypto universe and filters to the
    requested (bare-symbol) coins so the join overlaps the whale markets.

Run: pytest tests/test_correlation_overlap.py
"""
import asyncio

import pytest

import config
from storage import database as db
from services import correlation as corr
from scanner import screener
from scanner.indicators import TFRead


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(config, "CORRELATION_MIN_SCORE", 60)
    monkeypatch.setattr(config, "CORRELATION_MIN_WHALES", 2)
    monkeypatch.setattr(config, "WHALE_POSITION_THRESHOLD_USD", 500_000)
    yield


def _pos(coin, side, notional):
    return {"coin": coin, "side": side, "size": notional / 100.0,
            "notional_usd": notional, "entry_px": 100.0, "liq_px": 0.0,
            "unrealized_pnl": 0.0}


def _whales_long_btc():
    db.save_positions("0x" + "a" * 40, [_pos("BTC", "long", 2_000_000)])
    db.save_positions("0x" + "b" * 40, [_pos("BTC", "long", 1_500_000)])


# --------------------------- _setup_directions ---------------------------
def test_setup_directions_multiple_and_toplevel():
    s = {"setups": [{"direction": "long"}, {"direction": "short"}]}
    assert corr._setup_directions(s) == {"long", "short"}
    assert corr._setup_directions({"direction": "SHORT", "setups": []}) == {"short"}
    assert corr._setup_directions({"setups": [{"direction": "bogus"}]}) == set()


# --------------------------- find_confluence ---------------------------
def test_find_confluence_fires_on_crypto_match(tmp_db):
    _whales_long_btc()
    setups = [{"coin": "BTC", "score": 72, "setups": [{"direction": "long"}]}]
    matches = corr.find_confluence(setups)
    assert len(matches) == 1
    assert matches[0]["coin"] == "BTC" and matches[0]["side"] == "long"
    assert matches[0]["whales"] == 2


def test_find_confluence_no_match_when_side_opposes(tmp_db):
    _whales_long_btc()
    setups = [{"coin": "BTC", "score": 72, "setups": [{"direction": "short"}]}]
    assert corr.find_confluence(setups) == []


def test_find_confluence_handles_multiple_setups_per_coin(tmp_db):
    _whales_long_btc()
    # A coin carrying both a long and a short setup must still match the long.
    setups = [{"coin": "BTC", "score": 70, "setups": [
        {"direction": "short"}, {"direction": "long"}]}]
    matches = corr.find_confluence(setups)
    assert len(matches) == 1 and matches[0]["side"] == "long"


def test_find_confluence_respects_score_gate(tmp_db):
    _whales_long_btc()
    setups = [{"coin": "BTC", "score": 50, "setups": [{"direction": "long"}]}]  # < 60
    assert corr.find_confluence(setups) == []


def test_find_confluence_requires_min_whales(tmp_db):
    db.save_positions("0x" + "a" * 40, [_pos("BTC", "long", 2_000_000)])  # only 1 whale
    setups = [{"coin": "BTC", "score": 72, "setups": [{"direction": "long"}]}]
    assert corr.find_confluence(setups) == []


# --------------------------- scan_specific ---------------------------
def _read(lean, regime="trend up"):
    return TFRead(tf="4h", close=100, ema21=99, ema50=99, ema200=99, adx=28,
                  plus_di=30, minus_di=10, rsi=55, macd_hist=1.0, atr=2.0,
                  bb_width=0.05, vwap=98, structure="uptrend (HH/HL)",
                  regime=regime, lean=lean, notes=[], bars=300)


def test_scan_specific_filters_and_uses_crypto(tmp_db, monkeypatch):
    meta = [{"name": "BTC"}, {"name": "ETH"}, {"name": "HYPE"}]
    ctxs = [{"funding": "0", "markPx": "100", "openInterest": "10000", "dayNtlVlm": "1e9"}
            for _ in meta]

    async def fake_meta():
        return meta, ctxs
    async def fake_candles(coin, tf):
        return None  # ignored by the stubbed analyse_tf
    monkeypatch.setattr(screener.hl, "get_meta_and_ctxs", fake_meta)
    monkeypatch.setattr(screener.hl, "candles_df", fake_candles)
    monkeypatch.setattr(screener, "analyse_tf", lambda tf, df: _read(5))

    out = asyncio.run(screener.scan_specific(["BTC", "HYPE"]))
    coins = {d["coin"] for d in out}
    assert coins == {"BTC", "HYPE"}        # only requested coins, ETH excluded
    assert all(d["direction"] == "long" for d in out)

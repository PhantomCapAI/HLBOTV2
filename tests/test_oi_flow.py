"""Tests for OI-trend persistence + funding/OI crowding read.

  * classify_crowding — the standard framework (crowded longs/shorts, unwind,
    neutral) including the elevated-funding gate.
  * crowding_modifier — score points + the supersede flag that prevents
    double-counting the funding term.
  * calculate_confluence_score — crowding modifier interacts with the existing
    funding-vs-crowd term correctly (no double count; unwind stacks).
  * oi_snapshots persistence + get_oi_ago + oi_context ΔOI%.
  * flow_line formatting.

Run: pytest tests/test_oi_flow.py
"""
import pytest

import config
from storage import database as db
from scanner import flow
from scanner.indicators import TFRead
from scanner.screener import calculate_confluence_score, EXTREME_FUNDING_HR

EX = EXTREME_FUNDING_HR
ELEV = EX * 2       # an "elevated" funding rate
LOW = EX * 0.1      # below the elevated threshold


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(config, "OI_LOOKBACK_SHORT_MIN", 60)
    monkeypatch.setattr(config, "OI_LOOKBACK_LONG_MIN", 240)
    monkeypatch.setattr(config, "OI_RISE_PCT", 5.0)
    monkeypatch.setattr(config, "OI_FALL_PCT", 5.0)
    yield


def _read(lean, regime="trend up", **kw):
    d = dict(close=100, ema21=99, ema50=99, ema200=99, adx=28, plus_di=30,
             minus_di=10, rsi=55, macd_hist=1.0, atr=2.0, bb_width=0.05, vwap=98,
             structure="uptrend (HH/HL)", regime=regime, lean=lean, notes=[], bars=300)
    d.update(kw)
    return TFRead(tf="4h", **d)


# --------------------------- classify_crowding ---------------------------
def test_crowded_long():
    assert flow.classify_crowding(10, 2, ELEV, rise_pct=5, fall_pct=5,
                                  funding_threshold=EX) == "crowded_long"


def test_crowded_short():
    assert flow.classify_crowding(10, -2, -ELEV, rise_pct=5, fall_pct=5,
                                  funding_threshold=EX) == "crowded_short"


def test_rising_oi_but_funding_not_elevated_is_neutral():
    assert flow.classify_crowding(10, 2, LOW, rise_pct=5, fall_pct=5,
                                  funding_threshold=EX) == "neutral"


def test_falling_oi_is_unwind_regardless_of_funding():
    assert flow.classify_crowding(-9, 1, ELEV, rise_pct=5, fall_pct=5,
                                  funding_threshold=EX) == "unwind"


def test_no_history_is_neutral():
    assert flow.classify_crowding(None, None, ELEV, rise_pct=5, fall_pct=5,
                                  funding_threshold=EX) == "neutral"


def test_flat_oi_is_neutral():
    assert flow.classify_crowding(2, 2, ELEV, rise_pct=5, fall_pct=5,
                                  funding_threshold=EX) == "neutral"


# --------------------------- crowding_modifier ---------------------------
def test_modifier_join_vs_fade_and_supersede():
    assert flow.crowding_modifier("crowded_long", 1) == (-12.0, True)   # join fragile longs
    assert flow.crowding_modifier("crowded_long", -1) == (8.0, True)    # fade for squeeze
    assert flow.crowding_modifier("crowded_short", -1) == (-12.0, True)
    assert flow.crowding_modifier("crowded_short", 1) == (8.0, True)
    assert flow.crowding_modifier("unwind", 1) == (-5.0, False)         # stacks, not supersede
    assert flow.crowding_modifier("neutral", 1) == (0.0, False)


# --------------------------- no double-count in the score ---------------------------
def test_crowding_supersedes_funding_term_no_double_count():
    reads = {"1h": _read(5), "4h": _read(5)}
    # Long into elevated +funding. Plain funding term alone = -10 (into crowd).
    base = calculate_confluence_score(reads, funding=ELEV)
    # With crowding state crowded_long, the modifier (-12) SUPERSEDES funding
    # (not -10 + -12). So score should equal base + (-12) - (-10) = base - 2.
    crowded = calculate_confluence_score(reads, funding=ELEV,
                                         crowding={"state": "crowded_long"})
    assert round(crowded - base, 1) == -2.0


def test_fade_the_crowd_rewards():
    reads = {"1h": _read(-5, regime="trend down", plus_di=10, minus_di=30,
                         macd_hist=-1.0, rsi=42, close=90, ema21=92, vwap=93,
                         structure="downtrend (LH/LL)"),
             "4h": _read(-5, regime="trend down", plus_di=10, minus_di=30,
                         macd_hist=-1.0, rsi=42, close=90, ema21=92, vwap=93,
                         structure="downtrend (LH/LL)")}
    # Short while longs are crowded -> fading the fragile crowd -> +8, supersedes.
    faded = calculate_confluence_score(reads, funding=ELEV,
                                       crowding={"state": "crowded_long"})
    plain = calculate_confluence_score(reads, funding=ELEV)  # funding term gives +5 (against crowd)
    assert faded > plain


def test_unwind_stacks_with_funding_term():
    reads = {"1h": _read(5), "4h": _read(5)}
    base = calculate_confluence_score(reads, funding=ELEV)          # funding -10 (into crowd)
    unwind = calculate_confluence_score(reads, funding=ELEV,
                                        crowding={"state": "unwind"})
    assert round(unwind - base, 1) == -5.0                          # -5 stacks on top


# --------------------------- persistence + oi_context ---------------------------
def test_oi_snapshot_and_delta(tmp_db):
    db.save_oi_snapshot("BTC", 100_000_000, 0.0001, 60000.0)
    assert db.get_oi_ago("BTC", 0) is not None       # 0-min-ago = latest
    assert db.get_oi_ago("BTC", 60) is None          # nothing 60m old yet

    # Backdate a snapshot ~70 min ago so the short window has a baseline.
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO oi_snapshots (coin, oi_usd, funding, mark_px, snapshot_at) "
            "VALUES ('BTC', 80000000, 0.0001, 59000, datetime('now', '-70 minutes'))")
    ctx = flow.oi_context("BTC", 100_000_000, ELEV, 61000.0,
                          funding_threshold=EX, persist=False)
    # +25% OI over the short window, price up, elevated +funding -> crowded longs.
    assert round(ctx["doi_short"]) == 25
    assert ctx["state"] == "crowded_long"


def test_flow_line_formats(tmp_db):
    ctx = {"state": "crowded_long", "doi_primary": 18.0, "window_min": 240,
           "funding": 0.0004}
    line = flow.flow_line(ctx)
    assert "OI +18% 4h" in line and "crowded longs" in line and "%/hr" in line
    assert flow.flow_line({}) == ""

"""Tests for skill-weighted wallet selection (smart_score).

Covers:
  * window_roi parsing of both leaderboard (list-of-pairs) and watch (dict) rows.
  * has_positive_week_roi / filter_by_performance — drop negative trailing-week ROI.
  * wallet_added_under_stress — adding to a position while the day is red.
  * compute_smart_score — ROI backbone minus leverage and stress-add penalties.
  * DB round-trip — smart_score persists; /scores ordering is skill-ranked.
  * confluence ranking — correlation ranks clusters by combined smart_score,
    not combined notional.

Run: pytest tests/test_smart_score.py
"""
import pytest

import config
from storage import database as db
from trackers import wallet_tracker as wt
from services import correlation as corr


# --------------------------- fixtures ---------------------------
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


def _perf(address, smart, *, account_value=1_000_000.0, open_upnl=0.0,
          state="stable", book_leverage=1.0):
    db.save_wallet_performance_snapshot(
        address=address, account_value=account_value, exposure_total=account_value,
        open_upnl=open_upnl, negative_upnl=min(0.0, open_upnl), open_positions=1,
        book_leverage=book_leverage, state=state, health_score=50.0, smart_score=smart,
    )


# --------------------------- window_roi ---------------------------
def test_window_roi_leaderboard_pairs():
    row = {"windowPerformances": [
        ["day", {"pnl": "1", "roi": "0.001"}],
        ["week", {"pnl": "2", "roi": "-0.0149"}],
        ["month", {"pnl": "3", "roi": "0.03"}],
        ["allTime", {"pnl": "4", "roi": "0.5"}],
    ]}
    week, month = wt.window_roi(row)
    assert round(week, 4) == -0.0149
    assert round(month, 4) == 0.03


def test_window_roi_watch_row_defaults_zero():
    # Curated watch rows carry a plain dict with no roi → neutral 0.
    row = {"windowPerformances": {"day": {"pnl": 0}, "week": {"pnl": 0}}}
    assert wt.window_roi(row) == (0.0, 0.0)


def test_window_roi_missing_and_garbage():
    assert wt.window_roi({}) == (0.0, 0.0)
    assert wt.window_roi({"windowPerformances": [["week", {"roi": "n/a"}]]}) == (0.0, 0.0)


# --------------------------- performance filter ---------------------------
def test_has_positive_week_roi():
    assert wt.has_positive_week_roi({"windowPerformances": [["week", {"roi": "0.01"}]]}) is True
    assert wt.has_positive_week_roi({"windowPerformances": [["week", {"roi": "0"}]]}) is True
    assert wt.has_positive_week_roi({"windowPerformances": [["week", {"roi": "-0.0001"}]]}) is False
    # missing roi → kept (neutral)
    assert wt.has_positive_week_roi({"windowPerformances": {"week": {"pnl": 0}}}) is True


def test_filter_by_performance_drops_losers():
    lb = [
        {"ethAddress": "0xwin", "windowPerformances": [["week", {"roi": "0.05"}]]},
        {"ethAddress": "0xlose", "windowPerformances": [["week", {"roi": "-0.05"}]]},
        {"ethAddress": "0xflat", "windowPerformances": [["week", {"roi": "0.0"}]]},
    ]
    kept = wt.filter_by_performance(lb)
    addrs = {r["ethAddress"] for r in kept}
    assert addrs == {"0xwin", "0xflat"}


# --------------------------- stress-add ---------------------------
def test_added_under_stress_true_when_growing_while_red():
    positions = [{"coin": "BTC", "side": "long", "notional_usd": 120_000}]
    prev = {"BTC:long": {"notional_usd": 100_000}}
    assert wt.wallet_added_under_stress(positions, prev, day_pnl=-5_000) is True


def test_added_under_stress_false_when_green():
    positions = [{"coin": "BTC", "side": "long", "notional_usd": 120_000}]
    prev = {"BTC:long": {"notional_usd": 100_000}}
    assert wt.wallet_added_under_stress(positions, prev, day_pnl=5_000) is False


def test_added_under_stress_false_when_not_growing():
    positions = [{"coin": "BTC", "side": "long", "notional_usd": 101_000}]  # +1% < threshold
    prev = {"BTC:long": {"notional_usd": 100_000}}
    assert wt.wallet_added_under_stress(positions, prev, day_pnl=-5_000) is False


def test_added_under_stress_false_when_new_position():
    positions = [{"coin": "BTC", "side": "long", "notional_usd": 120_000}]
    assert wt.wallet_added_under_stress(positions, {}, day_pnl=-5_000) is False


# --------------------------- compute_smart_score ---------------------------
def test_smart_score_roi_backbone():
    assert wt.compute_smart_score(0.10, 0.05, book_leverage=3.0, added_under_stress=False) == 15.0


def test_smart_score_leverage_penalty():
    # 5x book → (5-3)*2 = 4 points off
    assert wt.compute_smart_score(0.10, 0.05, book_leverage=5.0, added_under_stress=False) == 11.0


def test_smart_score_stress_penalty():
    assert wt.compute_smart_score(0.10, 0.05, book_leverage=5.0, added_under_stress=True) == -4.0


def test_smart_score_negative_roi():
    assert wt.compute_smart_score(-0.10, -0.05, book_leverage=1.0, added_under_stress=False) == -15.0


# --------------------------- DB round-trip ---------------------------
def test_smart_score_persists_and_ranks(tmp_db):
    _perf("0xAAA", smart=30.0)
    _perf("0xBBB", smart=-5.0)
    _perf("0xCCC", smart=12.0)
    scores = db.get_latest_scores()
    order = [r["address"] for r in scores]
    assert order == ["0xaaa", "0xccc", "0xbbb"]          # ranked by smart desc
    assert scores[0]["smart_score"] == 30.0


def test_get_latest_smart_scores_lookup(tmp_db):
    _perf("0xAAA", smart=30.0)
    _perf("0xBBB", smart=-5.0)
    got = db.get_latest_smart_scores(["0xAAA", "0xBBB", "0xMISSING"])
    assert got == {"0xaaa": 30.0, "0xbbb": -5.0}


# --------------------------- confluence ranking ---------------------------
def _pos(address, coin, side, notional):
    db.save_positions(address, [{
        "coin": coin, "side": side, "size": 1.0, "notional_usd": notional,
        "entry_px": 100.0, "liq_px": 0.0, "unrealized_pnl": 0.0,
    }])


def test_confluence_ranks_by_smart_not_notional(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "WHALE_POSITION_THRESHOLD_USD", 500_000)

    # Coin SKILL: two skilled wallets, modest size.
    _pos("0xskill1", "SKILL", "long", 600_000)
    _pos("0xskill2", "SKILL", "long", 600_000)
    _perf("0xskill1", smart=30.0)
    _perf("0xskill2", smart=20.0)   # combined 50, total $1.2M

    # Coin WHALE: two huge-but-mediocre wallets.
    _pos("0xbig1", "WHALE", "long", 5_000_000)
    _pos("0xbig2", "WHALE", "long", 5_000_000)
    _perf("0xbig1", smart=1.0)
    _perf("0xbig2", smart=1.0)      # combined 2, total $10M

    groups = corr.current_wallet_confluence(window_minutes=15)
    assert len(groups) == 2
    # Despite far less notional, the skilled cluster ranks first.
    assert groups[0]["coin"] == "SKILL"
    assert groups[0]["smart"] == 50.0
    assert groups[1]["coin"] == "WHALE"
    assert groups[0]["total"] < groups[1]["total"]

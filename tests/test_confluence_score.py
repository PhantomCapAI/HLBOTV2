"""Tests for the rebuilt scanner/screener.calculate_confluence_score.

The old score saturated (~100 for everything). These assert the new score
(a) discriminates fresh vs extended setups, (b) caps ADX so a blow-off can't
dominate, (c) scales timeframe agreement, and (d) stays in [0, 100].

Run: pytest tests/test_confluence_score.py
"""
from scanner.indicators import TFRead
from scanner.screener import calculate_confluence_score


def _read(tf, *, lean, regime, adx=28.0, rsi=55.0, close=100.0, ema21=99.0,
          vwap=98.0, atr=2.0, plus_di=30.0, minus_di=10.0, macd_hist=1.0,
          structure="uptrend (HH/HL)"):
    return TFRead(
        tf=tf, close=close, ema21=ema21, ema50=ema21, ema200=ema21,
        adx=adx, plus_di=plus_di, minus_di=minus_di, rsi=rsi, macd_hist=macd_hist,
        atr=atr, bb_width=0.05, vwap=vwap, structure=structure, regime=regime,
        lean=lean, notes=[], bars=300,
    )


def _bounded(reads, funding=0.0):
    s = calculate_confluence_score(reads, funding)
    assert 0.0 <= s <= 100.0
    return s


def test_fresh_strong_long_scores_high():
    reads = {"1h": _read("1h", lean=5, regime="trend up"),
             "4h": _read("4h", lean=5, regime="trend up")}
    assert _bounded(reads) >= 75.0


def test_extended_long_scores_much_lower_than_fresh():
    fresh = {"1h": _read("1h", lean=5, regime="trend up"),
             "4h": _read("4h", lean=5, regime="trend up")}
    # Same direction/regime/agreement, but RSI extreme, stretched 6 ATR, blow-off ADX.
    extended = {"1h": _read("1h", lean=5, regime="trend up", adx=80, rsi=88,
                            close=120.0, ema21=99.0, vwap=98.0, atr=2.0),
                "4h": _read("4h", lean=5, regime="trend up", adx=80, rsi=88,
                            close=120.0, ema21=99.0, vwap=98.0, atr=2.0)}
    s_fresh, s_ext = _bounded(fresh), _bounded(extended)
    assert s_fresh - s_ext >= 25.0, (s_fresh, s_ext)


def test_adx_does_not_dominate():
    # An otherwise weak setup with a huge ADX must not score high off ADX alone.
    weak_hi_adx = {"1h": _read("1h", lean=1, regime="transition", adx=80,
                               structure="ranging / mixed", plus_di=20, minus_di=18,
                               macd_hist=-0.1),
                   "4h": _read("4h", lean=1, regime="transition", adx=80,
                               structure="ranging / mixed", plus_di=20, minus_di=18,
                               macd_hist=-0.1)}
    assert _bounded(weak_hi_adx) <= 45.0


def test_timeframe_disagreement_lowers_score():
    agree = {"1h": _read("1h", lean=5, regime="trend up"),
             "4h": _read("4h", lean=5, regime="trend up")}
    disagree = {"1h": _read("1h", lean=-5, regime="trend up"),
                "4h": _read("4h", lean=5, regime="trend up")}
    assert _bounded(agree) > _bounded(disagree)


def test_chop_no_trend_scores_low():
    reads = {"1h": _read("1h", lean=0, regime="chop", adx=12,
                         structure="ranging / mixed", plus_di=15, minus_di=15),
             "4h": _read("4h", lean=0, regime="chop", adx=12,
                         structure="ranging / mixed", plus_di=15, minus_di=15)}
    assert _bounded(reads) <= 15.0


def test_funding_against_crowd_helps_into_crowd_hurts():
    base = {"1h": _read("1h", lean=5, regime="trend up"),
            "4h": _read("4h", lean=5, regime="trend up")}
    # Long into positive (crowded long) funding = penalty; vs short crowd = bonus.
    into_crowd = calculate_confluence_score(base, funding=0.001)    # longs crowded
    against_crowd = calculate_confluence_score(base, funding=-0.001)  # shorts crowded
    assert against_crowd > into_crowd


def test_short_side_symmetry():
    reads = {"1h": _read("1h", lean=-5, regime="trend down", plus_di=10, minus_di=30,
                         macd_hist=-1.0, rsi=42, close=90.0, ema21=92.0, vwap=93.0,
                         structure="downtrend (LH/LL)"),
             "4h": _read("4h", lean=-5, regime="trend down", plus_di=10, minus_di=30,
                         macd_hist=-1.0, rsi=42, close=90.0, ema21=92.0, vwap=93.0,
                         structure="downtrend (LH/LL)")}
    assert _bounded(reads) >= 75.0

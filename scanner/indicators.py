"""Indicator engine — ported verbatim from the 5-file engine.py.

Pure functions over pandas frames; the math is unchanged from the live scanner.
Only CONFIG is now sourced from the centralized config module.
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config

CONFIG = config.CONFIG


def rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return rma(true_range(df), n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain, loss = d.clip(lower=0), -d.clip(upper=0)
    rs = rma(gain, n) / rma(loss, n).replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def macd(close: pd.Series):
    line = ema(close, 12) - ema(close, 26)
    signal = ema(line, 9)
    return line, signal, line - signal


def bollinger_width(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return ((mid + k * sd) - (mid - k * sd)) / mid


def adx(df: pd.DataFrame, n: int = 14):
    up, down = df["high"].diff(), -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr_n = rma(true_range(df), n)
    plus_di = 100 * rma(pd.Series(plus_dm, index=df.index), n) / tr_n
    minus_di = 100 * rma(pd.Series(minus_dm, index=df.index), n) / tr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return rma(dx, n).fillna(0), plus_di.fillna(0), minus_di.fillna(0)


def anchored_vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)


def swing_points(df: pd.DataFrame, k: int = 3):
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    for i in range(k, len(df) - k):
        if h[i] == max(h[i - k:i + k + 1]):
            highs.append((i, h[i]))
        if l[i] == min(l[i - k:i + k + 1]):
            lows.append((i, l[i]))
    return highs, lows


@dataclass
class TFRead:
    tf: str; close: float; ema21: float; ema50: float; ema200: float
    adx: float; plus_di: float; minus_di: float; rsi: float; macd_hist: float
    atr: float; bb_width: float; vwap: float; structure: str; regime: str
    lean: int; notes: list; bars: int


def analyse_tf(tf: str, df: pd.DataFrame) -> TFRead:
    close = df["close"].iloc[-1]
    e21 = ema(df["close"], 21).iloc[-1]
    e50 = ema(df["close"], 50).iloc[-1]
    e200 = ema(df["close"], 200).iloc[-1]
    adx_v, pdi, mdi = (x.iloc[-1] for x in adx(df))
    r = rsi(df["close"]).iloc[-1]
    _, _, hist = macd(df["close"])
    hist = hist.iloc[-1]
    a = atr(df).iloc[-1]
    bw = bollinger_width(df["close"]).iloc[-1]
    vw = anchored_vwap(df).iloc[-1]
    highs, lows = swing_points(df)
    structure = "n/a"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        structure = "uptrend (HH/HL)" if hh and hl else "downtrend (LH/LL)" if lh and ll else "ranging / mixed"
    if adx_v > 25 and pdi > mdi and close > e50:
        regime = "trend up"
    elif adx_v > 25 and mdi > pdi and close < e50:
        regime = "trend down"
    elif adx_v < 20:
        regime = "chop"
    else:
        regime = "transition"
    lean, notes = 0, []
    if close > e21 > e50:
        lean += 1; notes.append("price above 21>50 EMA (up)")
    elif close < e21 < e50:
        lean -= 1; notes.append("price below 21<50 EMA (down)")
    if pdi > mdi:
        lean += 1; notes.append("+DI > -DI")
    else:
        lean -= 1; notes.append("-DI > +DI")
    if hist > 0:
        lean += 1; notes.append("MACD hist positive")
    else:
        lean -= 1; notes.append("MACD hist negative")
    if r >= 55:
        lean += 1; notes.append(f"RSI {r:.0f} (up)")
    elif r <= 45:
        lean -= 1; notes.append(f"RSI {r:.0f} (down)")
    if close > vw:
        lean += 1; notes.append("above anchored VWAP")
    else:
        lean -= 1; notes.append("below anchored VWAP")
    return TFRead(tf, close, e21, e50, e200, adx_v, pdi, mdi, r, hist, a, bw, vw,
                  structure, regime, lean, notes, len(df))


def position_sizing(entry: float, atr_val: float):
    risk_usd = CONFIG.account_equity * CONFIG.risk_pct
    stop_dist = CONFIG.atr_stop_mult * atr_val
    size_units = risk_usd / stop_dist if stop_dist else 0.0
    return risk_usd, stop_dist, size_units, size_units * entry

"""Open-interest trend + funding crowding (market-context only).

The bot already fetches OI and funding per coin but used OI only as a static
liquidity floor. This adds the missing half of the order-flow read: how OI is
*changing*, combined with price direction and funding sign, classified with the
standard framework:

    rising OI + rising price + elevated +funding  -> crowded longs  (squeeze ↓)
    rising OI + falling price + elevated -funding  -> crowded shorts (squeeze ↑)
    falling OI                                      -> unwind (trend losing fuel)
    flat / low OI                                   -> neutral

This is context only — no order book / trade streaming (that's the later CVD
task). All pure functions here; persistence lives in storage.database and the
wiring in scanner.screener.
"""
from __future__ import annotations

import config
from storage import database as db


def _pct_change(now: float, then: float | None) -> float | None:
    if then is None or then == 0:
        return None
    return (now - then) / then * 100.0


def classify_crowding(doi_pct: float | None, price_change_pct: float | None,
                      funding: float, *, rise_pct: float, fall_pct: float,
                      funding_threshold: float) -> str:
    """Pure crowding state from ΔOI%, price change %, and funding (per-hr rate).

    'Elevated funding' = abs(funding) > funding_threshold (reuse
    EXTREME_FUNDING_HR at the call site so it's never duplicated).
    """
    if doi_pct is None:
        return "neutral"                      # no OI history yet
    if doi_pct <= -fall_pct:
        return "unwind"                       # positions closing, trend de-fueling
    if doi_pct >= rise_pct:
        if price_change_pct is not None and price_change_pct > 0 and funding > funding_threshold:
            return "crowded_long"
        if price_change_pct is not None and price_change_pct < 0 and funding < -funding_threshold:
            return "crowded_short"
    return "neutral"


def crowding_modifier(state: str | None, direction: int) -> tuple[float, bool]:
    """Score modifier from a crowding state, given the setup's direction
    (1 long / -1 short / 0 none).

    Returns (points, supersede_funding). For crowded states the funding sign is
    *part of* the classification, so the modifier SUPERSEDES the plain
    funding-vs-crowd term (no double counting). 'unwind' is OI-only and stacks
    with the plain funding term.

      crowded_long : join the fragile crowd (long) -12 | fade for the squeeze +8
      crowded_short: join the fragile crowd (short) -12 | fade for the squeeze +8
      unwind       : trend losing fuel -> -5 (lower conviction), funding still applies
      neutral      : 0, funding term applies as usual
    """
    if state == "crowded_long":
        if direction > 0:
            return -12.0, True
        if direction < 0:
            return 8.0, True
        return 0.0, True
    if state == "crowded_short":
        if direction < 0:
            return -12.0, True
        if direction > 0:
            return 8.0, True
        return 0.0, True
    if state == "unwind":
        return -5.0, False
    return 0.0, False


def oi_context(coin: str, oi_usd: float, funding: float, mark_px: float, *,
               funding_threshold: float, persist: bool = True) -> dict:
    """Snapshot OI and compute the crowding context for a coin.

    Stores (coin, oi_usd, funding, mark_px, now) then derives ΔOI% + price change
    over the configured short/long windows. The longer window drives the state
    when it has history, else the shorter one. Returns a dict for scoring + display.
    """
    if persist:
        db.save_oi_snapshot(coin, oi_usd, funding, mark_px)

    short_ago = db.get_oi_ago(coin, config.OI_LOOKBACK_SHORT_MIN)
    long_ago = db.get_oi_ago(coin, config.OI_LOOKBACK_LONG_MIN)

    doi_short = _pct_change(oi_usd, short_ago["oi_usd"] if short_ago else None)
    doi_long = _pct_change(oi_usd, long_ago["oi_usd"] if long_ago else None)
    px_short = _pct_change(mark_px, short_ago["mark_px"] if short_ago else None)
    px_long = _pct_change(mark_px, long_ago["mark_px"] if long_ago else None)

    if long_ago is not None:
        doi_primary, px_primary, window_min = doi_long, px_long, config.OI_LOOKBACK_LONG_MIN
    else:
        doi_primary, px_primary, window_min = doi_short, px_short, config.OI_LOOKBACK_SHORT_MIN

    state = classify_crowding(
        doi_primary, px_primary, funding,
        rise_pct=config.OI_RISE_PCT, fall_pct=config.OI_FALL_PCT,
        funding_threshold=funding_threshold,
    )
    return {
        "state": state,
        "doi_short": doi_short,
        "doi_long": doi_long,
        "doi_primary": doi_primary,
        "price_primary": px_primary,
        "window_min": window_min,
        "funding": funding,
        "oi_usd": oi_usd,
    }


_STATE_LABEL = {
    "crowded_long": "crowded longs — squeeze risk ↓",
    "crowded_short": "crowded shorts — squeeze risk ↑",
    "unwind": "OI unwinding — trend losing fuel",
    "neutral": "neutral flow",
}


def flow_line(ctx: dict) -> str:
    """One-line OI/funding context, e.g.
    'OI +18% 4h · funding +0.04%/hr · crowded longs — squeeze risk ↓'."""
    if not ctx:
        return ""
    doi = ctx.get("doi_primary")
    window_h = (ctx.get("window_min") or 0) / 60.0
    win = f"{window_h:.0f}h" if window_h >= 1 else f"{ctx.get('window_min', 0)}m"
    oi_part = f"OI {doi:+.0f}% {win}" if doi is not None else "OI —"
    fund_part = f"funding {ctx.get('funding', 0.0) * 100:+.4f}%/hr"
    label = _STATE_LABEL.get(ctx.get("state", "neutral"), "neutral flow")
    return f"📡 {oi_part} · {fund_part} · {label}"

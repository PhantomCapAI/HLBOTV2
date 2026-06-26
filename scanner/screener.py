"""All-pairs coin screener (read-only) — async port of the 5-file screen.py.

Two passes:
  run_scan()  - liquidity filter from one metaAndAssetCtxs call, light 1h/4h
                fetch per survivor, confluence score, ranked shortlist.
  deep_dive() - full fetch (all TFs + order book microstructure) on the top N,
                attaching entry/stop/target/sizing numbers for the Grok layer.
"""
from __future__ import annotations
import logging

import config
from integrations import hyperliquid as hl
from scanner.indicators import analyse_tf, position_sizing

log = logging.getLogger(__name__)

MIN_VOLUME = 5_000_000        # 24h notional floor (USD)
MIN_OI_USD = 1_000_000        # open-interest floor (USD notional)
SHORTLIST_SIZE = 8
SCAN_TFS = ("1h", "4h")
EXTREME_FUNDING_HR = 0.0003   # ~0.03%/hr; above this the funded side is "crowded"


def suggest_leverage(entry: float, atr: float, score: float, max_lev: int) -> int:
    """Volatility- and conviction-aware leverage, clamped to the asset's max.

    Calmer asset (low ATR%) and higher confluence score -> more leverage.
    Never exceeds the asset's on-chain maxLeverage or a hard 25x ceiling.
    """
    if entry <= 0 or atr <= 0:
        return min(3, max_lev)
    atr_pct = (atr / entry) * 100
    vol_lev = max(2.0, min(20.0, 10.0 / atr_pct))      # ~1% ATR -> 10x, ~5% -> 2x
    conviction = max(0.5, min(1.25, score / 80.0))      # score 80 -> x1.0
    lev = vol_lev * conviction
    return int(max(1, min(round(lev), max_lev, 25)))


async def _dexs_to_scan() -> list[str]:
    """Which builder dexs to include. [] = crypto only (default)."""
    if not getattr(config, "ENABLE_BUILDER_DEXS", False):
        return []
    if config.BUILDER_DEXS:
        return config.BUILDER_DEXS
    try:
        found = await hl.get_perp_dexs()
        log.info("Builder dexs discovered: %s", found)
        return found
    except Exception as e:
        log.warning("perpDexs discovery failed: %s", e)
        return []


def calculate_confluence_score(reads: dict, funding: float = 0.0) -> float:
    """Deterministic 0-100 setup quality score, built to *discriminate*.

    The old version saturated (almost everything ~100) because ADX scaled up to
    30pts and timeframe agreement was a flat +30 — so the most extended trends
    scored highest. This rebuild caps trend-existence terms, scales agreement by
    breadth+strength, and subtracts an extension/exhaustion guard so a strong-
    but-fresh setup outscores a strong-but-extended one.

    Direction is taken from the 4h lean. Positive contributions (max):
        regime              25   4h regime confirms a directional trend
        tf_agreement        25   scaled by how many TFs agree AND how strongly
        adx_confirm         10   HARD cap — confirms a trend exists, never scales up
        momentum            10   +DI/-DI (5) and MACD hist (5) agree with direction
        structure           10   4h swing structure matches the direction
        funding (vs crowd)   5   positioned against an overcrowded funded side
    Penalties (the extension / exhaustion guard), subtracted:
        rsi_extreme        -20   entering into an RSI extreme against the move
        price_stretch      -15   price stretched from EMA21 *and* VWAP (ATR terms)
        adx_exhaustion     -10   blow-off ADX (>45) = late/extended entry
        funding_crowded    -10   entering the overcrowded funded side
    Net is clamped to [0, 100]. A fresh, multi-TF-aligned, non-extended trend
    tops out near 85; extended or conflicted setups land materially lower.
    """
    primary = reads.get("4h") or reads[sorted(reads)[-1]]
    direction = 1 if primary.lean > 0 else -1 if primary.lean < 0 else 0

    # 1. Regime (max 25): a confirmed directional regime, not magnitude.
    if primary.regime in ("trend up", "trend down"):
        regime_pts = 25.0
    elif primary.regime == "transition":
        regime_pts = 10.0
    else:  # chop
        regime_pts = 0.0

    # 2. Timeframe agreement (max 25): scales with breadth AND strength. Each
    # read's lean is in [-5, 5]; aligned strength = lean * direction, summed and
    # normalized. Two TFs both maxed-and-aligned -> 25; partial/opposing -> less.
    if direction != 0 and reads:
        frac = sum(r.lean * direction for r in reads.values()) / (len(reads) * 5.0)
        tf_pts = max(0.0, frac) * 25.0
    else:
        tf_pts = 0.0

    # 3. ADX trend confirmation (HARD cap 10): presence of a trend, not its age.
    if primary.adx >= 25:
        adx_pts = 10.0
    elif primary.adx >= 20:
        adx_pts = 5.0
    else:
        adx_pts = 0.0

    # 4. Momentum agreement (max 10): DI cross (5) + MACD hist sign (5).
    momentum_pts = 0.0
    if direction != 0:
        di_dir = 1 if primary.plus_di > primary.minus_di else -1
        if di_dir == direction:
            momentum_pts += 5.0
        if (primary.macd_hist > 0) == (direction > 0):
            momentum_pts += 5.0

    # 5. Structure (max 10): 4h swing structure matches the direction.
    structure_pts = 0.0
    if direction > 0 and primary.structure.startswith("uptrend"):
        structure_pts = 10.0
    elif direction < 0 and primary.structure.startswith("downtrend"):
        structure_pts = 10.0

    # 6. Funding vs crowd (+5 against the funded side, -10 into it).
    funding_pts = 0.0
    if abs(funding) > EXTREME_FUNDING_HR and direction != 0:
        crowded = 1 if funding > 0 else -1
        funding_pts = -10.0 if direction == crowded else 5.0

    # 7. RSI extreme against the entry (penalty up to -20): longing into >70 or
    # shorting into <30 is chasing an extended move.
    rsi_pen = 0.0
    if direction > 0 and primary.rsi > 70:
        rsi_pen = -min((primary.rsi - 70) / 30.0, 1.0) * 20.0
    elif direction < 0 and primary.rsi < 30:
        rsi_pen = -min((30.0 - primary.rsi) / 30.0, 1.0) * 20.0

    # 8. Price stretch (penalty up to -15): how far price has run in the trade's
    # direction beyond BOTH anchors (EMA21 and anchored VWAP), in ATR. Using the
    # *nearer* anchor means a fresh pullback to EMA21 isn't punished, but a price
    # extended from every reference is. Kicks in past 2 ATR, maxes at 6 ATR.
    stretch_pen = 0.0
    if direction != 0 and primary.atr > 0:
        stretch = min(
            direction * (primary.close - primary.ema21) / primary.atr,
            direction * (primary.close - primary.vwap) / primary.atr,
        )
        if stretch > 2.0:
            stretch_pen = -min((stretch - 2.0) / 4.0, 1.0) * 15.0

    # 9. ADX exhaustion (penalty up to -10): a blow-off (ADX>45) is late — this
    # is what kept the old score from rewarding the snapback risk.
    adx_pen = 0.0
    if primary.adx > 45:
        adx_pen = -min((primary.adx - 45) / 35.0, 1.0) * 10.0

    score = (regime_pts + tf_pts + adx_pts + momentum_pts + structure_pts
             + funding_pts + rsi_pen + stretch_pen + adx_pen)
    return round(max(0.0, min(100.0, score)), 1)


async def _fetch_screen(coin: str, timeframes=SCAN_TFS) -> dict:
    return {tf: await hl.candles_df(coin, tf) for tf in timeframes}


async def run_scan() -> list[dict]:
    dexs = await _dexs_to_scan()
    meta, ctxs = await hl.get_all_meta_and_ctxs(dexs) if dexs else await hl.get_meta_and_ctxs()
    candidates = []
    for i, asset in enumerate(meta):
        coin, ctx = asset["name"], ctxs[i]
        is_builder = ":" in coin
        min_vol = config.BUILDER_MIN_VOLUME if is_builder else MIN_VOLUME
        min_oi = config.BUILDER_MIN_OI if is_builder else MIN_OI_USD
        day_vol = float(ctx.get("dayNtlVlm") or 0)
        mark = float(ctx.get("markPx") or 0)
        oi_usd = float(ctx.get("openInterest") or 0) * mark
        if day_vol < min_vol or oi_usd < min_oi:
            continue
        candidates.append((coin, ctx))

    log.info("%s pairs pass the liquidity filter; scanning...", len(candidates))
    discoveries, errors = [], 0
    for coin, ctx in candidates:
        try:
            frames = await _fetch_screen(coin, SCAN_TFS)
            reads = {tf: analyse_tf(tf, frames[tf]) for tf in SCAN_TFS}
            discoveries.append({
                "coin": coin,
                "score": calculate_confluence_score(reads, float(ctx.get("funding") or 0)),
                "regime_4h": reads["4h"].regime,
                "lean_4h": reads["4h"].lean,
                "adx_4h": round(reads["4h"].adx, 1),
                "direction": "long" if reads["4h"].lean > 0 else "short" if reads["4h"].lean < 0 else "none",
                "funding": float(ctx.get("funding") or 0),
                "oi_usd": float(ctx.get("openInterest") or 0) * float(ctx.get("markPx") or 0),
            })
        except Exception as e:
            errors += 1
            log.warning("  skip %s: %s: %s", coin, type(e).__name__, e)
    if errors:
        log.info("%s pair(s) skipped due to errors.", errors)
    discoveries.sort(key=lambda x: x["score"], reverse=True)
    return discoveries[:SHORTLIST_SIZE]


async def _fetch_live(coin: str, ctx: dict) -> tuple[dict, dict]:
    frames = {tf: await hl.candles_df(coin, tf) for tf in config.CONFIG.timeframes}
    book = await hl.get_l2_book(coin)
    levels = book.get("levels", [[], []])
    bids = levels[0] if len(levels) > 0 else []
    asks = levels[1] if len(levels) > 1 else []
    bid_sz = sum(float(x["sz"]) for x in bids[:10])
    ask_sz = sum(float(x["sz"]) for x in asks[:10])
    micro = {
        "mark": float(ctx.get("markPx") or 0),
        "funding": float(ctx.get("funding") or 0),
        "open_interest": float(ctx.get("openInterest") or 0),
        "day_volume": float(ctx.get("dayNtlVlm") or 0),
        "book_imbalance": (bid_sz - ask_sz) / (bid_sz + ask_sz) if (bid_sz + ask_sz) else 0.0,
    }
    return frames, micro


async def deep_dive(discoveries: list[dict]) -> list[dict]:
    if not discoveries:
        return []
    dexs = await _dexs_to_scan()
    meta, ctxs = await hl.get_all_meta_and_ctxs(dexs) if dexs else await hl.get_meta_and_ctxs()
    ctx_by_name = {asset["name"]: ctxs[i] for i, asset in enumerate(meta)}
    maxlev_by_name = {asset["name"]: int(asset.get("maxLeverage", 3) or 3) for asset in meta}
    enriched = []
    for d in discoveries:
        coin = d["coin"]
        ctx = ctx_by_name.get(coin)
        if ctx is None:
            log.warning("  deep-dive: %s not in perp universe", coin)
            continue
        try:
            frames, micro = await _fetch_live(coin, ctx)
            reads = {tf: analyse_tf(tf, frames[tf]) for tf in config.CONFIG.timeframes}
            p = reads[config.CONFIG.timeframes[-1]]
            entry = p.close
            risk_usd, stop_dist, units, notional = position_sizing(entry, p.atr)
            side = -1 if p.lean < 0 else 1
            lev = suggest_leverage(entry, p.atr, d["score"], maxlev_by_name.get(coin, 3))
            d2 = dict(d)
            d2.update({
                "micro": micro,
                "entry": round(entry, 6),
                "stop": round(entry - side * stop_dist, 6),
                "targets": [round(entry + side * m * stop_dist, 6) for m in (2, 3, 4)],
                "atr": round(p.atr, 6),
                "risk_usd": round(risk_usd, 2),
                "size_units": round(units, 6),
                "notional": round(notional, 2),
                "leverage_set": lev,
                "structure_4h": p.structure,
                "notes_4h": p.notes,
            })
            enriched.append(d2)
        except Exception as e:
            log.warning("  deep-dive failed for %s: %s: %s", coin, type(e).__name__, e)
    return enriched


async def scan_one(symbol: str) -> dict | None:
    """Score a single requested coin directly (for /coin), across all scanned dexs.

    Searches the FULL universe (not just the top-N shortlist) and tolerates
    user input like "$TSLA", "tsla", or "BTC-PERP". Returns None only if the
    symbol genuinely isn't listed or has no usable candle history.
    """
    symbol = symbol.upper().lstrip("$").replace("-PERP", "").strip()
    dexs = await _dexs_to_scan()
    meta, ctxs = await hl.get_all_meta_and_ctxs(dexs) if dexs else await hl.get_meta_and_ctxs()

    def bare(n: str) -> str:
        return n.split(":", 1)[1] if ":" in n else n

    idx = next(
        (i for i, a in enumerate(meta)
         if a["name"].upper() == symbol or bare(a["name"]).upper() == symbol),
        None,
    )
    if idx is None:  # substring fallback (e.g. "PEPE" -> "kPEPE")
        idx = next((i for i, a in enumerate(meta) if symbol in a["name"].upper()), None)
    if idx is None:
        return None

    coin, ctx = meta[idx]["name"], ctxs[idx]
    try:
        frames = await _fetch_screen(coin, SCAN_TFS)
        reads = {tf: analyse_tf(tf, frames[tf]) for tf in SCAN_TFS}
    except Exception as e:
        log.warning("scan_one failed for %s: %s", coin, e)
        return None

    return {
        "coin": coin,
        "score": calculate_confluence_score(reads, float(ctx.get("funding") or 0)),
        "regime_4h": reads["4h"].regime,
        "lean_4h": reads["4h"].lean,
        "adx_4h": round(reads["4h"].adx, 1),
        "direction": "long" if reads["4h"].lean > 0 else "short" if reads["4h"].lean < 0 else "none",
        "funding": float(ctx.get("funding") or 0),
        "oi_usd": float(ctx.get("openInterest") or 0) * float(ctx.get("markPx") or 0),
    }

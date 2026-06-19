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


def calculate_confluence_score(reads: dict, funding: float = 0.0) -> float:
    primary = reads["4h"]
    score = 0.0
    if primary.regime in ("trend up", "trend down"):
        score += 40
    elif primary.regime == "transition":
        score += 15
    signs = {1 if r.lean > 0 else -1 if r.lean < 0 else 0 for r in reads.values()}
    if len(signs) == 1 and 0 not in signs:
        score += 30
    score += min(primary.adx, 50) * 0.6
    direction = 1 if primary.lean > 0 else -1 if primary.lean < 0 else 0
    if abs(funding) > EXTREME_FUNDING_HR and direction != 0:
        crowded = 1 if funding > 0 else -1
        score += -15 if direction == crowded else 5
    return round(score, 1)


async def _fetch_screen(coin: str, timeframes=SCAN_TFS) -> dict:
    return {tf: await hl.candles_df(coin, tf) for tf in timeframes}


async def run_scan() -> list[dict]:
    meta, ctxs = await hl.get_meta_and_ctxs()
    candidates = []
    for i, asset in enumerate(meta):
        coin, ctx = asset["name"], ctxs[i]
        day_vol = float(ctx.get("dayNtlVlm") or 0)
        mark = float(ctx.get("markPx") or 0)
        oi_usd = float(ctx.get("openInterest") or 0) * mark
        if day_vol < MIN_VOLUME or oi_usd < MIN_OI_USD:
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
    meta, ctxs = await hl.get_meta_and_ctxs()
    ctx_by_name = {asset["name"]: ctxs[i] for i, asset in enumerate(meta)}
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
            lev = config.CONFIG.leverage.get(coin, config.CONFIG.leverage["default"])
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

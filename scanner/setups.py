"""Coin -> setup pipeline. Orchestrates screener + Grok into ready-to-send setups.

Mirrors the behavior of the live bot's /scan and /coin commands.
"""
import logging

from integrations import grok
from scanner.screener import run_scan, deep_dive, scan_one

log = logging.getLogger(__name__)


async def coin_scan(deep_limit: int = 6) -> list[dict]:
    """Full scan -> shortlist -> deep dive -> Grok setups (matches /scan)."""
    discoveries = await run_scan()
    if not discoveries:
        return []
    good = [d for d in discoveries if d.get("score", 0) >= 35]
    if not good:
        good = discoveries[:5]
    enriched = await deep_dive(good[:deep_limit])
    return await grok.generate_setups(enriched)


async def deep_dive_symbol(symbol: str) -> list[dict]:
    """Deep dive on one symbol (matches /coin SYMBOL).

    Scores the requested coin directly from the full universe via scan_one,
    so it works for any liquid market, not just today's top-N shortlist.
    """
    disc = await scan_one(symbol)
    if disc is None:
        return []
    enriched = await deep_dive([disc])
    return await grok.generate_setups(enriched)

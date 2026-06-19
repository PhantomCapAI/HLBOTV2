"""Single async Hyperliquid /info client for the whole system.

Merges the repo scanner's aiohttp client (paced + exponential backoff on
429/5xx) with the data needs of the coin scanner (meta+ctxs, candle frames,
order book) and adds the weight-budget limiter ported from the 5-file scanner.
Read-only: no keys, no signing, no orders.
"""
import asyncio
import logging
import random
import time
from typing import Any

import aiohttp
import pandas as pd

import config
from core.rate_limiter import LIMITER, weight_for

log = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000,
}

_info_lock = asyncio.Lock()
_last_info_request_at = 0.0


def _client_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=config.HL_HTTP_TIMEOUT_SECONDS)


def _retry_after_seconds(headers) -> float | None:
    if not headers:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def _retry_wait(retry_after: float | None, delay: float) -> float:
    if retry_after is not None:
        return retry_after
    return delay + random.uniform(0, delay * 0.3)


async def _paced_post(session: aiohttp.ClientSession, payload: dict) -> Any:
    """Weight-budget + min-interval paced POST to /info."""
    global _last_info_request_at
    await LIMITER.acquire(weight_for(payload))
    async with _info_lock:
        elapsed = time.monotonic() - _last_info_request_at
        min_interval = max(config.HL_INFO_MIN_REQUEST_INTERVAL_SECONDS, 0.0)
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        async with session.post(HL_INFO_URL, json=payload) as resp:
            _last_info_request_at = time.monotonic()
            if resp.status in RETRYABLE_STATUSES:
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history, status=resp.status,
                    message=resp.reason, headers=resp.headers,
                ) from None
            resp.raise_for_status()
            return await resp.json()


async def _post_with_backoff(session: aiohttp.ClientSession, payload: dict, max_retries: int | None = None) -> Any:
    if max_retries is None:
        max_retries = config.HL_INFO_MAX_RETRIES
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        retry_after = None
        try:
            return await _paced_post(session, payload)
        except aiohttp.ClientResponseError as e:
            last_exc = e
            if e.status not in RETRYABLE_STATUSES or attempt >= max_retries:
                raise
            retry_after = _retry_after_seconds(e.headers)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
            if attempt >= max_retries:
                raise
        wait = _retry_wait(retry_after, delay)
        log.warning("HL /info retry %s/%s in %.1fs after %s",
                    attempt + 1, max_retries, wait, type(last_exc).__name__)
        await asyncio.sleep(wait)
        delay = min(delay * 2, 30.0)
    raise last_exc  # type: ignore[misc]


async def _get_json_with_backoff(session: aiohttp.ClientSession, url: str, max_retries: int | None = None) -> Any:
    if max_retries is None:
        max_retries = config.HL_INFO_MAX_RETRIES
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        retry_after = None
        try:
            async with session.get(url) as resp:
                if resp.status in RETRYABLE_STATUSES:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status,
                        message=resp.reason, headers=resp.headers,
                    ) from None
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientResponseError as e:
            last_exc = e
            if e.status not in RETRYABLE_STATUSES or attempt >= max_retries:
                raise
            retry_after = _retry_after_seconds(e.headers)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
            if attempt >= max_retries:
                raise
        wait = _retry_wait(retry_after, delay)
        log.warning("HL leaderboard retry %s/%s in %.1fs after %s",
                    attempt + 1, max_retries, wait, type(last_exc).__name__)
        await asyncio.sleep(wait)
        delay = min(delay * 2, 30.0)
    raise last_exc  # type: ignore[misc]


# --------------------------- wallet-side endpoints ---------------------------
async def get_leaderboard(top_n: int = 100) -> list[dict]:
    async with aiohttp.ClientSession(timeout=_client_timeout()) as session:
        data = await _get_json_with_backoff(session, HL_LEADERBOARD_URL)
    return data.get("leaderboardRows", [])[:top_n]


async def get_positions(address: str) -> dict:
    async with aiohttp.ClientSession(timeout=_client_timeout()) as session:
        return await _post_with_backoff(session, {"type": "clearinghouseState", "user": address})


async def fetch_all_positions(addresses: list[str]) -> dict[str, Any]:
    async with aiohttp.ClientSession(timeout=_client_timeout()) as session:
        results = await asyncio.gather(
            *[_post_with_backoff(session, {"type": "clearinghouseState", "user": a}) for a in addresses],
            return_exceptions=True,
        )
    out: dict[str, Any] = {}
    for addr, result in zip(addresses, results):
        if isinstance(result, Exception):
            log.warning("Failed to fetch positions for %s: %s", addr[:10], result)
        else:
            out[addr] = result
    return out


async def get_funding_and_oi() -> list[dict]:
    """Parsed funding/OI list (wallet cycle)."""
    async with aiohttp.ClientSession(timeout=_client_timeout()) as session:
        data = await _post_with_backoff(session, {"type": "metaAndAssetCtxs"})
    meta, ctxs = data[0]["universe"], data[1]
    out = []
    for asset, ctx in zip(meta, ctxs):
        out.append({
            "name": asset["name"],
            "funding": float(ctx.get("funding", 0) or 0),
            "open_interest": float(ctx.get("openInterest", 0) or 0),
            "mark_px": float(ctx.get("markPx", 0) or 0),
            "day_volume": float(ctx.get("dayNtlVlm", 0) or 0),
        })
    return out


# --------------------------- coin-scanner endpoints ---------------------------
async def get_meta_and_ctxs() -> tuple[list, list]:
    """Raw (universe, ctxs) for the coin scanner's liquidity filter."""
    async with aiohttp.ClientSession(timeout=_client_timeout()) as session:
        data = await _post_with_backoff(session, {"type": "metaAndAssetCtxs"})
    return data[0]["universe"], data[1]


async def get_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    async with aiohttp.ClientSession(timeout=_client_timeout()) as session:
        return await _post_with_backoff(session, {
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        })


async def candles_df(coin: str, interval: str, lookback_bars: int | None = None) -> pd.DataFrame:
    lookback = config.CONFIG.lookback_bars if lookback_bars is None else lookback_bars
    end = int(time.time() * 1000)
    start = end - INTERVAL_MS[interval] * (lookback + 5)
    raw = await get_candles(coin, interval, start, end)
    if not raw:
        raise ValueError(f"No candles for {coin} {interval}")
    df = pd.DataFrame(raw).rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    if "t" in df.columns:
        df = df.sort_values("t")
    return df.reset_index(drop=True)


async def get_l2_book(coin: str) -> dict:
    async with aiohttp.ClientSession(timeout=_client_timeout()) as session:
        return await _post_with_backoff(session, {"type": "l2Book", "coin": coin})

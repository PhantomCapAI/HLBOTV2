"""Async rolling-window weight limiter for the Hyperliquid /info endpoint.

Async port of the 5-file scanner's WeightLimiter. Hyperliquid /info shares a
~1200 weight/min budget; we stay under it with headroom.
"""
import asyncio
import time
from collections import deque

import config

# Documented /info weights; unknown types default to 20 (the common case).
REQ_WEIGHTS = {
    "l2Book": 2,
    "allMids": 2,
    "clearinghouseState": 2,
    "metaAndAssetCtxs": 20,
    "candleSnapshot": 20,
}


def weight_for(body: dict) -> int:
    return REQ_WEIGHTS.get(body.get("type"), 20)


class AsyncWeightLimiter:
    def __init__(self, budget=None, window=None, headroom=None):
        budget = config.HL_WEIGHT_BUDGET if budget is None else budget
        window = config.HL_WEIGHT_WINDOW_SECONDS if window is None else window
        headroom = config.HL_WEIGHT_HEADROOM if headroom is None else headroom
        self.cap = budget * headroom
        self.window = window
        self.events: deque = deque()
        self.lock = asyncio.Lock()

    async def acquire(self, weight: int) -> None:
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0][0] > self.window:
                    self.events.popleft()
                used = sum(w for _, w in self.events)
                if used + weight <= self.cap:
                    self.events.append((now, weight))
                    return
                wait = self.window - (now - self.events[0][0]) + 0.05
            await asyncio.sleep(max(wait, 0.05))


LIMITER = AsyncWeightLimiter()

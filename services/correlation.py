"""Correlation: wallet positioning x technical setups.

The payoff of the merge. current_wallet_confluence() summarizes how tracked
wallets are currently positioned; find_confluence() flags coins where a strong
technical setup lines up with multiple whales on the same side.
"""
import logging

import config
from storage import database as db

log = logging.getLogger(__name__)


def current_wallet_confluence(window_minutes: int = 15, min_notional: float | None = None) -> list[dict]:
    min_notional = config.WHALE_POSITION_THRESHOLD_USD if min_notional is None else min_notional
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT coin, side, COUNT(*) AS cnt, SUM(notional_usd) AS tot FROM (
                    SELECT ps.coin, ps.side, ps.address, ps.notional_usd
                    FROM position_snapshots ps
                    INNER JOIN (
                        SELECT address, MAX(snapshot_at) AS latest
                        FROM position_snapshots
                        WHERE snapshot_at > datetime('now', '-{int(window_minutes)} minutes')
                        GROUP BY address
                    ) m ON ps.address = m.address AND ps.snapshot_at = m.latest
                    WHERE ps.notional_usd >= ?
                ) GROUP BY coin, side
                ORDER BY cnt DESC, tot DESC""",
            (min_notional,),
        ).fetchall()
    return [
        {"coin": r["coin"], "side": r["side"], "count": r["cnt"], "total": float(r["tot"] or 0)}
        for r in rows
    ]


def find_confluence(setups: list[dict]) -> list[dict]:
    if not setups:
        return []
    conf = {(c["coin"], c["side"]): c for c in current_wallet_confluence()}
    out = []
    for s in setups:
        coin = s.get("coin")
        inner = (s.get("setups") or [{}])[0]
        direction = (inner.get("direction") or s.get("direction") or "long").lower()
        side = "long" if direction == "long" else "short"
        score = s.get("score", 0)
        if score < config.CORRELATION_MIN_SCORE:
            continue
        w = conf.get((coin, side))
        if not w or w["count"] < config.CORRELATION_MIN_WHALES:
            continue
        out.append({
            "coin": coin, "side": side, "score": score,
            "whales": w["count"], "total_notional": w["total"], "setup": s,
        })
    return out

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
    """Aggregate current tracked-wallet positioning per (coin, side).

    Ranked by wallet count, then by combined smart_score (skill) rather than
    combined notional — so a cluster of skilled wallets outranks a cluster of
    merely large ones.
    """
    min_notional = config.WHALE_POSITION_THRESHOLD_USD if min_notional is None else min_notional
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT ps.coin, ps.side, ps.address, ps.notional_usd
                FROM position_snapshots ps
                INNER JOIN (
                    SELECT address, MAX(snapshot_at) AS latest
                    FROM position_snapshots
                    WHERE snapshot_at > datetime('now', '-{int(window_minutes)} minutes')
                    GROUP BY address
                ) m ON ps.address = m.address AND ps.snapshot_at = m.latest
                WHERE ps.notional_usd >= ?""",
            (min_notional,),
        ).fetchall()

    smart_scores = db.get_latest_smart_scores([r["address"] for r in rows])
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        g = groups.setdefault(
            (r["coin"], r["side"]),
            {"coin": r["coin"], "side": r["side"], "count": 0, "total": 0.0, "smart": 0.0},
        )
        g["count"] += 1
        g["total"] += float(r["notional_usd"] or 0)
        g["smart"] += smart_scores.get((r["address"] or "").lower(), 0.0)

    out = list(groups.values())
    for g in out:
        g["smart"] = round(g["smart"], 1)
    out.sort(key=lambda g: (g["count"], g["smart"]), reverse=True)
    return out


def _setup_directions(s: dict) -> set[str]:
    """All directions a setup expresses — across every inner setup, not just the
    first — plus any top-level direction. Robust to multiple setups per coin."""
    directions: set[str] = set()
    for inner in (s.get("setups") or []):
        d = str(inner.get("direction") or "").lower()
        if d in ("long", "short"):
            directions.add(d)
    top = str(s.get("direction") or "").lower()
    if top in ("long", "short"):
        directions.add(top)
    return directions


def find_confluence(setups: list[dict]) -> list[dict]:
    if not setups:
        return []
    conf = {(c["coin"], c["side"]): c for c in current_wallet_confluence()}
    out = []
    seen: set[tuple] = set()
    for s in setups:
        coin = s.get("coin")
        score = s.get("score", 0)
        if score < config.CORRELATION_MIN_SCORE:
            continue
        # A coin can carry multiple setups / directions — check each against
        # whale positioning, not just setups[0].
        for side in _setup_directions(s):
            w = conf.get((coin, side))
            if not w or w["count"] < config.CORRELATION_MIN_WHALES:
                continue
            key = (coin, side)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "coin": coin, "side": side, "score": score,
                "whales": w["count"], "total_notional": w["total"],
                "smart": w.get("smart", 0.0), "setup": s,
            })
    # Rank confluence matches by combined smart_score (skill), not notional.
    out.sort(key=lambda m: m["smart"], reverse=True)
    return out

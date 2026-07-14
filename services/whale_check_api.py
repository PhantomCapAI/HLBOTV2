"""Read-only internal HTTP endpoint that powers the Bankr x402 `whale-check`
product — a NEW agent-facing surface, fully isolated from the Telegram push
stream.

What it is
----------
A single GET route that returns ONE point-in-time whale/wallet-health reading
for a `target` (a Hyperliquid wallet address, or a coin/market symbol). It only
READS already-persisted snapshots — the same rows the scanner writes every cycle
— and derives a reading from them. It never writes, never sends a Telegram
message, never touches alert/cooldown state. Calling it has zero effect on the
live alert/push stream.

Why it lives inside the bot process
-----------------------------------
The scoring data (wallet health/smart scores, whale positioning, OI history)
lives in the SQLite DB the scanner populates on the Zeabur persistent volume.
Running this route in the same process is the cheapest way to read that data
point-in-time. It is started ONLY when WHALE_CHECK_API_ENABLED=true AND an
internal key is set (see config.settings). Merging this module changes nothing
about the running bot until you explicitly opt in.

Wiring to Bankr
---------------
The Bankr x402 Cloud handler (TypeScript, hosted by Bankr) does:
    GET http://<this-host>:<port>/whale-check?target=...&chain=base
    header: X-Internal-Key: <WHALE_CHECK_API_KEY>
On unknown/stale target this returns 4xx with an error body; the Bankr handler
MUST throw on any non-200 so Bankr's settle-after-response never bills the
caller for a stub. See x402/whale-check.ts.

Security
--------
Gated by a shared secret (WHALE_CHECK_API_KEY) compared in constant time. Bind
to loopback / private network on Zeabur; never expose it publicly (Bankr is the
only intended caller, over the internal key).
"""
from __future__ import annotations

import hmac
import logging
import re
from datetime import datetime, timezone

from aiohttp import web

import config
from core import identity
from storage import database as db

log = logging.getLogger(__name__)

_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# --- tunable mapping knobs (safe defaults; tune to taste — see x402/README.md) ---
_MIN_WHALE_NOTIONAL = config.WHALE_POSITION_THRESHOLD_USD  # reuse the whale floor
_CONFLUENCE_WINDOW_MIN = 15          # how recent a position snapshot counts as "current"
_NETFLOW_EXPOSURE_DELTA_PCT = 5.0    # wallet exposure move that flips accumulating/distributing
_NETFLOW_OI_DELTA_PCT = 3.0          # coin OI move that reinforces accumulating/distributing


# --------------------------- freshness ---------------------------
def _parse_snapshot_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # stored via sqlite datetime('now') -> 'YYYY-MM-DD HH:MM:SS' (UTC)
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_fresh(raw: str | None) -> bool:
    ts = _parse_snapshot_at(raw)
    if ts is None:
        return False
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    return age_min <= config.WHALE_CHECK_FRESHNESS_MINUTES


# --------------------------- read helpers (all read-only) ---------------------------
def _recent_exposures(address: str, limit: int = 2) -> list[float]:
    """The N most-recent exposure_total values for a wallet (newest first)."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT exposure_total FROM wallet_performance_snapshots
               WHERE address = ? ORDER BY snapshot_at DESC, id DESC LIMIT ?""",
            (address.lower(), limit),
        ).fetchall()
    return [float(r["exposure_total"] or 0.0) for r in rows]


def _coin_whales(coin: str) -> list[dict]:
    """Current tracked-whale positions in `coin` (>= whale floor): one row per
    wallet from its newest snapshot, enriched with that wallet's latest
    health/smart score (via the tie-safe single-row reader)."""
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT ps.address, ps.side, ps.notional_usd
                FROM position_snapshots ps
                INNER JOIN (
                    SELECT address, MAX(snapshot_at) AS latest
                    FROM position_snapshots
                    WHERE snapshot_at > datetime('now', '-{int(_CONFLUENCE_WINDOW_MIN)} minutes')
                    GROUP BY address
                ) m ON ps.address = m.address AND ps.snapshot_at = m.latest
                WHERE UPPER(ps.coin) = UPPER(?) AND ps.notional_usd >= ?""",
            (coin, _MIN_WHALE_NOTIONAL),
        ).fetchall()

    whales: dict[str, dict] = {}
    for r in rows:
        addr = r["address"]
        if addr in whales:  # one current position per (coin, side) per wallet
            continue
        perf = db.get_latest_wallet_performance(addr)
        whales[addr] = {
            "address": addr,
            "side": r["side"],
            "notional_usd": float(r["notional_usd"] or 0),
            "health_score": float(perf["health_score"]) if perf else 50.0,
            "smart_score": float(perf["smart_score"]) if perf else 0.0,
        }
    return list(whales.values())


def _recent_oi_delta_pct(coin: str) -> float | None:
    """Percent change between the two most-recent OI snapshots for a coin."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT oi_usd FROM oi_snapshots
               WHERE UPPER(coin) = UPPER(?) ORDER BY snapshot_at DESC LIMIT 2""",
            (coin,),
        ).fetchall()
    if len(rows) < 2:
        return None
    now, then = float(rows[0]["oi_usd"] or 0), float(rows[1]["oi_usd"] or 0)
    if then <= 0:
        return None
    return (now - then) / then * 100.0


# --------------------------- readings ---------------------------
def _wallet_netflow(address: str) -> str:
    """accumulating|distributing|neutral from the wallet's exposure trend."""
    exp = _recent_exposures(address, 2)
    if len(exp) < 2 or exp[1] <= 0:
        return "neutral"
    delta_pct = (exp[0] - exp[1]) / exp[1] * 100.0
    if delta_pct >= _NETFLOW_EXPOSURE_DELTA_PCT:
        return "accumulating"
    if delta_pct <= -_NETFLOW_EXPOSURE_DELTA_PCT:
        return "distributing"
    return "neutral"


def build_wallet_reading(address: str) -> dict | None:
    """Point-in-time reading for a single wallet. None if untracked/stale."""
    perf = db.get_latest_wallet_performance(address)
    if perf is None or not _is_fresh(perf["snapshot_at"]):
        return None

    # Company on the wallet's dominant position: how many tracked whales sit on
    # the same coin+side, and their combined skill (confluence strength).
    positions = db.get_last_snapshot_positions(address)
    whale_count, confluence = 1, float(perf["smart_score"] or 0.0)
    if positions:
        dom = max(positions, key=lambda p: abs(float(p["notional_usd"] or 0)))
        peers = [w for w in _coin_whales(dom["coin"]) if w["side"] == dom["side"]]
        if peers:
            whale_count = len(peers)
            confluence = round(sum(float(w["smart_score"] or 0.0) for w in peers), 1)

    return {
        "target": address.lower(),
        "targetType": "wallet",
        "codename": identity.codename_for(address),
        "healthScore": round(float(perf["health_score"] or 0.0), 1),
        "confluence": confluence,
        "whaleCount": whale_count,
        "netFlow": _wallet_netflow(address),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _coin_netflow(dominant_side: str, oi_delta_pct: float | None) -> str:
    """accumulating|distributing|neutral from whale side + OI trend."""
    rising = oi_delta_pct is not None and oi_delta_pct >= _NETFLOW_OI_DELTA_PCT
    falling = oi_delta_pct is not None and oi_delta_pct <= -_NETFLOW_OI_DELTA_PCT
    if dominant_side == "long" and not falling:
        return "accumulating"
    if dominant_side == "short" or falling:
        return "distributing"
    if rising:
        return "accumulating"
    return "neutral"


def build_coin_reading(coin: str) -> dict | None:
    """Point-in-time reading for a coin/market. None if no current whale data."""
    whales = _coin_whales(coin)
    if not whales:
        return None

    # Dominant side = the side more tracked whales are on right now.
    by_side: dict[str, list[dict]] = {"long": [], "short": []}
    for w in whales:
        if w["side"] in by_side:
            by_side[w["side"]].append(w)
    dominant_side = max(by_side, key=lambda s: len(by_side[s]))
    group = by_side[dominant_side]
    if not group:
        return None

    # notional-weighted average health of the whales on the dominant side.
    tot_notional = sum(abs(float(w["notional_usd"] or 0)) for w in group) or 1.0
    health = sum(
        (float(w["health_score"] or 50.0)) * abs(float(w["notional_usd"] or 0))
        for w in group
    ) / tot_notional

    return {
        "target": coin.upper(),
        "targetType": "coin",
        "side": dominant_side,
        "healthScore": round(health, 1),
        "confluence": round(sum(float(w["smart_score"] or 0.0) for w in group), 1),
        "whaleCount": len(group),
        "netFlow": _coin_netflow(dominant_side, _recent_oi_delta_pct(coin)),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------- HTTP layer ---------------------------
def _authorized(request: web.Request) -> bool:
    provided = request.headers.get("X-Internal-Key", "")
    expected = config.WHALE_CHECK_API_KEY or ""
    if not expected:
        return False
    return hmac.compare_digest(provided, expected)


async def _handle_whale_check(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    target = (request.query.get("target") or "").strip()
    chain = (request.query.get("chain") or "base").strip().lower()
    if not target:
        return web.json_response({"error": "missing required 'target'"}, status=400)

    try:
        if _WALLET_RE.match(target):
            reading = build_wallet_reading(target)
        else:
            reading = build_coin_reading(target)
    except Exception:  # never leak internals; a 5xx makes the caller's handler throw
        log.exception("whale-check failed for target=%s", target)
        return web.json_response({"error": "internal error"}, status=500)

    if reading is None:
        # No fresh data for this target -> 404 so the Bankr handler THROWS and the
        # caller is NOT billed (settle-after-response). Never return zeroed data.
        return web.json_response(
            {"error": f"no current whale data for target '{target}'"}, status=404
        )

    reading["chain"] = chain
    return web.json_response(reading)


async def _handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/whale-check", _handle_whale_check),
        web.get("/healthz", _handle_health),
    ])
    return app


# Runner handle so the caller can start/stop it alongside the PTB app.
_runner: web.AppRunner | None = None


async def start(host: str | None = None, port: int | None = None) -> None:
    """Start the internal endpoint (no-op unless enabled + key present)."""
    global _runner
    if not config.WHALE_CHECK_API_ENABLED:
        return
    if not config.WHALE_CHECK_API_KEY:
        log.warning("WHALE_CHECK_API_ENABLED=true but WHALE_CHECK_API_KEY unset — not starting.")
        return
    host = host or config.WHALE_CHECK_API_HOST
    port = port or config.WHALE_CHECK_API_PORT
    _runner = web.AppRunner(build_app())
    await _runner.setup()
    site = web.TCPSite(_runner, host, port)
    await site.start()
    log.info("whale-check x402 endpoint listening on %s:%s", host, port)


async def stop() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None

"""Persistent wallet identity & behavioral profile.

Consumes the existing smart_score + wallet-health/stress logic (see
trackers/wallet_tracker.py) and turns it into a durable per-wallet profile:

  * point-in-time, refreshed every cycle: smart_score, skill tier, current state,
    leverage, and trailing day/week/month ROI + PnL (from leaderboard
    windowPerformances).
  * accumulated over observed history (NOT recomputed from scratch each cycle):
    win/loss on closed positions, adds-to-losers vs cuts, average leverage,
    average hold, biggest observed drawdown, and a flailing (rapid flips +
    stress-adds) signal over a trailing window.

Thresholds (tiers, state) are env-configurable; see config.settings.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from core import identity
from storage import database as db
from utils.fmt import fmt_price

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------- classifiers ---------------------------
def skill_tier(smart_score: float | None) -> str:
    """Sharp / Solid / Average / Sloppy from smart_score (cutoffs in config)."""
    s = smart_score if smart_score is not None else 0.0
    if s >= config.WALLET_TIER_SHARP:
        return "Sharp"
    if s >= config.WALLET_TIER_SOLID:
        return "Solid"
    if s >= config.WALLET_TIER_AVERAGE:
        return "Average"
    return "Sloppy"


# (state_key, emoji, label)
_STATE_META = {
    "imploding": ("🆘", "imploding"),
    "stress": ("⚠️", "under stress"),
    "hot": ("🔥", "hot"),
    "cold": ("🧊", "cold/bleeding"),
    "neutral": ("➖", "neutral"),
}


def derive_state(perf_state: str, day_roi: float, week_roi: float,
                 stress_add: bool) -> str:
    """Map existing health + stress signals to a current-state flag.

    Priority: imploding (existing self-implosion) > under-stress (adding into a
    loss) > hot (green day+week) > cold/bleeding (red day+week) > neutral.
    """
    eps = config.WALLET_STATE_ROI_EPS
    if perf_state == "self_imploding":
        return "imploding"
    if stress_add:
        return "stress"
    if day_roi > eps and week_roi > eps:
        return "hot"
    if day_roi < -eps and week_roi < -eps:
        return "cold"
    return "neutral"


def _window_perf(row: dict) -> tuple[float, float, float, float, float, float]:
    """(day_roi, week_roi, month_roi, day_pnl, week_pnl, month_pnl) from a row."""
    perfs = dict(row.get("windowPerformances", {}) or {})

    def val(name: str, key: str) -> float:
        try:
            return float((perfs.get(name) or {}).get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    return (val("day", "roi"), val("week", "roi"), val("month", "roi"),
            val("day", "pnl"), val("week", "pnl"), val("month", "pnl"))


# --------------------------- per-cycle update ---------------------------
def update_profile(address: str, row: dict, prev_cycle_by_coin: dict,
                   current_by_coin: dict, diff_events: list[dict],
                   day_pnl: float, stress_add: bool, seed_mode: bool) -> None:
    """Refresh point-in-time profile fields and accumulate behavioral stats.

    Reads the wallet-performance snapshot just written this cycle (state,
    smart_score, leverage, open uPnL) rather than recomputing it.
    """
    perf = db.get_latest_wallet_performance(address)
    if perf is None:
        return
    smart = perf["smart_score"]
    lev = perf["book_leverage"] or 0.0
    open_upnl = perf["open_upnl"] or 0.0
    acct = perf["account_value"] or 0.0
    codename = identity.codename_for(address)
    tier = skill_tier(smart)
    droi, wroi, mroi, dpnl, wpnl, mpnl = _window_perf(row)
    state = derive_state(perf["state"], droi, wroi, stress_add)

    db.upsert_profile_point_in_time(
        address, codename, smart, tier, state, acct, lev,
        droi, wroi, mroi, dpnl, wpnl, mpnl,
    )
    db.bump_profile_counters(
        address, {"cycles_observed": 1, "sum_leverage": lev},
        drawdown_candidate=open_upnl,
    )
    _accumulate(address, prev_cycle_by_coin, current_by_coin, diff_events,
                day_pnl, seed_mode)


def _add_hold(lot, deltas: dict) -> None:
    if not lot:
        return
    try:
        opened = datetime.fromisoformat(lot["opened_at"])
    except (TypeError, ValueError):
        return
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    secs = max(0.0, (datetime.now(timezone.utc) - opened).total_seconds())
    deltas["sum_hold_seconds"] = deltas.get("sum_hold_seconds", 0.0) + secs
    deltas["hold_samples"] = deltas.get("hold_samples", 0) + 1


def _accumulate(address: str, prev_cycle_by_coin: dict, current_by_coin: dict,
                diff_events: list[dict], day_pnl: float, seed_mode: bool) -> None:
    # Lazy import avoids a circular dependency (wallet_tracker imports this module).
    from trackers.wallet_tracker import SIZE_INCREASE_THRESHOLD_PCT

    deltas: dict = {}

    def bump(key: str, amount=1):
        deltas[key] = deltas.get(key, 0) + amount

    # Opens & adds from the raw position compare; keep open-lots current.
    for coin, curr in current_by_coin.items():
        prev = prev_cycle_by_coin.get(coin)
        if prev is None:
            db.upsert_open_lot(address, coin, curr["side"], _now_iso(),
                               float(curr["unrealized_pnl"]))
            continue
        if curr["side"] != prev["side"]:
            continue  # flip — handled via diff_events
        prev_size = float(prev["size"])
        if prev_size > 0 and not seed_mode:
            growth = (float(curr["size"]) - prev_size) / prev_size * 100.0
            if growth >= SIZE_INCREASE_THRESHOLD_PCT:
                bump("adds_total")
                if day_pnl < 0:  # adding into a red day = adding to a loser
                    bump("adds_to_losers")
                    db.record_behavior_event(address, "stress_add", coin)
        if db.get_open_lot(address, coin, curr["side"]) is None:
            db.upsert_open_lot(address, coin, curr["side"], _now_iso(),
                               float(curr["unrealized_pnl"]))
        else:
            db.update_open_lot_pnl(address, coin, curr["side"],
                                   float(curr["unrealized_pnl"]))

    # Closes / trims / flips from the exit diff.
    for ev in diff_events:
        coin = ev["coin"]
        prev = ev["prev"]
        side = prev["side"]
        lot = db.get_open_lot(address, coin, side)
        last_pnl = lot["last_pnl"] if lot else float(prev["unrealized_pnl"])
        if ev["type"] == "close":
            bump("closes_observed")
            bump("cuts_total")
            if last_pnl < 0:
                bump("cuts_in_loss")
                bump("losses")
            else:
                bump("wins")
            _add_hold(lot, deltas)
            db.remove_open_lot(address, coin, side)
        elif ev["type"] == "trim":
            bump("cuts_total")
            if last_pnl < 0:
                bump("cuts_in_loss")
        elif ev["type"] == "flip":
            bump("flips_total")
            bump("closes_observed")
            if last_pnl < 0:
                bump("losses")
            else:
                bump("wins")
            db.record_behavior_event(address, "flip", coin)
            _add_hold(lot, deltas)
            db.remove_open_lot(address, coin, side)
            curr = ev["curr"]
            db.upsert_open_lot(address, coin, curr["side"], _now_iso(),
                               float(curr["unrealized_pnl"]))

    if deltas:
        db.bump_profile_counters(address, deltas)


# --------------------------- presentation ---------------------------
def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _usd(x: float) -> str:
    return f"+${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}"


def _dur(seconds: float) -> str:
    h = seconds / 3600.0
    if h < 1:
        return f"{seconds / 60:.0f}m"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h / 24:.1f}d"


def flails_per_hr(address: str) -> float:
    window = max(1, config.WALLET_FLAIL_WINDOW_MIN)
    n = db.count_behavior_events(address, ["flip", "stress_add"], window)
    return n * 60.0 / window


def _behavioral_tag(p, fph: float) -> str:
    adds = p["adds_total"] or 0
    a2l = (p["adds_to_losers"] / adds) if adds else 0.0
    decisions = (p["wins"] or 0) + (p["losses"] or 0)
    win_rate = (p["wins"] / decisions) if decisions else None
    parts = []
    if adds >= 2 and a2l >= 0.5:
        parts.append("adds to losers")
    elif decisions >= 3 and a2l < 0.25 and (win_rate or 0) >= 0.5:
        parts.append("cuts losers fast")
    if fph >= 2:
        parts.append(f"{fph:.0f} flips/hr")
    if not parts and (p["cycles_observed"] or 0) > 0:
        parts.append(f"{(p['sum_leverage'] or 0) / p['cycles_observed']:.0f}x avg lev")
    return " · ".join(parts)


def _primary_stat(p, state: str) -> str:
    if state in ("cold", "imploding"):
        return f"{_usd(p['day_pnl'] or 0)} day"
    if state == "stress":
        return "adding into loss"
    return f"{(p['week_roi'] or 0) * 100:+.0f}% week"


def profile_line(address: str) -> str:
    """Short, scannable profile context for an alert (no codename — that's the
    headline). e.g. 'Sharp · 🔥 hot · +18% week · cuts losers fast'."""
    p = db.get_wallet_profile(address)
    if p is None:
        return ""
    emoji, label = _STATE_META.get(p["state"] or "neutral", _STATE_META["neutral"])
    bits = [p["skill_tier"] or "Average", f"{emoji} {label}",
            _primary_stat(p, p["state"] or "neutral")]
    tag = _behavioral_tag(p, flails_per_hr(address))
    if tag:
        bits.append(tag)
    return " · ".join(bits)


def format_dossier(address: str) -> str | None:
    """Full /wallet dossier for one wallet."""
    p = db.get_wallet_profile(address)
    if p is None:
        return None
    emoji, label = _STATE_META.get(p["state"] or "neutral", _STATE_META["neutral"])
    cyc = p["cycles_observed"] or 0
    avg_lev = (p["sum_leverage"] or 0) / cyc if cyc else 0.0
    decisions = (p["wins"] or 0) + (p["losses"] or 0)
    win_rate = (p["wins"] / decisions * 100) if decisions else 0.0
    adds = p["adds_total"] or 0
    a2l = (p["adds_to_losers"] / adds * 100) if adds else 0.0
    holds = p["hold_samples"] or 0
    avg_hold = (p["sum_hold_seconds"] or 0) / holds if holds else 0.0
    fph = flails_per_hr(address)

    lines = [
        f"🪪 <b>{p['codename']}</b>  <code>{address[:6]}...{address[-4:]}</code>",
        f"🎖 <b>{p['skill_tier']}</b> · smart {(p['smart_score'] or 0):+.1f}"
        f" · {emoji} {label}",
        "━━━━━━━━━━━━━━━━",
        "<b>Trailing performance</b>",
        f"  ROI  — day {_pct(p['day_roi'] or 0)} | week {_pct(p['week_roi'] or 0)}"
        f" | month {_pct(p['month_roi'] or 0)}",
        f"  PnL  — day {_usd(p['day_pnl'] or 0)} | week {_usd(p['week_pnl'] or 0)}"
        f" | month {_usd(p['month_pnl'] or 0)}",
        "━━━━━━━━━━━━━━━━",
        "<b>Behavior</b> (accumulated)",
        f"  Win/loss on closes: {p['wins'] or 0}-{p['losses'] or 0}"
        + (f" ({win_rate:.0f}% win)" if decisions else " (none yet)"),
        f"  Adds to losers: {p['adds_to_losers'] or 0}/{adds}"
        + (f" ({a2l:.0f}%)" if adds else ""),
        f"  Cuts: {p['cuts_total'] or 0} (in loss {p['cuts_in_loss'] or 0})",
        f"  Avg leverage: {avg_lev:.1f}x",
        f"  Avg hold: {_dur(avg_hold)}" if holds else "  Avg hold: n/a",
        f"  Biggest drawdown: {_usd(p['max_drawdown_usd'] or 0)}",
        f"  Flips: {p['flips_total'] or 0} total · {fph:.0f}/hr now",
        f"  Observed: {cyc} cycles",
        "━━━━━━━━━━━━━━━━",
        "<b>Open positions</b>",
    ]
    positions = db.get_last_snapshot_positions(address)
    if not positions:
        lines.append("  (none / flat)")
    else:
        for r in positions:
            side = (r["side"] or "").upper()
            lines.append(
                f"  {r['coin']} {side} ${r['notional_usd']:,.0f} | "
                f"entry ${fmt_price(r['entry_px'])} | uPnL {_usd(r['unrealized_pnl'] or 0)}"
            )
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("<i>Not financial advice. Data only.</i>")
    return "\n".join(lines)

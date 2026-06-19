"""Coin-setup message formatting.

Ported from the 5-file bot.py format_setup, converted Markdown -> HTML so the
whole system uses one Telegram parse mode (the wallet alerts are already HTML).
Visually identical to the live cards in the screenshot. Dynamic text is escaped.
"""
import html

from utils.fmt import fmt
from utils.sizing import position_size


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def format_setup(s: dict) -> str:
    coin = _esc(s.get("coin", "???"))
    score = s.get("score") or 0
    msg = f"🚀 <b>{coin}</b>  |  Score: {score}"

    for setup in s.get("setups", []):
        direction = setup.get("direction", "long").upper()
        emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
        sizing = position_size(float(setup.get("entry", 0) or 0), float(setup.get("stop", 0) or 0))
        targets = setup.get("targets") or [0, 0, 0]
        targets = (list(targets) + [0, 0, 0])[:3]

        msg += f"\n\n{emoji}  |  Conf: {setup.get('confidence', 'med').upper()}"
        msg += f"\nEntry: <code>{fmt(setup.get('entry'))}</code>"
        msg += f"\nStop: <code>{fmt(setup.get('stop'))}</code>"
        msg += (f"\nTargets: <code>{fmt(targets[0])}</code> → "
                f"<code>{fmt(targets[1])}</code> → <code>{fmt(targets[2])}</code>")
        msg += f"\nLeverage: {setup.get('leverage_set', 5)}x  |  Risk: {setup.get('risk_pct_at_leverage', 1)}%"
        msg += f"\nPosition Size: ~${sizing['size_usd']} ({sizing['size_units']} units)"
        msg += f"\nRationale: {_esc(setup.get('rationale'))}"
        msg += f"\nInvalidation: {_esc(setup.get('invalidation'))}"
    return msg

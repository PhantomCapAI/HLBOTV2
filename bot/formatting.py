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

    # Score badge
    if score >= 90:
        badge = "🔥🔥🔥"
    elif score >= 70:
        badge = "🔥🔥"
    elif score >= 52:
        badge = "🔥"
    else:
        badge = "👀"

    msg = f"🚀 <b>{coin}</b>  |  Score: {score} {badge}"

    for setup in s.get("setups", []):
        direction = setup.get("direction", "long").upper()
        conf = setup.get("confidence", "med").upper()

        if direction == "LONG":
            dir_emoji = "🟢 LONG"
        else:
            dir_emoji = "🔴 SHORT"

        if conf == "HIGH":
            conf_emoji = "⚡ HIGH"
        elif conf == "MED":
            conf_emoji = "📊 MED"
        else:
            conf_emoji = "🌫 LOW"

        sizing = position_size(float(setup.get("entry", 0) or 0), float(setup.get("stop", 0) or 0))
        targets = setup.get("targets") or [0, 0, 0]
        targets = (list(targets) + [0, 0, 0])[:3]

        msg += f"\n\n{dir_emoji}  |  {conf_emoji}"
        msg += f"\n\n🎯 Entry:  <code>{fmt(setup.get('entry'))}</code>"
        msg += f"\n🛑 Stop:   <code>{fmt(setup.get('stop'))}</code>"
        msg += (f"\n💰 TP1:    <code>{fmt(targets[0])}</code>\n"
                f"💰 TP2:    <code>{fmt(targets[1])}</code>\n"
                f"💰 TP3:    <code>{fmt(targets[2])}</code>")
        msg += f"\n\n⚙️ Leverage: {setup.get('leverage_set', 5)}x  |  Risk: {setup.get('risk_pct_at_leverage', 1)}%"
        msg += f"\n💵 Size: ~${sizing['size_usd']} ({sizing['size_units']} units)"
        msg += f"\n\n🧠 {_esc(setup.get('rationale'))}"
        msg += f"\n❌ Invalidation: {_esc(setup.get('invalidation'))}"
        msg += "\n\n─────────────────"
    return msg

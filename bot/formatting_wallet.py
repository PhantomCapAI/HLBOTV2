from datetime import datetime


def _rank_line(rank) -> str:
    if isinstance(rank, int):
        return f"🏆 Rank: <b>#{rank}</b> on leaderboard\n"
    return f"👁 Watch: <b>{rank}</b>\n"


def _rank_inline(rank) -> str:
    if isinstance(rank, int):
        return f"Rank #{rank}"
    return f"Watch: {rank}"


def whale_alert(rank: int, address: str, coin: str, side: str,
                notional_usd: float, account_value: float, day_pnl: float) -> str:
    side_emoji = "🟢" if side == "long" else "🔴"
    direction = "LONG" if side == "long" else "SHORT"
    pnl_str = f"+${day_pnl:,.0f}" if day_pnl >= 0 else f"-${abs(day_pnl):,.0f}"
    return (
        f"🐋 <b>WHALE MOVE — Hyperliquid</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{coin}-PERP</b> | {direction}\n"
        f"📊 Size: <b>${notional_usd:,.0f}</b>\n"
        f"{_rank_line(rank)}"
        f"💰 Account: ${account_value:,.0f}\n"
        f"📈 Day PnL: {pnl_str}\n"
        f"🔑 <code>{address[:6]}...{address[-4:]}</code>\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def vip_whale_alert(rank: str, address: str, coin: str, side: str,
                    notional_usd: float, account_value: float, day_pnl: float,
                    strategy: str) -> str:
    side_emoji = "🟢" if side == "long" else "🔴"
    direction  = "LONG" if side == "long" else "SHORT"
    pnl_str    = f"+${day_pnl:,.0f}" if day_pnl >= 0 else f"-${abs(day_pnl):,.0f}"
    return (
        f"⭐ <b>VIP WHALE — {coin}-PERP</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{direction}</b> | Leaderboard {rank}\n"
        f"📊 Size: <b>${notional_usd:,.0f}</b>\n"
        f"💰 Account: ${account_value:,.0f}\n"
        f"📈 Day PnL: {pnl_str}\n"
        f"🧠 Strategy: <i>{strategy}</i>\n"
        f"🔑 <code>{address[:6]}...{address[-4:]}</code>\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def whale_size_increase_alert(rank: int, address: str, coin: str, side: str,
                               notional_usd: float, prev_notional: float,
                               pct_increase: float, account_value: float,
                               day_pnl: float) -> str:
    side_emoji = "🟢" if side == "long" else "🔴"
    direction = "LONG" if side == "long" else "SHORT"
    pnl_str = f"+${day_pnl:,.0f}" if day_pnl >= 0 else f"-${abs(day_pnl):,.0f}"
    added = notional_usd - prev_notional
    return (
        f"🐋📈 <b>WHALE ADDING — Hyperliquid</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{coin}-PERP</b> | {direction}\n"
        f"➕ Added: <b>${added:,.0f}</b> (+{pct_increase:.0f}%)\n"
        f"📊 New Size: <b>${notional_usd:,.0f}</b>\n"
        f"📉 Previous: ${prev_notional:,.0f}\n"
        f"{_rank_line(rank)}"
        f"💰 Account: ${account_value:,.0f}\n"
        f"📈 Day PnL: {pnl_str}\n"
        f"🔑 <code>{address[:6]}...{address[-4:]}</code>\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def whale_stress_watch_alert(rank: int, address: str, coin: str, side: str,
                             notional_usd: float, prev_notional: float,
                             pct_increase: float, account_value: float,
                             day_pnl: float, unrealized_pnl: float,
                             entry_px: float, curr_px: float,
                             liq_px: float = 0.0) -> str:
    side_emoji = "🟢" if side == "long" else "🔴"
    direction = "LONG" if side == "long" else "SHORT"
    pnl_str = f"+${day_pnl:,.0f}" if day_pnl >= 0 else f"-${abs(day_pnl):,.0f}"
    upnl_str = (
        f"+${unrealized_pnl:,.0f}"
        if unrealized_pnl >= 0
        else f"-${abs(unrealized_pnl):,.0f}"
    )
    added = notional_usd - prev_notional
    move_dir = "drop" if side == "long" else "pump"

    liq_line = ""
    if liq_px > 0 and curr_px > 0:
        dist_pct = abs(((liq_px - curr_px) / curr_px) * 100)
        liq_line = (
            f"💀 Liq: <b>${liq_px:,.4g}</b> "
            f"({dist_pct:.1f}% {move_dir})\n"
        )

    return (
        f"🔥 <b>WHALE STRESS WATCH — {coin}-PERP</b> | 🔒 <b>PRO</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{direction}</b> | {_rank_inline(rank)}\n"
        f"📊 Position: <b>${notional_usd:,.0f}</b>\n"
        f"➕ Added: <b>${added:,.0f}</b> (+{pct_increase:.0f}%)\n"
        f"📉 uPnL: <b>{upnl_str}</b>\n"
        f"🎯 Entry: ${entry_px:,.4g} | Current: ${curr_px:,.4g}\n"
        f"{liq_line}"
        f"💰 Account: ${account_value:,.0f}\n"
        f"📈 Day PnL: {pnl_str}\n"
        f"🔑 <code>{address[:6]}...{address[-4:]}</code>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>Read:</b> wallet is adding instead of closing while under stress.\n"
        f"The signal is what happens next: add, trim, exit, or forced pressure.\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def whale_reactivation_alert(rank: int, address: str, coin: str, side: str,
                             notional_usd: float, account_value: float,
                             day_pnl: float, profile: str,
                             inactive_for: str) -> str:
    side_emoji = "🟢" if side == "long" else "🔴"
    direction = "LONG" if side == "long" else "SHORT"
    pnl_str = f"+${day_pnl:,.0f}" if day_pnl >= 0 else f"-${abs(day_pnl):,.0f}"
    return (
        f"🟣🐋 <b>WHALE REACTIVATION — {coin}-PERP</b> | 🔒 <b>PRO</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{direction}</b> | {_rank_inline(rank)}\n"
        f"📊 Position: <b>${notional_usd:,.0f}</b>\n"
        f"👤 Profile: <i>{profile}</i>\n"
        f"⏱ Last active: <b>{inactive_for}</b>\n"
        f"💰 Account: ${account_value:,.0f}\n"
        f"📈 Day PnL: {pnl_str}\n"
        f"🔑 <code>{address[:6]}...{address[-4:]}</code>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>Read:</b> sidelined/watch wallet is active again.\n"
        f"The signal is what happens next: add, trim, flip, hedge, or de-risk.\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def wallet_performance_alert(rank, address: str, state: str, account_value: float,
                             exposure_total: float, open_upnl: float,
                             negative_upnl: float, book_leverage: float,
                             reason: str) -> str:
    state_labels = {
        "hot_streak": "🔥 HOT STREAK",
        "heating_up": "📈 HEATING UP",
        "cooling_off": "📉 COOLING OFF",
        "implosion_watch": "⚠️ IMPLOSION WATCH",
        "self_imploding": "🚨 SELF-IMPLODING",
    }
    label = state_labels.get(state, state.replace("_", " ").upper())
    upnl_str = f"+${open_upnl:,.0f}" if open_upnl >= 0 else f"-${abs(open_upnl):,.0f}"
    negative_line = f"\n🩸 Negative uPnL: <b>-${abs(negative_upnl):,.0f}</b>" if negative_upnl < 0 else ""
    return (
        f"{label} — <b>Wallet Health</b> | 🔒 <b>PRO</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{_rank_line(rank)}"
        f"🔑 <code>{address[:6]}...{address[-4:]}</code>\n"
        f"💰 Equity: <b>${account_value:,.0f}</b>\n"
        f"📊 Exposure: <b>${exposure_total:,.0f}</b> ({book_leverage:.2f}x book)\n"
        f"📈 Open uPnL: <b>{upnl_str}</b>"
        f"{negative_line}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>Read:</b> {reason}\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def confluence_alert(coin: str, side: str, whale_count: int,
                     total_notional: float, whales: list[dict],
                     premium: bool = False) -> str:
    """Full confluence alert — sent to Pro channel."""
    side_emoji = "🟢" if side == "long" else "🔴"
    direction = "LONG" if side == "long" else "SHORT"
    premium_tag = " | 🔒 <b>PREMIUM</b>" if premium else ""
    whale_lines = "\n".join(
        f"  🏆 #{w['rank']} — ${w['notional']:,.0f} | <code>{w['address'][:6]}...{w['address'][-4:]}</code>"
        for w in sorted(whales, key=lambda x: x["rank"])
    )
    return (
        f"🐋🐋 <b>WHALE CONFLUENCE — {coin}-PERP</b>{premium_tag}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{direction}</b> | {whale_count} top-50 wallets aligned\n"
        f"💰 Combined: <b>${total_notional:,.0f}</b>\n\n"
        f"<b>Wallets:</b>\n{whale_lines}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def liquidation_risk_alert(rank: int, address: str, coin: str, side: str,
                           notional_usd: float, liq_px: float, curr_px: float,
                           dist_pct: float, danger: bool = False) -> str:
    side_emoji = "🟢" if side == "long" else "🔴"
    direction  = "LONG" if side == "long" else "SHORT"
    level      = "🚨 DANGER" if danger else "⚠️ WARNING"
    move_dir   = "drop" if side == "long" else "pump"
    return (
        f"{level} <b>LIQUIDATION RISK — {coin}-PERP</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{direction}</b> | {_rank_inline(rank)}\n"
        f"📊 Position: <b>${notional_usd:,.0f}</b>\n"
        f"💀 Liq Price: <b>${liq_px:,.2f}</b>\n"
        f"📍 Current:  ${curr_px:,.2f}\n"
        f"📏 Distance: <b>{abs(dist_pct):.1f}%</b> — needs {abs(dist_pct):.1f}% {move_dir}\n"
        f"🔑 <code>{address[:6]}...{address[-4:]}</code>\n"
        f"🕐 {__import__('datetime').datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def confluence_teaser(coin: str, side: str, whale_count: int) -> str:
    """Teaser alert for free channel — no wallet details, no chart, creates FOMO."""
    side_emoji = "🟢" if side == "long" else "🔴"
    direction = "LONG" if side == "long" else "SHORT"
    return (
        f"🐋🐋 <b>WHALE CONFLUENCE DETECTED</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{coin}-PERP</b> | {direction}\n"
        f"📡 {whale_count} top-50 wallets aligned\n\n"
        f"🔒 Full signal, wallet ranks + chart\n"
        f"→ <b>@HLIntelPro</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def funding_spike_alert(asset: str, funding_rate: float, open_interest: float,
                        mark_px: float, prev_rate: float) -> str:
    rate_pct = funding_rate * 100
    prev_pct = prev_rate * 100
    direction = "LONGS paying SHORTS" if funding_rate > 0 else "SHORTS paying LONGS"
    arrow = "📈" if funding_rate > prev_rate else "📉"
    return (
        f"⚡ <b>FUNDING SPIKE — {asset}-PERP</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{arrow} Rate: <b>{rate_pct:.4f}%</b> per 8h\n"
        f"📊 Previously: {prev_pct:.4f}%\n"
        f"💸 {direction}\n"
        f"📉 Mark Price: ${mark_px:,.2f}\n"
        f"🔓 Open Interest: ${open_interest:,.0f}\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )


def oi_surge_alert(asset: str, open_interest: float, prev_oi: float,
                   pct_change: float, mark_px: float, prev_px: float = 0.0) -> str:
    oi_up = pct_change > 0
    px_up = mark_px > prev_px if prev_px > 0 else None
    px_pct = ((mark_px - prev_px) / prev_px * 100) if prev_px > 0 else None

    if px_up is None:
        # No price context — fallback to original simple format
        emoji   = "🚀" if oi_up else "📉"
        signal  = "OI SURGE" if oi_up else "OI DROP"
        context = ""
    elif oi_up and px_up:
        emoji   = "📈"
        signal  = "🟢 NEW LONGS ENTERING"
        context = "Rising OI + rising price — trend continuation\n\n"
    elif oi_up and not px_up:
        emoji   = "📉"
        signal  = "🔴 NEW SHORTS ENTERING"
        context = "Rising OI + falling price — bearish pressure building\n\n"
    elif not oi_up and px_up:
        emoji   = "⚠️"
        signal  = "🟡 SHORT COVERING"
        context = "Falling OI + rising price — shorts closing, weaker move\n\n"
    else:
        emoji   = "⚠️"
        signal  = "🟡 LONGS UNWINDING"
        context = "Falling OI + falling price — long liquidations / exits\n\n"

    oi_str = f"+{pct_change:.1f}%" if oi_up else f"{pct_change:.1f}%"
    px_line = ""
    if px_pct is not None:
        px_str  = f"+{px_pct:.1f}%" if px_up else f"{px_pct:.1f}%"
        px_line = f"📍 Price change: <b>{px_str}</b>  (${mark_px:,.2f})\n"

    return (
        f"{emoji} <b>OI SIGNAL — {asset}-PERP</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{signal}\n"
        f"<i>{context}</i>"
        f"📊 OI change:    <b>{oi_str}</b>  (${open_interest:,.0f})\n"
        f"{px_line}"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Not financial advice. Data only.</i>"
    )

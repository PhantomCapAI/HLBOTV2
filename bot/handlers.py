"""Telegram command handlers.

Async port of the 5-file bot.py commands, plus the merged wallet/confluence
commands. /start and /stop are the on/off toggle; /alerts pauses just the
proactive pushes. /scan and /coin are pull (work whenever). State is persisted
in SQLite so the toggle survives restarts.
"""
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

import config
from storage import database as db
from core.entitlements import require_paid, is_paid, paywall_message
from core.solana_pay import verify_usdc_payment
from scanner.setups import coin_scan, deep_dive_symbol
from scanner.screener import flow_for
from scanner.flow import flow_line
from bot.formatting import format_setup
from services import wallet_profile as wp

log = logging.getLogger(__name__)

# Friendly explanations for verify_usdc_payment failure reasons.
_PAID_REASONS = {
    "receiving_address_not_configured": "Payment isn't configured yet — contact the operator.",
    "malformed_signature": "That doesn't look like a Solana transaction signature.",
    "rpc_error": "Couldn't reach Solana to verify right now — try again in a moment.",
    "tx_not_found": "I couldn't find that transaction on Solana (yet). Wait for it to confirm, then retry.",
    "tx_failed": "That transaction failed on-chain.",
    "no_block_time": "That transaction isn't confirmed yet — wait a few seconds and retry.",
    "tx_too_old": "That payment is too old to redeem.",
    "bad_block_time": "That transaction's timestamp looks off — please retry.",
    "no_usdc_to_recipient": "I don't see a USDC payment to our address in that transaction.",
    "unexpected_token_decimals": "That token doesn't look like USDC.",
    "amount_too_low": f"That payment is below the required ${config.PAYMENT_PRICE_USD:.2f} USDC.",
}


def _fmt_until(raw: str | None) -> str:
    """Render a stored paid_until ISO timestamp as 'YYYY-MM-DD HH:MM UTC'."""
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return raw

BANNER = (
    "🟢 <b>HL Intel Scanner ONLINE</b> — on its tippy toes.\n\n"
    "Commands:\n"
    "/scan — manual coin scan\n"
    "/coin SYMBOL — deep dive\n"
    "/wallets — tracked wallet activity\n"
    "/confluence — wallet × setup confluence\n"
    "/alerts — toggle proactive alerts\n"
    "/status — show state\n"
    "/stop — turn off\n\n"
    "High-confluence setups and whale activity will ping you automatically when on."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome + how-to-pay. /start never activates for free — every /start
    routes through payment. Paying ($3 USDC) opens access for up to 3 days
    ($1/day); after that the user repays. Background alerts + value commands
    are driven by the paid window."""
    chat_id = update.effective_chat.id
    header = (
        "👋 <b>HL Intel — pay to use.</b>\n\n"
        f"<b>${config.PAYMENT_PRICE_USD:.2f} USDC</b> on Solana opens the scanner "
        f"(value commands + proactive alerts) for up to "
        f"<b>{config.PAYMENT_VALIDITY_DAYS} days</b> — about $1/day.\n\n"
    )
    if is_paid(chat_id):
        raw = db.get_paid_until(chat_id)
        header = (
            "✅ <b>You're active.</b>\n"
            f"Access runs until <b>{_fmt_until(raw)}</b>. "
            "Paying again refills your time.\n\n"
        )
    await update.message.reply_text(header + paywall_message(), parse_mode="HTML")


async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redeem a Solana USDC payment: /paid <tx_signature>."""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/paid &lt;tx_signature&gt;</code>\n"
            f"Pay ${config.PAYMENT_PRICE_USD:.2f} USDC (Solana) first, then send "
            "the transaction signature here.",
            parse_mode="HTML",
        )
        return

    tx = context.args[0].strip()
    if db.is_payment_used(tx):
        await update.message.reply_text(
            "⚠️ That transaction has already been redeemed.")
        return

    await update.message.reply_text("⏳ Verifying your payment on Solana...")
    result = await verify_usdc_payment(tx)
    if not result.get("ok"):
        reason = _PAID_REASONS.get(result.get("reason"), "Payment could not be verified.")
        await update.message.reply_text(f"❌ {reason}")
        return

    # Success: burn the tx (replay protection), grant entitlement, activate.
    db.mark_payment_used(tx, chat_id)
    paid_until = datetime.now(timezone.utc) + timedelta(days=config.PAYMENT_VALIDITY_DAYS)
    db.set_paid_until(chat_id, paid_until.isoformat())
    db.activate_chat(chat_id)
    db.set_state("wallet_seeded", "0")  # fresh baseline on activation
    from services import cycles
    if context.job_queue:
        context.job_queue.run_once(cycles.wallet_seed_job, when=2)
    await update.message.reply_text(
        "✅ <b>Payment verified — you're in.</b>\n"
        f"Access active until <b>{paid_until.strftime('%Y-%m-%d %H:%M UTC')}</b>.\n\n"
        + BANNER,
        parse_mode="HTML",
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.deactivate_chat(chat_id)
    db.set_state("wallet_seeded", "0")
    await update.message.reply_text("🔴 Scanner OFF. Toggle back on with /start.")


async def toggle_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    enabled = not db.get_alerts_enabled(chat_id)
    db.set_alerts_enabled(chat_id, enabled)
    status = "🟢 ENABLED" if enabled else "🔴 DISABLED"
    await update.message.reply_text(f"Proactive alerts are now {status}")


@require_paid(free_taste=True)
async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning Hyperliquid...")
    try:
        setups = await coin_scan()
        if not setups:
            await update.message.reply_text("No valid setups right now.")
            return
        for s in setups:
            await update.message.reply_text(format_setup(s), parse_mode="HTML")
        await update.message.reply_text("✅ Scan complete. Use /scan again anytime.")
    except Exception as e:
        log.error("Scan error: %s", e, exc_info=True)
        await update.message.reply_text(f"Scan error: {str(e)[:200]}")


@require_paid()
async def coin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /coin SYMBOL (e.g. /coin HYPE)")
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"🔎 Deep dive on {symbol}...")
    try:
        setups = await deep_dive_symbol(symbol)
        if not setups:
            await update.message.reply_text(f"No recent data for {symbol}. Try /scan first.")
            return
        for s in setups:
            await update.message.reply_text(format_setup(s), parse_mode="HTML")
    except Exception as e:
        log.error("/coin error for %s: %s", symbol, e, exc_info=True)
        await update.message.reply_text(f"Error analyzing {symbol}: {str(e)[:150]}")


@require_paid()
async def flow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OI trend + funding crowding read for one coin: /flow SYMBOL."""
    if not context.args:
        await update.message.reply_text("Usage: /flow SYMBOL (e.g. /flow BTC)")
        return
    symbol = context.args[0]
    try:
        ctx = await flow_for(symbol)
    except Exception as e:
        log.error("/flow error for %s: %s", symbol, e, exc_info=True)
        await update.message.reply_text(f"Flow lookup failed: {str(e)[:150]}")
        return
    if not ctx:
        await update.message.reply_text(f"{symbol.upper()} isn't in the perp universe.")
        return
    c = ctx["crowding"]
    short = f"{c['doi_short']:+.0f}%" if c.get("doi_short") is not None else "—"
    long_ = f"{c['doi_long']:+.0f}%" if c.get("doi_long") is not None else "—"
    msg = (
        f"📡 <b>{ctx['coin']} — order-flow context</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{flow_line(c)}\n"
        f"ΔOI: {short} ({config.OI_LOOKBACK_SHORT_MIN}m) | {long_} ({config.OI_LOOKBACK_LONG_MIN}m)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<i>Market context only — not financial advice.</i>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


@require_paid()
async def wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from services import correlation
    rows = correlation.current_wallet_confluence()
    if not rows:
        await update.message.reply_text(
            "No recent tracked-wallet positions yet. If you just started, give it one scan cycle."
        )
        return
    lines = ["🐋 <b>Tracked wallet positioning</b> (current):"]
    for r in rows[:15]:
        lines.append(f"• {r['coin']} {r['side'].upper()} — {r['count']} wallet(s), ${r['total']:,.0f}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@require_paid()
async def confluence_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from services import cycles
    snapshot = cycles.last_confluence_snapshot()
    if not snapshot:
        await update.message.reply_text(
            "No confluence signals cached yet. Run /scan or wait for a scan cycle."
        )
        return
    await update.message.reply_text(snapshot, parse_mode="HTML")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active = chat_id in db.get_active_chats()
    alerts_on = db.get_alerts_enabled(chat_id)
    seeded = db.get_state("wallet_seeded") == "1"
    msg = (
        f"<b>Status</b>\n"
        f"Scanner: {'🟢 ON' if active else '🔴 OFF'}\n"
        f"Proactive alerts: {'🟢 ON' if alerts_on else '🔴 OFF'}\n"
        f"Wallet baseline: {'ready' if seeded else 'warming up'}\n"
        f"Wallet scan: every {config.WALLET_SCAN_INTERVAL_SECONDS}s\n"
        f"Coin scan: every {config.COIN_SCAN_INTERVAL_SECONDS}s"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


@require_paid()
async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full dossier for one wallet, by codename or 0x address."""
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/wallet &lt;codename or address&gt;</code>", parse_mode="HTML")
        return
    q = context.args[0].strip()
    if q.lower().startswith("0x"):
        address = q.lower()
    else:
        row = db.get_wallet_profile_by_codename(q)
        if row is None:
            await update.message.reply_text(
                "No tracked wallet by that codename. Check /scores, or pass the 0x address.")
            return
        address = row["address"]
    dossier = wp.format_dossier(address)
    if dossier is None:
        await update.message.reply_text(
            "No profile yet for that wallet — it hasn't been seen in a scan cycle.")
        return
    await update.message.reply_text(dossier, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(BANNER, parse_mode="HTML")


@require_paid()
async def dexs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List builder-deployed (HIP-3) perp dexs and their symbols (equities/metals/FX)."""
    from integrations import hyperliquid as hl
    await update.message.reply_text("🔎 Querying builder perp dexs...")
    try:
        dexs = await hl.get_perp_dexs()
    except Exception as e:
        await update.message.reply_text(f"perpDexs error: {str(e)[:150]}")
        return
    if not dexs:
        await update.message.reply_text("No builder perp dexs found (crypto-only universe).")
        return
    lines = []
    for d in dexs:
        try:
            uni, _ = await hl.get_meta_and_ctxs(d)
            coins = ", ".join(a["name"].split(":", 1)[-1] for a in uni[:40])
            lines.append(f"<b>{d}</b>: {coins or '(no assets)'}")
        except Exception as e:
            lines.append(f"<b>{d}</b>: (error {str(e)[:40]})")
    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


def _owner_ok(chat_id: int) -> bool:
    """Discovery management is operator-only.

    With OWNER_CHAT_ID set, only that chat qualifies; otherwise (single-operator
    deployments with the bypass off) any chat holding a live paid window may.
    """
    if config.OWNER_CHAT_ID:
        return chat_id == config.OWNER_CHAT_ID
    return is_paid(chat_id)


async def candidates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List current discovery suggestions awaiting approval."""
    if not _owner_ok(update.effective_chat.id):
        await update.message.reply_text("🔒 Discovery management is operator-only.")
        return
    rows = db.get_candidates_by_status("suggested")
    if not rows:
        await update.message.reply_text(
            "No discovery suggestions right now. The discovery job posts new ones as it finds them."
        )
        return
    lines = ["🔎 <b>Discovery suggestions</b> (approve with /track)"]
    for r in rows[:20]:
        addr = r["address"]
        lines.append(
            f"• <code>{addr[:6]}…{addr[-4:]}</code> — 🧠 <b>{(r['smart_score'] or 0):+.1f}</b>"
            f" | wk {(r['week_roi'] or 0)*100:+.1f}% mo {(r['month_roi'] or 0)*100:+.1f}%"
            f" | {(r['leverage'] or 0):.1f}x\n  <code>/track {addr}</code>"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def track_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Promote a discovery candidate into the active tracked set."""
    if not _owner_ok(update.effective_chat.id):
        await update.message.reply_text("🔒 Only the operator can promote wallets.")
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/track &lt;wallet_address&gt;</code>", parse_mode="HTML")
        return
    addr = context.args[0].strip().lower()
    cand = db.get_candidate(addr)
    if cand is None:
        await update.message.reply_text(
            "That address isn't a discovery candidate. See /candidates for current suggestions."
        )
        return
    if cand["status"] == "tracked":
        await update.message.reply_text("That wallet is already being tracked.")
        return
    db.set_candidate_status(addr, "tracked")
    await update.message.reply_text(
        f"✅ Now tracking <code>{addr}</code>. It joins the active set on the next wallet cycle.",
        parse_mode="HTML",
    )


@require_paid()
async def scores_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rank tracked wallets by current health score (best -> worst)."""
    rows = db.get_latest_scores()
    if not rows:
        await update.message.reply_text(
            "No wallet scores yet. Give it a wallet-scan cycle or two after /start."
        )
        return

    def line(r) -> str:
        smart = r["smart_score"] if "smart_score" in r.keys() else 0.0
        smart = smart or 0.0
        dot = "🟢" if smart >= 10 else ("⚪" if smart >= 0 else "🔴")
        addr = r["address"]
        short = f"{addr[:6]}…{addr[-4:]}"
        upnl = r["open_upnl"] or 0
        upnl_s = f"+${upnl:,.0f}" if upnl >= 0 else f"-${abs(upnl):,.0f}"
        return (
            f"{dot} <code>{short}</code> — 🧠 <b>{smart:+.1f}</b>"
            f" · health {r['health_score']:.0f} {r['state']}  {upnl_s}"
        )

    out = ["📊 <b>Wallet Smart Scores</b> (skill-ranked)"]
    if len(rows) <= 30:
        out += [line(r) for r in rows]
    else:
        out.append("<b>Top 20</b>")
        out += [line(r) for r in rows[:20]]
        out.append("\n<b>Weakest 8</b>")
        out += [line(r) for r in rows[-8:]]
    out.append(
        "\n<i>Smart score = trailing week+month ROI, minus penalties for high "
        "leverage and adding while red. Not a guarantee or trade advice.</i>"
    )
    await update.message.reply_text("\n".join(out), parse_mode="HTML")

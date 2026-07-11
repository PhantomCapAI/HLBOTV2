"""Entitlement gate for value commands (pay-to-use).

A chat may run the value commands (/scan, /coin, /wallets, /confluence, /dexs,
/scores) only while it has a live ``paid_until`` entitlement. One exception: the
first time an un-entitled chat touches any gated command, its one-time free
trial auto-starts (``TRIAL_HOURS`` of full access), so a new user gets value
immediately without knowing the /trial command exists. Once that trial is used
and expires, the gate shows the paywall.

``require_paid`` wraps a PTB command handler. It guards a feature (not money),
so on any ambiguity it denies and shows the paywall rather than erroring.
"""
from __future__ import annotations

import functools
import logging
from datetime import datetime, timedelta, timezone

import config
from storage import database as db

log = logging.getLogger(__name__)


def is_paid(chat_id: int) -> bool:
    """True iff the chat has a paid_until in the future.

    The operator (OWNER_CHAT_ID) is always treated as paid and never burns the
    free trial. Disabled when OWNER_CHAT_ID is 0 (the default).
    """
    if config.OWNER_CHAT_ID and chat_id == config.OWNER_CHAT_ID:
        return True
    raw = db.get_paid_until(chat_id)
    if not raw:
        return False
    try:
        paid_until = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False  # unparseable → treat as not paid
    if paid_until.tzinfo is None:
        paid_until = paid_until.replace(tzinfo=timezone.utc)
    return paid_until > datetime.now(timezone.utc)


def _plan_lines(chat_id: int | None) -> str:
    """One line per pass: label, the amount to send, and the redeem command.

    With a chat_id each plan shows that chat's unique bound amount (base price +
    per-chat sub-cent nonce, audit C1) at 4dp; without one it shows the plain
    base price."""
    lines = []
    for plan in config.PAYMENT_PLAN_ORDER:
        meta = config.PAYMENT_PLANS[plan]
        if chat_id is not None:
            amount = db.payment_reference(chat_id, plan) / 1_000_000
            price = f"${amount:.4f}"
        else:
            price = f"${meta['price_usd']:.2f}"
        lines.append(
            f"• <b>{meta['label']}</b> — <b>{price} USDC</b> → "
            f"<code>/paid {plan} &lt;tx_signature&gt;</code>"
        )
    return "\n".join(lines)


def paywall_message(chat_id: int | None = None) -> str:
    address = (config.PAYMENT_RECEIVING_ADDRESS or "").strip()
    addr_line = (
        f"<code>{address}</code>" if address
        else "<i>(payment address not configured — contact the operator)</i>"
    )
    if chat_id is not None:
        exact_note = (
            "\n⚠️ Send the <b>exact</b> amount for your plan (rounding up by "
            "under a cent is fine) — the trailing digits identify your account. "
            "A different amount can't be matched to you."
        )
    else:
        exact_note = ""
    # Offer the free trial only to a chat that still has one available.
    if chat_id is not None and config.TRIAL_HOURS > 0 and not db.get_trial_used(chat_id):
        trial_line = (
            f"\nNot ready to pay? Start a one-time <b>{config.TRIAL_HOURS}-hour "
            "free trial</b>: /trial"
        )
    else:
        trial_line = ""
    return (
        "🔒 <b>This command needs an active pass.</b>\n\n"
        f"Send USDC on <b>Solana</b> to:\n{addr_line}\n\n"
        f"{_plan_lines(chat_id)}{exact_note}\n\n"
        "Then run the matching <code>/paid</code> command with your transaction "
        f"signature to unlock.{trial_line}"
    )


def activate_entitled(chat_id: int, context) -> None:
    """Land an entitled chat (paid, trial, or operator) in the active/alert set so
    the proactive whale/confluence/liquidation pushes actually reach it.

    Without this an entitled chat shows "active" copy but never enters
    get_alert_chats(), so only on-demand commands work. Idempotent:
      * activate_chat upserts active=1 (and alerts_enabled=1 for a fresh row);
      * we (re)enable alerts so a prior /stop+/alerts-off chat re-arms;
      * the wallet baseline is seeded once globally, guarded by the wallet_seeded
        flag — never re-seeded per chat (that would pause the cycle for others).
    """
    db.activate_chat(chat_id)
    if not db.get_alerts_enabled(chat_id):
        db.set_alerts_enabled(chat_id, True)
    if db.get_state("wallet_seeded") != "1" and getattr(context, "job_queue", None):
        from services import cycles
        context.job_queue.run_once(cycles.wallet_seed_job, when=2)


def start_trial(chat_id: int, context) -> datetime | None:
    """Grant this chat's one-time free trial if eligible.

    Eligible = trials enabled (TRIAL_HOURS > 0), the chat isn't already entitled,
    and it hasn't used a trial before. Marks the trial used BEFORE granting so a
    racing double-tap can't double-grant, sets a TRIAL_HOURS ``paid_until``, and
    activates the chat. Returns the trial's expiry, or None if not granted.
    """
    if config.TRIAL_HOURS <= 0:
        return None
    if is_paid(chat_id):
        return None
    if db.get_trial_used(chat_id):
        return None
    db.mark_trial_used(chat_id)
    trial_until = datetime.now(timezone.utc) + timedelta(hours=config.TRIAL_HOURS)
    db.set_paid_until(chat_id, trial_until.isoformat())
    activate_entitled(chat_id, context)
    log.info("Free trial started for chat %s (until %s)", chat_id, trial_until.isoformat())
    return trial_until


def require_paid(free_taste: bool = False):
    """Decorator: allow the handler only for entitled chats.

    An un-entitled chat's first gated call auto-starts its one-time free trial
    (see ``start_trial``) and is then allowed through. Once the trial is used and
    expired, the gate shows the paywall. ``free_taste`` is accepted for backward
    compatibility and no longer changes behaviour (the trial supersedes it).
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update, context):
            chat_id = update.effective_chat.id
            if is_paid(chat_id):
                return await func(update, context)
            trial_until = start_trial(chat_id, context)
            if trial_until is not None:
                await update.message.reply_text(
                    f"🎁 <b>Free {config.TRIAL_HOURS}-hour trial started</b> — full "
                    f"access unlocked until "
                    f"<b>{trial_until.strftime('%Y-%m-%d %H:%M UTC')}</b>. Grab a "
                    "pass with /start before it ends.",
                    parse_mode="HTML",
                )
                return await func(update, context)
            await update.message.reply_text(paywall_message(chat_id), parse_mode="HTML")
            return None
        return wrapper
    return decorator

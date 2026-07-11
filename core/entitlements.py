"""Entitlement gate for value commands (pay-to-use).

A chat may run the value commands (/scan, /coin, /wallets, /confluence, /dexs,
/scores) only while it has a live ``paid_until`` entitlement. One exception: the
very first /scan per chat is free (a single taste), tracked in the app_state kv
so it never grants ``active``/alert state to an unpaid chat.

``require_paid`` wraps a PTB command handler. It guards a feature (not money),
so on any ambiguity it denies and shows the paywall rather than erroring.
"""
from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone

import config
from storage import database as db

log = logging.getLogger(__name__)


def is_paid(chat_id: int) -> bool:
    """True iff the chat has a paid_until in the future.

    The operator (OWNER_CHAT_ID) is always treated as paid and never burns the
    free taste. Disabled when OWNER_CHAT_ID is 0 (the default).
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
    return (
        "🔒 <b>This command needs an active pass.</b>\n\n"
        f"Send USDC on <b>Solana</b> to:\n{addr_line}\n\n"
        f"{_plan_lines(chat_id)}{exact_note}\n\n"
        "Then run the matching <code>/paid</code> command with your transaction "
        "signature to unlock.\n"
        "Your first /scan is on the house."
    )


def require_paid(free_taste: bool = False):
    """Decorator: allow the handler only for paid chats.

    If ``free_taste`` is True, the chat's first-ever call is allowed once
    (used only for /scan), then the gate applies.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update, context):
            chat_id = update.effective_chat.id
            if is_paid(chat_id):
                return await func(update, context)
            if free_taste and not db.get_free_used(chat_id):
                db.mark_free_used(chat_id)
                log.info("Free taste used by chat %s", chat_id)
                return await func(update, context)
            await update.message.reply_text(paywall_message(chat_id), parse_mode="HTML")
            return None
        return wrapper
    return decorator

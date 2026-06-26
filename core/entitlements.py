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
    """True iff the chat has a paid_until in the future."""
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


def paywall_message() -> str:
    address = (config.PAYMENT_RECEIVING_ADDRESS or "").strip()
    price = config.PAYMENT_PRICE_USD
    days = config.PAYMENT_VALIDITY_DAYS
    addr_line = (
        f"<code>{address}</code>" if address
        else "<i>(payment address not configured — contact the operator)</i>"
    )
    return (
        "🔒 <b>This command needs an active pass.</b>\n\n"
        f"Send <b>${price:.2f} USDC</b> on <b>Solana</b> to:\n"
        f"{addr_line}\n\n"
        f"Then run <code>/paid &lt;tx_signature&gt;</code> to unlock for "
        f"{days} days.\n"
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
            await update.message.reply_text(paywall_message(), parse_mode="HTML")
            return None
        return wrapper
    return decorator

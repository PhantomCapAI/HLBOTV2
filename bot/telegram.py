"""Single-destination Telegram send layer.

Uses the one PTB Application's bot. Proactive alerts go to every chat that
has toggled the bot on. paid_only is accepted but ignored (single destination).
Supports pinning messages (used for strong confluence alerts).
"""
import asyncio
import io
import logging

from telegram import InputFile
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut

from storage import database as db

log = logging.getLogger(__name__)

_app = None
_send_lock = asyncio.Lock()


def set_application(app) -> None:
    global _app
    _app = app


async def _retry(call, attempts: int = 3):
    async with _send_lock:
        last = None
        for attempt in range(1, attempts + 1):
            try:
                return await call()
            except (TimedOut, NetworkError) as e:
                last = e
                if attempt == attempts:
                    break
                await asyncio.sleep(2 * attempt)
        raise last


async def send_to_chat(chat_id: int, text: str = None, photo: bytes = None,
                       caption: str = None):
    """Send a message to one chat, return the Message object."""
    bot = _app.bot
    if photo is not None:
        return await _retry(lambda: bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(io.BytesIO(photo), filename="alert.png"),
            caption=caption or "",
            parse_mode=ParseMode.HTML,
        ))
    else:
        return await _retry(lambda: bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        ))


async def broadcast(text: str = None, photo: bytes = None, caption: str = None,
                    pin: bool = False) -> bool:
    """Broadcast to all active alert chats. If pin=True, pin the message."""
    sent_any = False
    for chat_id in db.get_alert_chats():
        try:
            msg = await send_to_chat(chat_id, text=text, photo=photo, caption=caption)
            if pin and msg:
                try:
                    await _app.bot.pin_chat_message(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        disable_notification=False,  # notify = True (user asked for it)
                    )
                except Exception as e:
                    log.warning("Could not pin message in %s: %s", chat_id, e)
            sent_any = True
        except Exception as e:
            log.warning("send to %s failed: %s", chat_id, e)
    return sent_any


async def notify_owner(text: str) -> bool:
    """Send an operator-only message.

    Prefers OWNER_CHAT_ID when configured; otherwise falls back to the active
    alert chats (single-operator deployments where the owner bypass is off).
    """
    import config
    owner = config.OWNER_CHAT_ID
    if owner:
        try:
            await send_to_chat(owner, text=text)
            return True
        except Exception as e:
            log.warning("notify_owner to %s failed: %s", owner, e)
            return False
    return await broadcast(text=text)


# --- compatibility shims (paid_only ignored) ---
async def send_alert(message: str, paid_only: bool = False) -> None:
    await broadcast(text=message)


async def send_photo_alert(image_bytes: bytes, caption: str, paid_only: bool = False) -> None:
    await broadcast(photo=image_bytes, caption=caption)

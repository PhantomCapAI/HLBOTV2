"""Single-destination Telegram send layer.

Uses the one PTB Application's bot (set at startup) — no second Bot instance.
Proactive alerts go to every chat that has toggled the bot on with alerts
enabled (the personal-tool model: your DM). The send_alert / send_photo_alert
shims keep the wallet tracker's existing calls working unchanged; paid_only is
accepted but ignored, since there is a single destination.
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


async def send_to_chat(chat_id: int, text: str = None, photo: bytes = None, caption: str = None) -> None:
    bot = _app.bot
    if photo is not None:
        await _retry(lambda: bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(io.BytesIO(photo), filename="alert.png"),
            caption=caption or "",
            parse_mode=ParseMode.HTML,
        ))
    else:
        await _retry(lambda: bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        ))


async def broadcast(text: str = None, photo: bytes = None, caption: str = None) -> bool:
    sent_any = False
    for chat_id in db.get_alert_chats():
        try:
            await send_to_chat(chat_id, text=text, photo=photo, caption=caption)
            sent_any = True
        except Exception as e:
            log.warning("send to %s failed: %s", chat_id, e)
    return sent_any


# --- compatibility shims used by trackers/wallet_tracker.py (paid_only ignored) ---
async def send_alert(message: str, paid_only: bool = False) -> None:
    await broadcast(text=message)


async def send_photo_alert(image_bytes: bytes, caption: str, paid_only: bool = False) -> None:
    await broadcast(photo=image_bytes, caption=caption)

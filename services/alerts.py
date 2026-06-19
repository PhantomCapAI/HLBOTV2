"""Single dedup-gated alert choke point."""
import logging

from storage import database as db
from bot import telegram as tg

log = logging.getLogger(__name__)


async def maybe_send(alert_type: str, key: str, text: str,
                     cooldown_minutes: int = 240, photo: bytes = None,
                     pin: bool = False) -> bool:
    if db.alert_already_sent(alert_type, key, cooldown_minutes=cooldown_minutes):
        return False
    if photo is not None:
        ok = await tg.broadcast(photo=photo, caption=text, pin=pin)
    else:
        ok = await tg.broadcast(text=text, pin=pin)
    if ok:
        db.record_alert(alert_type, key)
    return ok

"""Single dedup-gated alert choke point.

Every coin/correlation alert goes through here: check the SQLite dedup ledger,
broadcast once to the single destination, record. Guarantees no duplicate
notifications. (The wallet tracker keeps its own finely-tuned dedup and sends
through the same bot.telegram broadcast underneath.)
"""
import logging

from storage import database as db
from bot import telegram as tg

log = logging.getLogger(__name__)


async def maybe_send(alert_type: str, key: str, text: str,
                     cooldown_minutes: int = 240, photo: bytes = None) -> bool:
    if db.alert_already_sent(alert_type, key, cooldown_minutes=cooldown_minutes):
        return False
    if photo is not None:
        ok = await tg.broadcast(photo=photo, caption=text)
    else:
        ok = await tg.broadcast(text=text)
    if ok:
        db.record_alert(alert_type, key)
    return ok

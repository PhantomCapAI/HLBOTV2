"""HL Intel — unified personal Hyperliquid intelligence bot.

Single entry point. One PTB Application owns the event loop:
  - command handlers (interactive: /scan /coin /wallets /confluence /status, plus
    the /start /stop /alerts toggle),
  - JobQueue repeating jobs (wallet scan, coin scan, retention),
  - one rate-limited async Hyperliquid client and one SQLite store underneath.
"""
import logging

import config
from core.logging import setup_logging
from storage import database as db
from bot import telegram as tg
from bot import handlers as h
from services import cycles

from telegram.ext import Application, CommandHandler

log = setup_logging()


async def _post_init(app: Application) -> None:
    db.init_db()
    tg.set_application(app)
    if config.SEND_STARTUP_MESSAGE and db.get_alert_chats():
        await tg.broadcast(text="🚀 <b>HL Intel is live.</b>")
    log.info("HL Intel started. Active chats: %s", db.get_active_chats())


def main() -> None:
    problems = config.validate()
    if problems:
        for p in problems:
            log.error("CONFIG: %s", p)
        raise SystemExit(1)

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", h.start))
    app.add_handler(CommandHandler("stop", h.stop_cmd))
    app.add_handler(CommandHandler("alerts", h.toggle_alerts))
    app.add_handler(CommandHandler("scan", h.scan))
    app.add_handler(CommandHandler("coin", h.coin_cmd))
    app.add_handler(CommandHandler("wallets", h.wallets_cmd))
    app.add_handler(CommandHandler("confluence", h.confluence_cmd))
    app.add_handler(CommandHandler("dexs", h.dexs_cmd))
    app.add_handler(CommandHandler("status", h.status_cmd))
    app.add_handler(CommandHandler("scores", h.scores_cmd))
    app.add_handler(CommandHandler("help", h.help_cmd))

    jq = app.job_queue
    jq.run_repeating(cycles.wallet_job, interval=config.WALLET_SCAN_INTERVAL_SECONDS, first=10)
    jq.run_repeating(cycles.coin_job, interval=config.COIN_SCAN_INTERVAL_SECONDS, first=25)
    jq.run_repeating(cycles.prune_job, interval=24 * 3600, first=3600)

    log.info("HL Intel running (wallet=%ss, coin=%ss)...",
             config.WALLET_SCAN_INTERVAL_SECONDS, config.COIN_SCAN_INTERVAL_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()

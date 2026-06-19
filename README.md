# HL Intel — unified personal Hyperliquid intelligence bot

One Telegram bot, one process, on Zeabur. Combines:

- **Coin technical scanner** (your Grok bot): universe screen → multi-timeframe
  confluence score → deep dive → Grok setups (entry/stop/targets), via `/scan`,
  `/coin`, and proactive high-score alerts.
- **Wallet tracker** (the GitHub engine): whale position opens/adds, whale
  confluence, liquidation-risk, funding/OI surges, wallet PnL health.
- **Correlation** (the payoff): when a strong technical setup lines up with
  multiple tracked whales on the same side → a prioritized `STRONG CONFLUENCE`
  alert.

Everything goes to **your DM**, controlled by an on/off toggle. No channels.

## Commands

- `/start` — turn on (begins background scanning + proactive alerts to this chat)
- `/stop` — turn off (fully idle, no API calls)
- `/alerts` — pause/resume just the proactive pushes
- `/scan` — manual coin scan (works anytime)
- `/coin SYMBOL` — deep dive one coin
- `/wallets` — current tracked-wallet positioning
- `/confluence` — latest wallet × setup confluence
- `/status` — show state

After `/start`, the wallet baseline seeds for one cycle (silent), then
change-alerts go live.

## Architecture

```
app.py                      one PTB Application: handlers + JobQueue + run_polling
config/        env-driven settings (no hardcoded secrets)
core/          logging, async weight-budget rate limiter
integrations/  hyperliquid (async, backoff + weight limiter), grok (async)
storage/       SQLite: snapshots, dedup ledger, subscribers, retention
scanner/       indicators (pure), screener (async), setups pipeline
trackers/      wallet_tracker (whale/confluence/liq/funding/OI detection)
services/      correlation, alerts (dedup choke point), cycles (jobs), digest
bot/           telegram (single-destination send), handlers, formatting, charts
```

## Run locally

```sh
cp .env.example .env        # fill TELEGRAM_BOT_TOKEN (and GROK_API_KEY if you have one)
pip install -r requirements.txt
python app.py
```

## Deploy on Zeabur

1. Push this folder to a repo (or point Zeabur at it).
2. Create a service from the Dockerfile.
3. Add a **persistent volume mounted at `/data`** (keeps SQLite across deploys).
4. Set env vars from `.env.example` (at minimum `TELEGRAM_BOT_TOKEN`;
   `HL_INTEL_DB_PATH=/data/hl_intel.db`).
5. Deploy. DM your bot `/start`.

Notes: it's a long-running polling worker (no inbound HTTP), so Zeabur health =
process liveness; it restarts on crash and resumes from SQLite. Charts are off by
default (`ENABLE_CHARTS=false`); enabling them needs ~1GB RAM (matplotlib/Pillow).

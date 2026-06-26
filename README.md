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

Everything goes to **your DM**. Access is **pay-to-use** (see below). No channels.

## Access — pay-to-use ($3 USDC, Solana)

The value commands and proactive alerts are gated behind an on-chain payment:

- **$3.00 USDC on Solana** opens access for up to **3 days** (~$1/day). Pay to
  our receiving address, then run `/paid <tx_signature>`.
- `/paid` verifies the payment **on-chain** (re-implemented in-bot, independent
  of any gateway): correct USDC mint, amount, recipient, success, and freshness.
  It **fails closed** — if anything is uncertain, it does not grant access.
- **Replay-protected:** each transaction signature can be redeemed only once.
- **One free `/scan`** per chat — a single taste before the gate applies.
- After the window expires the value commands re-gate; `/start` again to repay.
- The receiving address is configured **only via the environment**
  (`PAYMENT_RECEIVING_ADDRESS`); it is never committed to this repo.

Gated (need an active paid window): `/scan` (first one free), `/coin`,
`/wallets`, `/confluence`, `/dexs`, `/scores`.
Always free: `/start`, `/paid`, `/stop`, `/alerts`, `/status`, `/help`.

## Commands

- `/start` — how to pay / refill (every `/start` routes through payment)
- `/paid <tx>` — redeem a Solana USDC payment to activate (up to 3 days)
- `/stop` — turn off (fully idle, no API calls)
- `/alerts` — pause/resume just the proactive pushes
- `/scan` — manual coin scan (first one free, then paid)
- `/coin SYMBOL` — deep dive one coin (paid)
- `/wallets` — current tracked-wallet positioning (paid)
- `/confluence` — latest wallet × setup confluence (paid)
- `/status` — show state

After `/paid` succeeds, the wallet baseline seeds for one cycle (silent), then
change-alerts go live for the paid window.

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
cp .env.example .env        # fill TELEGRAM_BOT_TOKEN and PAYMENT_RECEIVING_ADDRESS
pip install -r requirements.txt          # (GROK_API_KEY optional)
python app.py
```

The app refuses to start unless both `TELEGRAM_BOT_TOKEN` and
`PAYMENT_RECEIVING_ADDRESS` are set (no paywall without a payout address).

## Deploy on Zeabur

1. Push this folder to a repo (or point Zeabur at it).
2. Create a service from the Dockerfile.
3. Add a **persistent volume mounted at `/data`** (keeps SQLite across deploys).
4. Set env vars from `.env.example` (at minimum `TELEGRAM_BOT_TOKEN`,
   `PAYMENT_RECEIVING_ADDRESS`, and `HL_INTEL_DB_PATH=/data/hl_intel.db`).
5. Deploy. DM your bot `/start`.

Notes: it's a long-running polling worker (no inbound HTTP), so Zeabur health =
process liveness; it restarts on crash and resumes from SQLite. Charts are off by
default (`ENABLE_CHARTS=false`); enabling them needs ~1GB RAM (matplotlib/Pillow).

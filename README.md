# MOONBOYHL

**A Telegram bot for live Hyperliquid intel — whale tracking, multi-timeframe technical setups, and wallet health scores, delivered straight to your DM.**

MOONBOYHL watches the Hyperliquid perps universe and a curated set of tracked wallets, then surfaces the moments worth knowing about: high-confluence technical setups (entry / stop / targets), whale position opens, adds, trims, and exits, funding/OI surges, liquidation risk, and — the payoff — when a strong technical setup lines up with multiple whales on the same side.

## Try it

👉 **[t.me/MOONBOYHL_bot](https://t.me/MOONBOYHL_bot)**

Open the bot and send `/scan`. **Your first scan is free — no signup, no wallet connect, nothing to install.** After that, the value commands ask for a small pass (see [Access & pricing](#access--pricing)).

## Commands

| Command | What it does | Needs a pass? |
| --- | --- | --- |
| `/scan` | Scan the Hyperliquid universe for multi-timeframe setups (entry / stop / targets) | First one free, then yes |
| `/coin SYMBOL` | Deep dive on one coin, e.g. `/coin HYPE` | Yes |
| `/wallets` | Current tracked-wallet positioning (who's long/short what, and how big) | Yes |
| `/confluence` | Latest wallet × technical-setup confluence | Yes |
| `/dexs` | List builder-deployed (HIP-3) perp dexs and their markets (equities / metals / FX) | Yes |
| `/scores` | Tracked wallets ranked by current health score | Yes |
| `/status` | Show your current state (active pass, alerts, scan cadence) | Free |
| `/alerts` | Toggle the proactive push alerts on/off | Free |
| `/start` | How to pay / refill your pass | Free |
| `/paid <plan> <tx>` | Redeem a Solana USDC payment to activate (`plan` = `week` or `month`) | Free |
| `/stop` | Turn the scanner off | Free |
| `/help` | Show the command list | Free |

When you have an active pass, MOONBOYHL also **pushes** high-confluence setups and notable whale activity to you automatically (toggle with `/alerts`).

## Access & pricing

The value commands and proactive alerts are gated behind a one-time on-chain payment:

- Two tiered passes on Solana USDC: **1 week = $10 USDC** or **1 month = $30 USDC**.
- Send the exact amount for your plan to the bot's receiving address (shown in-bot via `/start`), then run the matching command — **`/paid week <tx_signature>`** or **`/paid month <tx_signature>`**.
- Paying again **refills** your time (a new pass extends from whatever access you have left, never shortening it).
- The bot verifies the payment **directly on-chain** — it confirms the transaction succeeded, is recent, paid the correct USDC mint to the correct address, and met the required amount. It **fails closed**: if anything is uncertain, access is *not* granted.
- **Replay-protected** — each transaction signature can be redeemed only once.
- Your **first `/scan` is free**, once per chat, before the gate applies.
- When your window expires, the value commands re-gate; pay again to refill.

The bot only ever *reads* the payment transaction to verify it. It never has custody of, or access to, your funds.

## Self-host

MOONBOYHL is a single long-running Python process (Telegram long-polling, SQLite for state). Run it with Docker or directly with Python.

```sh
cp .env.example .env     # fill in your values
pip install -r requirements.txt
python app.py
```

Or build the container:

```sh
docker build -t moonboyhl .
docker run --env-file .env -v "$PWD/data:/data" moonboyhl
```

The app refuses to start unless both `TELEGRAM_BOT_TOKEN` and `PAYMENT_RECEIVING_ADDRESS` are set (no paywall without a payout address).

### Environment variables

Set these in your environment or `.env` — **use your own values; the examples below are placeholders.**

| Variable | Required | Example / placeholder | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | ✅ | `123456789:your-telegram-bot-token` | From [@BotFather](https://t.me/BotFather) |
| `PAYMENT_RECEIVING_ADDRESS` | ✅ | `YourSolanaUsdcAddressHere` | Your Solana address that receives USDC |
| `SOLANA_RPC_URL` | – | `https://api.mainnet-beta.solana.com` | Any Solana mainnet RPC endpoint |
| `OWNER_CHAT_ID` | – | `0` | Your Telegram chat id to bypass the paywall (`0` = disabled) |
| `PAYMENT_PRICE_WEEK_USD` | – | `10.00` | Price of the 1-week pass, in USDC |
| `PAYMENT_PRICE_MONTH_USD` | – | `30.00` | Price of the 1-month pass, in USDC |
| `PAYMENT_DAYS_WEEK` | – | `7` | Access length of the week pass, in days |
| `PAYMENT_DAYS_MONTH` | – | `30` | Access length of the month pass, in days |
| `PAYMENT_TX_MAX_AGE_DAYS` | – | `3` | How fresh a redeemed tx must be (independent of pass length) |
| `GROK_API_KEY` | – | `your-xai-api-key` | Optional; setups fall back to a local generator if unset |
| `HL_INTEL_DB_PATH` | – | `/data/hl_intel.db` | Point at a persistent volume to keep state across restarts |
| `WALLET_SCAN_INTERVAL_SECONDS` | – | `180` | How often tracked wallets are polled |
| `COIN_SCAN_INTERVAL_SECONDS` | – | `300` | How often the coin scanner runs |
| `ENABLE_CHARTS` | – | `false` | Chart images are off by default; enabling needs ~1GB RAM |

See [`.env.example`](.env.example) for the full set of tunable thresholds (whale size, funding/OI surge, liquidation proximity, correlation cooldowns, etc.).

State (subscribers, payment ledger, snapshots) lives in SQLite at `HL_INTEL_DB_PATH`. Mount it on a persistent volume so passes and the replay-protection ledger survive restarts.

## What this is — and isn't

- ✅ It **surfaces signals**: technical setups, whale positioning, confluence, and wallet health, so you can look faster.
- ✅ It **only reads** public on-chain and market data, plus the one payment transaction it verifies to activate your pass.
- ❌ It is **not financial advice.** Scores and setups are informational; do your own research.
- ❌ It **does not place trades**, manage positions, or touch your trading funds or keys. It never asks for them.

Markets are risky and signals are not guarantees. Trade at your own risk.

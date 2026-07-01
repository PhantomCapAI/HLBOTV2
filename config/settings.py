"""Centralized, environment-driven configuration.

Merges the repo scanner's `config.py` (env-based thresholds) with the 5-file
scanner's `CONFIG` dataclass (indicator engine settings). Single source of truth.
No secrets are hardcoded; everything sensitive comes from the environment.
"""
import os
from pathlib import Path
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

_ROOT = Path(__file__).resolve().parent.parent


def _f(name: str, default) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---- Telegram (your personal bot) ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ---- LLM (Grok / xAI) ----
GROK_API_KEY = os.getenv("GROK_API_KEY") or os.getenv("XAI_API_KEY")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4")
GROK_TIMEOUT_SECONDS = _f("GROK_TIMEOUT_SECONDS", 25.0)

# ---- Storage ----
DB_PATH = Path(os.getenv("HL_INTEL_DB_PATH", str(_ROOT / "hl_intel.db")))
WATCHLIST_PATH = os.getenv("HL_INTEL_WATCHLIST_PATH", str(_ROOT / "watchlist.json"))
RETENTION_DAYS = _i("RETENTION_DAYS", 14)

# ---- Hyperliquid client pacing / weight budget ----
HL_INFO_MIN_REQUEST_INTERVAL_SECONDS = _f("HL_INFO_MIN_REQUEST_INTERVAL_SECONDS", 0.75)
HL_INFO_MAX_RETRIES = _i("HL_INFO_MAX_RETRIES", 5)
HL_HTTP_TIMEOUT_SECONDS = _f("HL_HTTP_TIMEOUT_SECONDS", 20.0)
HL_WEIGHT_BUDGET = _i("HL_WEIGHT_BUDGET", 1200)
HL_WEIGHT_WINDOW_SECONDS = _f("HL_WEIGHT_WINDOW_SECONDS", 60.0)
HL_WEIGHT_HEADROOM = _f("HL_WEIGHT_HEADROOM", 0.85)

# ---- Scan cadence ----
WALLET_SCAN_INTERVAL_SECONDS = _i("WALLET_SCAN_INTERVAL_SECONDS", 180)
COIN_SCAN_INTERVAL_SECONDS = _i("COIN_SCAN_INTERVAL_SECONDS", 300)

# ---- Builder-deployed perps (HIP-3): equities / metals / FX ----
# Off by default. When on, the coin scanner also sweeps builder dexs.
ENABLE_BUILDER_DEXS = _b("ENABLE_BUILDER_DEXS", False)
# Comma-separated dex-name whitelist (recommended). Empty => auto-discover all.
BUILDER_DEXS = [d.strip() for d in os.getenv("BUILDER_DEXS", "").split(",") if d.strip()]
# Builder markets are thinner than crypto, so they get their own liquidity floors.
BUILDER_MIN_VOLUME = _f("BUILDER_MIN_VOLUME", 250_000)
BUILDER_MIN_OI = _f("BUILDER_MIN_OI", 100_000)

# ---- Coin scanner / setups ----
ACCOUNT_EQUITY = _f("ACCOUNT_EQUITY", 5000.0)
RISK_PCT = _f("RISK_PCT", 0.01)
MIN_SCORE_FOR_ALERT = _f("MIN_SCORE_FOR_ALERT", 80.0)
ENABLE_CHARTS = _b("ENABLE_CHARTS", False)
SEND_STARTUP_MESSAGE = _b("SEND_STARTUP_MESSAGE", False)
# When toggled off, do no background work at all (no API calls).
IDLE_WHEN_OFF = _b("IDLE_WHEN_OFF", True)

# ---- Wallet thresholds (from repo config.py) ----
WHALE_POSITION_THRESHOLD_USD = _f("WHALE_POSITION_THRESHOLD_USD", 500_000)
FUNDING_RATE_SPIKE_THRESHOLD = _f("FUNDING_RATE_SPIKE_THRESHOLD", 0.0001)
# OI signal noise controls. OI on HL is reported in *base-asset units* (coins);
# everywhere we show or floor it we convert to USD notional (coins * markPx).
OI_SURGE_PCT_THRESHOLD = _f("OI_SURGE_PCT_THRESHOLD", 40.0)
# Minimum OI *USD notional* (coins * markPx) for a market to be eligible — skips
# thin markets whose % swings are meaningless.
MIN_OI_FOR_SURGE = _f("MIN_OI_FOR_SURGE", 50_000_000)
# Per-coin cooldown; OI movers are consolidated into ONE 'OI Flow' digest per
# cycle showing at most OI_DIGEST_MAX top movers (by |%change|).
OI_SURGE_COOLDOWN_MINUTES = _i("OI_SURGE_COOLDOWN_MINUTES", 240)
OI_DIGEST_MAX = _i("OI_DIGEST_MAX", 6)
# Hard ceiling on OI USD notional — anything above this is a data glitch (total
# crypto OI across all venues is well under $100B), so drop it rather than alert.
OI_NOTIONAL_SANITY_MAX_USD = _f("OI_NOTIONAL_SANITY_MAX_USD", 100_000_000_000)
LIQ_PROXIMITY_THRESHOLD_PCT = _f("LIQ_PROXIMITY_THRESHOLD_PCT", 10.0)
LIQ_PROXIMITY_DANGER_PCT = _f("LIQ_PROXIMITY_DANGER_PCT", 5.0)
MIN_NOTIONAL_FOR_LIQ_ALERT = _f("MIN_NOTIONAL_FOR_LIQ_ALERT", 5_000_000)
# Wallet-health (HOT STREAK / SELF-IMPLODING / etc.) flags are consolidated into
# ONE 'Wallet Health' digest per cycle listing at most WALLET_HEALTH_DIGEST_MAX
# wallets. Per-wallet/per-state cooldown is WALLET_PERFORMANCE_COOLDOWN_MINUTES.
WALLET_HEALTH_DIGEST_MAX = _i("WALLET_HEALTH_DIGEST_MAX", 8)

# ---- Pay-to-activate (Solana USDC) ----
# Every /start re-charges $3.00 USDC on Solana ($1/day); paying via /paid <tx>
# opens the chat for up to PAYMENT_VALIDITY_DAYS (3). After that window the value
# commands re-gate and the user must repay. The receiving address is env-only.
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
PAYMENT_RECEIVING_ADDRESS = os.getenv("PAYMENT_RECEIVING_ADDRESS", "")
PAYMENT_PRICE_USD = _f("PAYMENT_PRICE_USD", 3.00)
PAYMENT_VALIDITY_DAYS = _i("PAYMENT_VALIDITY_DAYS", 3)
# Operator's Telegram chat id — bypasses the paywall (never pays, never burns
# the free taste). 0 disables the bypass.
OWNER_CHAT_ID = _i("OWNER_CHAT_ID", 0)

# ---- Wallet identity & profile layer ----
# Skill tier cutoffs on smart_score (trailing week+month ROI minus risk penalties):
#   Sharp   >= WALLET_TIER_SHARP
#   Solid   >= WALLET_TIER_SOLID   (and < SHARP)
#   Average >= WALLET_TIER_AVERAGE (and < SOLID)
#   Sloppy   < WALLET_TIER_AVERAGE
WALLET_TIER_SHARP = _f("WALLET_TIER_SHARP", 25.0)
WALLET_TIER_SOLID = _f("WALLET_TIER_SOLID", 10.0)
WALLET_TIER_AVERAGE = _f("WALLET_TIER_AVERAGE", 0.0)
# Current-state classifier: hot = day&week ROI above +eps; cold = both below -eps.
WALLET_STATE_ROI_EPS = _f("WALLET_STATE_ROI_EPS", 0.0)
# Trailing window (minutes) for the flailing signal (flips + stress-adds / hour).
WALLET_FLAIL_WINDOW_MIN = _i("WALLET_FLAIL_WINDOW_MIN", 60)

# ---- Open-interest trend / funding crowding (market context) ----
# Lookback windows (minutes) for the ΔOI% deltas. The longer window is the
# primary classifier when enough history exists, else the shorter one is used.
OI_LOOKBACK_SHORT_MIN = _i("OI_LOOKBACK_SHORT_MIN", 60)
OI_LOOKBACK_LONG_MIN = _i("OI_LOOKBACK_LONG_MIN", 240)
# ΔOI% thresholds: >= RISE = "rising" (building); <= -FALL = "unwind".
OI_RISE_PCT = _f("OI_RISE_PCT", 5.0)
OI_FALL_PCT = _f("OI_FALL_PCT", 5.0)
# "Elevated funding" reuses EXTREME_FUNDING_HR from scanner/screener.py.

# ---- Whale exit / flip / trim detection (the other half of WHALE ADDING) ----
# A tracked position that shrinks past CLOSE_PCT (of size) — or vanishes — fires
# WHALE CLOSED; a side reversal fires WHALE FLIPPED. Trims (partial reductions in
# the TRIM_PCT..CLOSE_PCT band) are off by default to avoid spam.
WHALE_CLOSE_PCT = _f("WHALE_CLOSE_PCT", 80.0)          # size shrink >= this % = a close
WHALE_TRIM_ENABLED = _b("WHALE_TRIM_ENABLED", False)   # alert on partial trims at all
WHALE_TRIM_PCT = _f("WHALE_TRIM_PCT", 30.0)            # lower bound of the trim band
WHALE_EXIT_COOLDOWN_MINUTES = _i("WHALE_EXIT_COOLDOWN_MINUTES", 240)
# Tiny-base fix: when a prior position was negligible, "+6939%" is noise — relabel
# the add as OPENED NEW (absolute size, no percentage).
WHALE_TINY_BASE_USD = _f("WHALE_TINY_BASE_USD", 5_000.0)   # prev notional below this = tiny
WHALE_TINY_BASE_PCT = _f("WHALE_TINY_BASE_PCT", 5.0)       # ...or below this % of the new size

# ---- Correlation (wallet x technical confluence) ----
CORRELATION_MIN_SCORE = _f("CORRELATION_MIN_SCORE", 60.0)
CORRELATION_MIN_WHALES = _i("CORRELATION_MIN_WHALES", 2)
CORRELATION_COOLDOWN_MINUTES = _i("CORRELATION_COOLDOWN_MINUTES", 180)

# ---- Whale-confluence digest + quality floor ----
# Confluence groups are consolidated into ONE 'Whale Confluence' digest per cycle
# instead of one message per coin. Quality floor first, then rank by combined
# smart score: the top CONFLUENCE_DIGEST_DETAIL get the full per-wallet breakdown,
# the next up to CONFLUENCE_DIGEST_MAX are one-liners, the rest are dropped.
CONFLUENCE_DIGEST_MAX = _i("CONFLUENCE_DIGEST_MAX", 8)
CONFLUENCE_DIGEST_DETAIL = _i("CONFLUENCE_DIGEST_DETAIL", 3)
# Drop a group whose combined smart score is below this floor (kills the
# "+13.9, same two wallets, one negative" noise).
CONFLUENCE_MIN_COMBINED_SMART = _f("CONFLUENCE_MIN_COMBINED_SMART", 15.0)
# A wallet counts as "strong" at/above this individual smart score; a group must
# contain at least CONFLUENCE_MIN_DISTINCT_STRONG of them (filters the same low/
# negative-score pair repeating across many coins).
CONFLUENCE_STRONG_WALLET_SMART = _f("CONFLUENCE_STRONG_WALLET_SMART", 10.0)
CONFLUENCE_MIN_DISTINCT_STRONG = _i("CONFLUENCE_MIN_DISTINCT_STRONG", 1)

# ---- Automated wallet discovery (skill-ranked promotion) ----
# A slow background job scores leaderboard wallets by smart_score and suggests
# genuinely skilled traders not already tracked. Human-gated by default: it only
# *suggests* (writes candidates + DMs the owner); you approve with /track <addr>.
DISCOVERY_ENABLED = _b("DISCOVERY_ENABLED", True)
DISCOVERY_INTERVAL_HOURS = _f("DISCOVERY_INTERVAL_HOURS", 8.0)
# How deep into the leaderboard to scan (rows are account-value ranked). The
# leaderboard itself is a single cheap GET at any depth; the cost is the per-
# wallet position fetch, which is paged across cycles (DISCOVERY_SCAN_PAGE_SIZE)
# so a deep sweep never hammers the API. Deeper = surface strong mid-ranked
# wallets that account-size ranking hides (e.g. #300 outperforming #10).
DISCOVERY_SCAN_TOP_N = _i("DISCOVERY_SCAN_TOP_N", 500)
# Leaderboard rows to process per cycle (bounds per-cycle clearinghouseState
# fetches). The scan cursor advances each cycle and wraps, sweeping the full
# DISCOVERY_SCAN_TOP_N depth over ceil(TOP_N / PAGE_SIZE) cycles. At the /info
# min-interval (~0.75s) this is ~80 fetches/min, so 150 ≈ under ~2 min/cycle.
DISCOVERY_SCAN_PAGE_SIZE = _i("DISCOVERY_SCAN_PAGE_SIZE", 150)
# Ignore dust accounts — skill on a tiny book isn't a tracking signal.
DISCOVERY_MIN_ACCOUNT_VALUE = _f("DISCOVERY_MIN_ACCOUNT_VALUE", 100_000)
# Minimum smart_score for a wallet to be *suggested*.
DISCOVERY_MIN_SMART_SCORE = _f("DISCOVERY_MIN_SMART_SCORE", 10.0)
# Exclude lottery-ticket books: cap on book leverage (exposure / equity).
DISCOVERY_MAX_LEVERAGE = _f("DISCOVERY_MAX_LEVERAGE", 20.0)
# Market-maker / delta-neutral detection (from current positions):
#   flag a wallet holding >= MM_MIN_COINS coins whose net exposure is a small
#   fraction (<= MM_NET_GROSS_RATIO) of gross exposure (balanced both sides).
DISCOVERY_MM_MIN_COINS = _i("DISCOVERY_MM_MIN_COINS", 6)
DISCOVERY_MM_NET_GROSS_RATIO = _f("DISCOVERY_MM_NET_GROSS_RATIO", 0.25)
# Optional silent auto-promotion (off by default). When on, only wallets at/above
# AUTO_ADD_MIN_SMART are auto-tracked, at most AUTO_ADD_MAX_PER_RUN per run.
DISCOVERY_AUTO_ADD = _b("DISCOVERY_AUTO_ADD", False)
DISCOVERY_AUTO_ADD_MIN_SMART = _f("DISCOVERY_AUTO_ADD_MIN_SMART", 25.0)
DISCOVERY_AUTO_ADD_MAX_PER_RUN = _i("DISCOVERY_AUTO_ADD_MAX_PER_RUN", 3)
# Auto-retire a *discovered* tracked wallet after this many consecutive discovery
# runs with negative week AND month ROI. Hand-picked watchlist entries are never
# auto-retired.
DISCOVERY_RETIRE_CYCLES = _i("DISCOVERY_RETIRE_CYCLES", 3)
# Raw discovery suggestions are consolidated into ONE digest per cycle (ranked by
# smart score, highest first), showing at most DISCOVERY_DIGEST_MAX wallets. The
# rest are still written to the candidate store and reachable via /candidates +
# /track. This is a lower-priority feed — the promotion pings are the real signal
# — so it can be toggled off entirely.
DISCOVERY_RAW_DIGEST_ENABLED = _b("DISCOVERY_RAW_DIGEST_ENABLED", True)
DISCOVERY_DIGEST_MAX = _i("DISCOVERY_DIGEST_MAX", 10)

# ---- Discovery: proven-candidate promotion (the real signal) ----
# Discovery records a per-cycle observation for every wallet that clears the full
# filter set. A candidate is only PROMOTED ("⭐ PROVEN — consider tracking") once
# it has sustained performance: seen in >= MIN_CYCLES observations spanning >=
# MIN_DAYS, week-ROI positive every time, and leverage under DISCOVERY_MAX_LEVERAGE
# throughout. A wallet that spikes once and vanishes never gets promoted.
DISCOVERY_PROVEN_ENABLED = _b("DISCOVERY_PROVEN_ENABLED", True)
DISCOVERY_PROVEN_MIN_CYCLES = _i("DISCOVERY_PROVEN_MIN_CYCLES", 3)
DISCOVERY_PROVEN_MIN_DAYS = _f("DISCOVERY_PROVEN_MIN_DAYS", 3.0)


@dataclass
class IndicatorConfig:
    """Indicator-engine settings (ported from the 5-file engine.Config)."""
    timeframes: list = field(default_factory=lambda: ["15m", "1h", "4h"])
    lookback_bars: int = 300
    account_equity: float = ACCOUNT_EQUITY
    risk_pct: float = RISK_PCT
    atr_stop_mult: float = 1.4
    min_bars: int = 60
    leverage: dict = field(default_factory=lambda: {"BTC": 40, "default": 5})


CONFIG = IndicatorConfig()


def validate() -> list[str]:
    """Return a list of fatal config problems (empty = OK)."""
    problems = []
    if not TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN is not set.")
    if not PAYMENT_RECEIVING_ADDRESS:
        problems.append(
            "PAYMENT_RECEIVING_ADDRESS is not set — refusing to run a paywall "
            "with no payout address."
        )
    return problems

"""Scheduled cycles + JobQueue callbacks.

- wallet cycle: ported from the repo main.run_cycle (leaderboard + funding ->
  save -> whale/confluence/liq/funding/OI checks -> weekly digest).
- coin cycle: coin_scan -> alert strong setups -> correlate with wallet
  positioning -> alert strong confluence.
- All gated by the on/off toggle (IDLE_WHEN_OFF => no work when off).
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
from integrations import hyperliquid as hl
from storage import database as db
from trackers import wallet_tracker as wt
from scanner.setups import coin_scan, correlation_scan
from services import correlation as corr
from services import alerts as alerts_svc
from services import digest as digest_svc
from bot import telegram as tg
from bot import formatting_wallet as fw
from bot.formatting import format_setup
from core import identity

log = logging.getLogger(__name__)

_last_setups: list[dict] = []
_last_confluence_snapshot: str | None = None

# Process-level cold-start guards. The persistent `wallet_seeded` DB flag survives
# restarts/redeploys, so on its own it does NOT stop the first cycle after a
# restart from flooding (every subsystem diffs against a stale/empty baseline at
# once). These reset to True on every process start; the first wallet/coin cycle
# after start runs in seed mode (refresh baselines silently, no broadcasts).
_wallet_cold_start_pending = True
_coin_cold_start_pending = True


def last_confluence_snapshot() -> str | None:
    return _last_confluence_snapshot


def _should_run() -> bool:
    return db.is_any_active() or not config.IDLE_WHEN_OFF


# --------------------------- watchlist helpers (ported from repo main.py) ---------------------------
def load_deploy_watchlist() -> list[dict]:
    path = Path(config.WATCHLIST_PATH)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        log.error("Failed to read watchlist %s: %s", path, exc)
        return []
    rows = []
    for wallet in payload.get("wallets", []):
        address = str(wallet.get("address", "")).lower()
        if not address.startswith("0x"):
            continue
        label = wallet.get("label") or wallet.get("priority") or "watch"
        priority = wallet.get("priority") or "B"
        rows.append({
            "ethAddress": address,
            "rank": f"{priority}:{label}",
            "accountValue": 0,
            "windowPerformances": {"day": {"pnl": 0}, "week": {"pnl": 0}},
            "watch_tokens": [str(t).upper() for t in wallet.get("tokens", [])],
            "min_notional_change_usd": float(wallet.get("min_notional_change_usd", 0) or 0),
            "watch_notes": wallet.get("notes") or "",
        })
    return rows


def manual_watch_rows(top50_addresses: set[str]) -> list[dict]:
    rows_by_address = {}
    for wallet in db.get_watch_wallets():
        address = wallet["address"]
        if address in top50_addresses:
            continue
        rows_by_address[address] = {
            "ethAddress": address,
            "rank": wallet["name"] or wallet["label"].replace("_", " ").title(),
            "accountValue": 0,
            "windowPerformances": {"day": {"pnl": 0}, "week": {"pnl": 0}},
        }
    for wallet in load_deploy_watchlist():
        address = wallet["ethAddress"]
        if address in top50_addresses:
            continue
        rows_by_address[address] = wallet
    # Wallets promoted by discovery (approved via /track or auto-added).
    for address in db.get_tracked_candidate_addresses():
        if address in top50_addresses or address in rows_by_address:
            continue
        rows_by_address[address] = {
            "ethAddress": address,
            "rank": "discovered",
            "accountValue": 0,
            "windowPerformances": {"day": {"pnl": 0}, "week": {"pnl": 0}},
        }
    return list(rows_by_address.values())


def apply_watch_account_values(watch_rows: list[dict], raw_positions: dict) -> None:
    for row in watch_rows:
        state = raw_positions.get(row["ethAddress"], {})
        margin = state.get("marginSummary", {})
        try:
            row["accountValue"] = float(margin.get("accountValue", 0) or 0)
        except (TypeError, ValueError):
            row["accountValue"] = 0


# --------------------------- wallet cycle ---------------------------
async def _wallet_cycle(seed_mode: bool = False) -> None:
    label = "seed" if seed_mode else "wallet-scan"
    log.info("Starting %s cycle...", label)
    try:
        leaderboard_full, assets = await asyncio.gather(
            hl.get_leaderboard(top_n=50),
            hl.get_funding_and_oi(),
        )
        db.save_leaderboard(leaderboard_full)
        db.save_funding(assets)

        await wt.check_funding_spikes(assets, seed_mode)
        await wt.check_oi_surges(assets, seed_mode)

        # Track/alert only on skilled wallets: drop negative trailing-week ROI.
        leaderboard = wt.filter_by_performance(leaderboard_full)
        top50 = [row["ethAddress"] for row in leaderboard[:50]]
        watch_rows = manual_watch_rows(set(top50))
        watch_addresses = [row["ethAddress"] for row in watch_rows]
        tracked = top50 + watch_addresses
        if watch_addresses:
            log.info("Tracking %s manual watch wallets outside top 50.", len(watch_addresses))

        raw_positions = await hl.fetch_all_positions(tracked)
        apply_watch_account_values(watch_rows, raw_positions)
        positions_by_address = {a: wt.parse_positions(s) for a, s in raw_positions.items()}

        alert_leaderboard = leaderboard[:50] + watch_rows
        await wt.check_whale_positions(alert_leaderboard, assets, positions_by_address, seed_mode)
        await wt.check_whale_confluence(leaderboard, assets, seed_mode)
        await wt.check_liquidation_risk(alert_leaderboard, assets, positions_by_address, seed_mode)

        if not seed_mode:
            await digest_svc.maybe_send_weekly_digest()
        log.info("%s cycle complete.", label.capitalize())
    except Exception as e:
        log.error("Wallet cycle error: %s", e, exc_info=True)


# --------------------------- coin cycle + correlation ---------------------------
def _format_confluence(m: dict) -> str:
    side_emoji = "🟢" if m["side"] == "long" else "🔴"
    head = (
        f"⭐⭐⭐ <b>STRONG CONFLUENCE</b> ⭐⭐⭐\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{side_emoji} <b>{m['coin']} {m['side'].upper()}</b>\n"
        f"📊 Technical score: <b>{m['score']}</b>\n"
        f"🐋 Whales aligned: <b>{m['whales']}</b> (${m['total_notional']:,.0f})\n"
        f"🧠 Combined smart score: <b>{m.get('smart', 0.0):+.1f}</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
    )
    return head + format_setup(m["setup"])


async def _coin_cycle(seed_mode: bool = False) -> None:
    global _last_setups, _last_confluence_snapshot
    log.info("Starting %s cycle...", "coin-seed" if seed_mode else "coin-scan")
    try:
        setups = await coin_scan()
        _last_setups = setups
        for s in setups:
            if s.get("score", 0) < config.MIN_SCORE_FOR_ALERT:
                continue
            inner = (s.get("setups") or [{}])[0]
            direction = (inner.get("direction") or "long").lower()
            key = f"coin:{s.get('coin')}:{direction}"
            if seed_mode:
                # Cold start: record the already-qualifying setups so they don't
                # all fire at once on the first scan; new/changed ones alert later.
                db.record_alert("coin", key)
                continue
            await alerts_svc.maybe_send("coin", key, format_setup(s), cooldown_minutes=240)

        # Dedicated correlation pass: the normal shortlist is often builder-dex
        # equities, which never overlap the crypto perps whales trade — so the
        # (coin, side) join can't fire. Score the coins where whales are actually
        # clustered (>= CORRELATION_MIN_WHALES aligned) and feed those in too.
        whale_coins = [
            g["coin"] for g in corr.current_wallet_confluence()
            if g["count"] >= config.CORRELATION_MIN_WHALES
        ]
        corr_setups = await correlation_scan(whale_coins) if whale_coins else []
        by_coin = {s.get("coin"): s for s in setups}
        for s in corr_setups:  # prefer the freshly-scored correlation setup
            by_coin[s.get("coin")] = s
        matches = corr.find_confluence(list(by_coin.values()))
        if matches:
            _last_confluence_snapshot = "\n\n".join(_format_confluence(m) for m in matches)
            for m in matches:
                key = f"corr:{m['coin']}:{m['side']}"
                if seed_mode:
                    db.record_alert("correlation", key)
                    continue
                await alerts_svc.maybe_send(
                    "correlation", key, _format_confluence(m),
                    cooldown_minutes=config.CORRELATION_COOLDOWN_MINUTES,
                    pin=True,
                )
        log.info("%s cycle complete (%s setups, %s confluence).",
                 "Coin-seed" if seed_mode else "Coin-scan", len(setups), len(matches))
    except Exception as e:
        log.error("Coin cycle error: %s", e, exc_info=True)


# --------------------------- wallet discovery ---------------------------
def _discovery_excluded_addresses(leaderboard: list[dict]) -> set[str]:
    """Addresses already in the tracked set (so discovery won't re-suggest them).

    Includes the current account-value top-50 (auto-tracked by the wallet cycle),
    the hand-picked watchlist.json, manual labelled watch wallets, and wallets
    already promoted by discovery.
    """
    excluded = {(row.get("ethAddress") or "").lower() for row in leaderboard[:50]}
    for w in db.get_watch_wallets():
        excluded.add(w["address"].lower())
    for w in load_deploy_watchlist():
        excluded.add(w["ethAddress"].lower())
    excluded |= {a.lower() for a in db.get_tracked_candidate_addresses()}
    excluded.discard("")
    return excluded


async def _retire_stale_candidates(roi_by_addr: dict) -> None:
    """Demote discovered wallets that go negative on week+month for N runs.

    Only touches discovery-tracked candidates — hand-picked watchlist entries
    are never auto-retired.
    """
    for addr in db.get_tracked_candidate_addresses():
        roi = roi_by_addr.get(addr)
        if roi is None:
            continue  # not in this run's scan range — can't assess, don't penalize
        week_roi, month_roi = roi
        if week_roi < 0 and month_roi < 0:
            streak = db.bump_candidate_negative_streak(addr)
            if streak >= config.DISCOVERY_RETIRE_CYCLES:
                db.set_candidate_status(addr, "retired")
                log.info("Discovery RETIRE: %s after %s negative cycles", addr, streak)
                await tg.notify_owner(
                    "📉 <b>DISCOVERY — retired</b>\n"
                    f"<code>{addr}</code>\n"
                    f"Negative week+month ROI for {streak} cycles — removed from tracked set."
                )
        else:
            db.reset_candidate_negative_streak(addr)


def _parse_obs_dt(raw: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def evaluate_proven(observations: list, *, min_cycles: int, min_days: float,
                    max_leverage: float) -> tuple[bool, dict]:
    """Decide if a candidate's observation history proves sustained performance.

    Proven requires: >= min_cycles observations spanning >= min_days, week ROI
    positive in EVERY observation, and leverage <= max_leverage throughout. A
    wallet that spiked once (or dipped negative / over-levered even once) fails.
    Returns (is_proven, stats) where stats feeds the promotion ping.
    """
    obs = list(observations)
    if len(obs) < min_cycles:
        return False, {}
    times = [t for t in (_parse_obs_dt(o["observed_at"]) for o in obs) if t]
    if len(times) < min_cycles:
        return False, {}
    span_days = (max(times) - min(times)).total_seconds() / 86400.0
    if span_days < min_days:
        return False, {}

    weeks = [float(o["week_roi"] or 0) for o in obs]
    months = [float(o["month_roi"] or 0) for o in obs]
    levs = [float(o["leverage"] or 0) for o in obs]
    smarts = [float(o["smart_score"] or 0) for o in obs]
    if not all(w > 0 for w in weeks):
        return False, {}
    if not all(lev <= max_leverage for lev in levs):
        return False, {}

    n = len(obs)
    consistent = sum(1 for w, lev in zip(weeks, levs) if w > 0 and lev <= max_leverage)
    stats = {
        "times_seen": n,
        "span_days": span_days,
        "week_roi_min": min(weeks), "week_roi_max": max(weeks),
        "month_roi_min": min(months), "month_roi_max": max(months),
        "smart_min": min(smarts), "smart_max": max(smarts), "smart_last": smarts[-1],
        "leverage_max": max(levs),
        "consistency_pct": consistent / n * 100.0,
    }
    return True, stats


def _track_button(address: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Track", callback_data=f"track:{address}")]]
    )


async def _maybe_promote_proven(address: str, codename: str) -> bool:
    """Promote a candidate to PROVEN (one ping + Track button) once its history
    earns it. Idempotent: a candidate already 'proven' won't re-ping."""
    cand = db.get_candidate(address)
    if cand and cand["status"] == "proven":
        return False
    ok, stats = evaluate_proven(
        db.get_candidate_observations(address),
        min_cycles=config.DISCOVERY_PROVEN_MIN_CYCLES,
        min_days=config.DISCOVERY_PROVEN_MIN_DAYS,
        max_leverage=config.DISCOVERY_MAX_LEVERAGE,
    )
    if not ok:
        return False
    db.set_candidate_status(address, "proven")
    log.info("Discovery PROVEN: %s seen %sx over %.1fd",
             address, stats["times_seen"], stats["span_days"])
    await tg.notify_owner(
        fw.proven_promotion_alert(address, codename, stats),
        reply_markup=_track_button(address),
    )
    await asyncio.sleep(1)
    return True


def _discovery_page(effective: list[dict]) -> tuple[list[dict], int, int]:
    """The slice of the leaderboard to run position-fetches on THIS cycle.

    The leaderboard GET is cheap at any depth; the cost is the per-wallet fetch,
    so we page it. A persisted cursor advances by DISCOVERY_SCAN_PAGE_SIZE each
    cycle and wraps, sweeping the full depth over ceil(TOP_N / PAGE_SIZE) cycles.
    """
    total = len(effective)
    if total == 0:
        return [], 0, 0
    page_size = max(1, config.DISCOVERY_SCAN_PAGE_SIZE)
    try:
        cursor = int(db.get_state("discovery_scan_cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    if cursor < 0 or cursor >= total:
        cursor = 0
    hi = min(cursor + page_size, total)
    db.set_state("discovery_scan_cursor", str(hi if hi < total else 0))
    return effective[cursor:hi], cursor, hi


async def _discovery_cycle() -> None:
    log.info("Starting discovery cycle (top_n=%s, page=%s)...",
             config.DISCOVERY_SCAN_TOP_N, config.DISCOVERY_SCAN_PAGE_SIZE)
    try:
        leaderboard = await hl.get_leaderboard(top_n=config.DISCOVERY_SCAN_TOP_N)
    except Exception as e:
        log.error("Discovery: leaderboard fetch failed: %s", e)
        return

    effective = leaderboard[:config.DISCOVERY_SCAN_TOP_N]
    excluded = _discovery_excluded_addresses(leaderboard)

    # ROI over the FULL scanned depth is cheap (no fetch) and drives retirement.
    roi_by_addr: dict[str, tuple[float, float]] = {}
    for row in effective:
        addr = (row.get("ethAddress") or "").lower()
        if addr:
            roi_by_addr[addr] = wt.window_roi(row)
    await _retire_stale_candidates(roi_by_addr)

    # Only the expensive position-fetch step is paged across cycles.
    page_rows, lo, hi = _discovery_page(effective)

    # Cheap pre-filter within this page (no fetch): not tracked/algo, real size,
    # positive ROI on BOTH week AND month so a single lucky day can't qualify.
    prelim: list[tuple] = []
    for row in page_rows:
        addr = (row.get("ethAddress") or "").lower()
        if not addr or addr in excluded or db.is_algo(addr):
            continue
        week_roi, month_roi = roi_by_addr.get(addr, wt.window_roi(row))
        try:
            account_value = float(row.get("accountValue", 0) or 0)
        except (TypeError, ValueError):
            account_value = 0.0
        if account_value < config.DISCOVERY_MIN_ACCOUNT_VALUE:
            continue
        if not (week_roi > 0 and month_roi > 0):
            continue
        prelim.append((addr, week_roi, month_roi, account_value))

    if not prelim:
        log.info("Discovery: page [%s:%s] had no wallets past the pre-filter.", lo, hi)
        return

    # Fetch positions only for this page's pre-filtered pool (rate-limiter paced).
    raw_positions = await hl.fetch_all_positions([addr for addr, *_ in prelim])

    suggested_items: list[dict] = []
    auto_items: list[dict] = []
    suggested = auto_added = skipped_mm = skipped_lev = proven = 0
    for addr, week_roi, month_roi, account_value in prelim:
        state = raw_positions.get(addr)
        if state is None:
            continue
        positions = wt.parse_positions(state)
        if not positions:
            continue  # no open book → can't assess leverage / direction
        snap = wt.wallet_performance_snapshot(account_value, positions)
        leverage = snap["book_leverage"]
        if leverage > config.DISCOVERY_MAX_LEVERAGE:
            skipped_lev += 1
            continue
        is_mm, mm_reason = wt.looks_like_market_maker(
            positions, config.DISCOVERY_MM_MIN_COINS, config.DISCOVERY_MM_NET_GROSS_RATIO
        )
        if is_mm:
            skipped_mm += 1
            log.info("Discovery: skipping %s — %s", addr[:10], mm_reason)
            continue
        smart = wt.compute_smart_score(week_roi, month_roi, leverage, added_under_stress=False)
        if smart < config.DISCOVERY_MIN_SMART_SCORE:
            continue

        # Respect an owner's rejection/retirement — don't re-observe or resurface.
        existing = db.get_candidate(addr)
        if existing and existing["status"] in ("rejected", "retired"):
            continue

        reason = (
            f"positive week ({week_roi*100:+.1f}%) & month ({month_roi*100:+.1f}%) ROI, "
            f"{leverage:.1f}x book, directional (not delta-neutral)"
        )
        codename = identity.codename_for(addr)
        item = {
            "address": addr, "codename": codename, "smart_score": smart,
            "week_roi": week_roi, "month_roi": month_roi,
            "leverage": leverage, "account_value": account_value,
        }
        # Accumulate track record for the proven-candidate layer (no ping here).
        db.record_candidate_observation(addr, smart, week_roi, month_roi, leverage, account_value)

        auto = (
            config.DISCOVERY_AUTO_ADD
            and smart >= config.DISCOVERY_AUTO_ADD_MIN_SMART
            and auto_added < config.DISCOVERY_AUTO_ADD_MAX_PER_RUN
        )
        if auto:
            db.upsert_suggested_candidate(addr, smart, week_roi, month_roi, leverage, account_value, reason)
            db.set_candidate_status(addr, "tracked")
            auto_added += 1
            auto_items.append(item)
            log.info("Discovery AUTO-ADD: %s smart=%.1f", addr, smart)
            continue

        is_new = db.upsert_suggested_candidate(
            addr, smart, week_roi, month_roi, leverage, account_value, reason)

        # The real signal: promote once the track record earns it.
        promoted = False
        if config.DISCOVERY_PROVEN_ENABLED:
            promoted = await _maybe_promote_proven(addr, codename)
            if promoted:
                proven += 1
        if is_new and not promoted:
            suggested += 1
            suggested_items.append(item)
            log.info("Discovery SUGGEST: %s smart=%.1f", addr, smart)

    # Raw suggestions → ONE lower-priority digest (optional), ranked by smart.
    if config.DISCOVERY_RAW_DIGEST_ENABLED and (suggested_items or auto_items):
        suggested_items.sort(key=lambda it: it["smart_score"], reverse=True)
        await tg.notify_owner(fw.discovery_digest(
            suggested_items[:config.DISCOVERY_DIGEST_MAX], auto_items))

    log.info(
        "Discovery cycle complete [rows %s:%s]: %s suggested, %s proven, %s auto-added, "
        "%s MM-skipped, %s over-leverage.",
        lo, hi, suggested, proven, auto_added, skipped_mm, skipped_lev,
    )


# --------------------------- JobQueue callbacks ---------------------------
async def wallet_seed_job(context) -> None:
    # Seeds the wallet baseline once, globally. Intentionally does NOT broadcast a
    # "baseline set" message: it ran on one chat's activation and is irrelevant
    # (and confusing) to every other subscriber (audit M1).
    await _wallet_cycle(seed_mode=True)
    db.set_state("wallet_seeded", "1")
    log.info("Wallet baseline seeded; change alerts active.")


async def wallet_job(context) -> None:
    global _wallet_cold_start_pending
    if not _should_run():
        return
    if db.get_state("wallet_seeded") != "1":
        return  # wait until the one-off seed has run
    if _wallet_cold_start_pending:
        # First wallet cycle this process: refresh baselines silently so a restart
        # doesn't re-diff against stale state and dump a flood of alerts.
        _wallet_cold_start_pending = False
        log.info("Cold start: seeding wallet baselines silently (no alerts this cycle).")
        await _wallet_cycle(seed_mode=True)
        return
    await _wallet_cycle(seed_mode=False)


async def coin_job(context) -> None:
    global _coin_cold_start_pending
    if not _should_run():
        return
    if _coin_cold_start_pending:
        _coin_cold_start_pending = False
        log.info("Cold start: seeding coin-scan baseline silently (no alerts this cycle).")
        await _coin_cycle(seed_mode=True)
        return
    await _coin_cycle()


async def discovery_job(context) -> None:
    if not config.DISCOVERY_ENABLED:
        return
    if not _should_run():
        return
    await _discovery_cycle()


async def prune_job(context) -> None:
    try:
        db.prune_old_data()
        log.info("Pruned data older than %s days.", config.RETENTION_DAYS)
    except Exception as e:
        log.warning("Prune failed: %s", e)

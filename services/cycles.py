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
from pathlib import Path

import config
from integrations import hyperliquid as hl
from storage import database as db
from trackers import wallet_tracker as wt
from scanner.setups import coin_scan
from services import correlation as corr
from services import alerts as alerts_svc
from services import digest as digest_svc
from bot import telegram as tg
from bot import formatting_wallet as fw
from bot.formatting import format_setup

log = logging.getLogger(__name__)

_last_setups: list[dict] = []
_last_confluence_snapshot: str | None = None


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


async def _coin_cycle() -> None:
    global _last_setups, _last_confluence_snapshot
    log.info("Starting coin-scan cycle...")
    try:
        setups = await coin_scan()
        _last_setups = setups
        for s in setups:
            if s.get("score", 0) < config.MIN_SCORE_FOR_ALERT:
                continue
            inner = (s.get("setups") or [{}])[0]
            direction = (inner.get("direction") or "long").lower()
            key = f"coin:{s.get('coin')}:{direction}"
            await alerts_svc.maybe_send("coin", key, format_setup(s), cooldown_minutes=240)

        matches = corr.find_confluence(setups)
        if matches:
            _last_confluence_snapshot = "\n\n".join(_format_confluence(m) for m in matches)
            for m in matches:
                key = f"corr:{m['coin']}:{m['side']}"
                await alerts_svc.maybe_send(
                    "correlation", key, _format_confluence(m),
                    cooldown_minutes=config.CORRELATION_COOLDOWN_MINUTES,
                    pin=True,
                )
        log.info("Coin-scan cycle complete (%s setups, %s confluence).", len(setups), len(matches))
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


async def _discovery_cycle() -> None:
    log.info("Starting discovery cycle (top_n=%s)...", config.DISCOVERY_SCAN_TOP_N)
    try:
        leaderboard = await hl.get_leaderboard(top_n=config.DISCOVERY_SCAN_TOP_N)
    except Exception as e:
        log.error("Discovery: leaderboard fetch failed: %s", e)
        return

    excluded = _discovery_excluded_addresses(leaderboard)

    # Cheap pre-filter (no position fetch): not tracked, not algo, real size, and
    # a multi-window track record — positive ROI on BOTH week AND month so a
    # single lucky day can't qualify a wallet.
    prelim: list[tuple] = []
    roi_by_addr: dict[str, tuple[float, float]] = {}
    for row in leaderboard:
        addr = (row.get("ethAddress") or "").lower()
        if not addr:
            continue
        week_roi, month_roi = wt.window_roi(row)
        roi_by_addr[addr] = (week_roi, month_roi)
        if addr in excluded or db.is_algo(addr):
            continue
        try:
            account_value = float(row.get("accountValue", 0) or 0)
        except (TypeError, ValueError):
            account_value = 0.0
        if account_value < config.DISCOVERY_MIN_ACCOUNT_VALUE:
            continue
        if not (week_roi > 0 and month_roi > 0):
            continue
        prelim.append((addr, week_roi, month_roi, account_value))

    # Retirement runs off the full scanned set's ROI, before promotions.
    await _retire_stale_candidates(roi_by_addr)

    if not prelim:
        log.info("Discovery: no wallets passed the multi-window pre-filter.")
        return

    # Fetch positions only for the small pre-filtered pool (leverage + MM checks).
    raw_positions = await hl.fetch_all_positions([addr for addr, *_ in prelim])

    suggested = auto_added = skipped_mm = skipped_lev = 0
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

        reason = (
            f"positive week ({week_roi*100:+.1f}%) & month ({month_roi*100:+.1f}%) ROI, "
            f"{leverage:.1f}x book, directional (not delta-neutral)"
        )
        auto = (
            config.DISCOVERY_AUTO_ADD
            and smart >= config.DISCOVERY_AUTO_ADD_MIN_SMART
            and auto_added < config.DISCOVERY_AUTO_ADD_MAX_PER_RUN
        )
        if auto:
            db.upsert_suggested_candidate(addr, smart, week_roi, month_roi, leverage, account_value, reason)
            db.set_candidate_status(addr, "tracked")
            auto_added += 1
            log.info("Discovery AUTO-ADD: %s smart=%.1f", addr, smart)
            await tg.notify_owner(fw.discovery_suggestion_alert(
                address=addr, smart_score=smart, week_roi=week_roi, month_roi=month_roi,
                leverage=leverage, account_value=account_value, reason=reason, auto_added=True))
            await asyncio.sleep(1)
        else:
            is_new = db.upsert_suggested_candidate(
                addr, smart, week_roi, month_roi, leverage, account_value, reason)
            if is_new:
                suggested += 1
                log.info("Discovery SUGGEST: %s smart=%.1f", addr, smart)
                await tg.notify_owner(fw.discovery_suggestion_alert(
                    address=addr, smart_score=smart, week_roi=week_roi, month_roi=month_roi,
                    leverage=leverage, account_value=account_value, reason=reason, auto_added=False))
                await asyncio.sleep(1)

    log.info(
        "Discovery cycle complete: %s suggested, %s auto-added, %s MM-skipped, %s over-leverage.",
        suggested, auto_added, skipped_mm, skipped_lev,
    )


# --------------------------- JobQueue callbacks ---------------------------
async def wallet_seed_job(context) -> None:
    await _wallet_cycle(seed_mode=True)
    db.set_state("wallet_seeded", "1")
    await tg.broadcast(text="✅ Wallet baseline set — change alerts are now active.")


async def wallet_job(context) -> None:
    if not _should_run():
        return
    if db.get_state("wallet_seeded") != "1":
        return  # wait until the one-off seed has run
    await _wallet_cycle(seed_mode=False)


async def coin_job(context) -> None:
    if not _should_run():
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

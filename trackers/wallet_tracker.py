import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from telegram.error import RetryAfter

from storage.database import (
    get_previous_positions,
    get_latest_position_snapshot_at,
    get_recent_positions_for_addresses,
    get_latest_wallet_performance,
    save_positions,
    save_wallet_performance_snapshot,
    alert_already_sent,
    get_recent_alerts_by_prefix,
    record_alert,
    get_previous_funding,
    get_funding_ago,
    is_algo,
    is_vip,
    get_wallet_label,
)
from bot.formatting_wallet import (
    whale_alert,
    vip_whale_alert,
    whale_size_increase_alert,
    whale_stress_watch_alert,
    whale_reactivation_alert,
    wallet_performance_alert,
    confluence_alert,
    confluence_teaser,
    liquidation_risk_alert,
    funding_spike_alert,
    oi_surge_alert,
)
from bot.charts import generate_whale_chart, generate_confluence_chart
from bot.telegram import send_alert, send_photo_alert
import config

log = logging.getLogger(__name__)

MAX_WHALE_ALERTS_PER_CYCLE = 5
SIZE_INCREASE_THRESHOLD_PCT = 20.0  # alert when notional grows by this % or more
WHALE_ADD_SEMANTIC_COOLDOWN_MINUTES = 90
WHALE_ADD_ESCALATION_PCT_FROM_LAST_ALERT = 35.0
WHALE_ADD_ESCALATION_MIN_USD = 250_000
CONFLUENCE_SEMANTIC_COOLDOWN_MINUTES = 240
LIQUIDATION_DANGER_COOLDOWN_MINUTES = 120
LIQUIDATION_WARNING_COOLDOWN_MINUTES = 180
WHALE_STRESS_MIN_UNREALIZED_LOSS_USD = 500_000
WHALE_STRESS_MIN_LOSS_PCT_OF_POSITION = 5.0
WHALE_STRESS_LIQ_DISTANCE_WATCH_PCT = 30.0
WHALE_REACTIVATION_LOOKBACK_HOURS = 12
WHALE_REACTIVATION_COOLDOWN_MINUTES = 1440
REACTIVATION_NOTE_MARKERS = ("flat", "watch", "sidelined", "empty", "inactive")
WALLET_PERFORMANCE_COOLDOWN_MINUTES = 360


def parse_alert_notional(alert_key: str) -> float | None:
    try:
        return float(alert_key.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def is_material_whale_add_since_last_alert(current_notional: float, recent_alert_keys: list[str]) -> bool:
    for key in recent_alert_keys:
        last_notional = parse_alert_notional(key)
        if last_notional is None:
            continue

        min_escalation = max(
            last_notional * (WHALE_ADD_ESCALATION_PCT_FROM_LAST_ALERT / 100),
            WHALE_ADD_ESCALATION_MIN_USD,
        )
        return current_notional >= last_notional + min_escalation

    return False


def should_use_stress_watch(pos: dict, current_px: float) -> bool:
    """Promote whale-add alerts into a Pro narrative when size is added under stress."""
    if pos["unrealized_pnl"] >= 0:
        return False
    if pos["notional_usd"] <= 0:
        return False

    loss = abs(pos["unrealized_pnl"])
    loss_pct = (loss / pos["notional_usd"]) * 100
    if loss >= WHALE_STRESS_MIN_UNREALIZED_LOSS_USD:
        return True
    if loss_pct >= WHALE_STRESS_MIN_LOSS_PCT_OF_POSITION:
        return True

    liq_px = pos.get("liq_px", 0) or 0
    if liq_px > 0 and current_px > 0:
        liq_dist_pct = abs(((liq_px - current_px) / current_px) * 100)
        return liq_dist_pct <= WHALE_STRESS_LIQ_DISTANCE_WATCH_PCT

    return False


def parse_snapshot_at(snapshot_at: str | None) -> datetime | None:
    if not snapshot_at:
        return None
    try:
        return datetime.fromisoformat(snapshot_at)
    except ValueError:
        return None


def hours_since_snapshot(snapshot_at: str | None) -> float | None:
    parsed = parse_snapshot_at(snapshot_at)
    if parsed is None:
        return None
    return max((datetime.utcnow() - parsed).total_seconds() / 3600, 0)


def format_inactive_for(inactive_hours: float | None) -> str:
    if inactive_hours is None:
        return "No recent recorded open positions"
    if inactive_hours < 24:
        return f"{inactive_hours:.1f}h ago"
    return f"{inactive_hours / 24:.1f}d ago"


def is_reactivation_candidate(wallet_info) -> bool:
    if not wallet_info:
        return False
    if wallet_info["label"] == "vip":
        return True

    profile_text = " ".join(
        str(wallet_info[field] or "")
        for field in ("label", "name", "notes")
    ).lower()
    return any(marker in profile_text for marker in REACTIVATION_NOTE_MARKERS)


def wallet_profile_text(wallet_info) -> str:
    if not wallet_info:
        return "watch wallet"

    name = wallet_info["name"] or wallet_info["label"] or "watch wallet"
    notes = wallet_info["notes"] or ""
    if notes:
        return f"{name} — {notes}"[:180]
    return str(name)[:180]


def allowed_watch_tokens(row: dict) -> set[str]:
    return {
        str(token).upper()
        for token in row.get("watch_tokens", [])
        if str(token).strip()
    }


def watch_min_notional_change(row: dict) -> float:
    try:
        return float(row.get("min_notional_change_usd", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def wallet_performance_snapshot(account_value: float, positions: list[dict]) -> dict:
    exposure_total = sum(float(pos["notional_usd"]) for pos in positions)
    open_upnl = sum(float(pos["unrealized_pnl"]) for pos in positions)
    negative_upnl = sum(min(0.0, float(pos["unrealized_pnl"])) for pos in positions)
    return {
        "account_value": account_value,
        "exposure_total": exposure_total,
        "open_upnl": open_upnl,
        "negative_upnl": negative_upnl,
        "open_positions": len(positions),
        "book_leverage": exposure_total / account_value if account_value else 0.0,
    }


def wallet_health_score(current: dict, state: str) -> float:
    """0-100 current-health score. State sets the backbone; magnitude nudges it.

    NOTE: this is a CURRENT-STATE gauge (open uPnL + leverage), not a track
    record. A wallet deep underwater scores low even if it's historically good.
    """
    base = {
        "hot_streak": 85, "heating_up": 68, "stable": 50,
        "cooling_off": 38, "implosion_watch": 22, "self_imploding": 8,
        "flat": 50,
    }.get(state, 50)
    av = current.get("account_value") or 1
    upnl_pct = (current.get("open_upnl", 0) / av) * 100
    lev = current.get("book_leverage", 0)
    score = base + max(-10, min(10, upnl_pct * 0.4)) - max(0, (lev - 5) * 1.5)
    return round(max(0.0, min(100.0, score)), 1)


def classify_wallet_performance(current: dict, previous) -> tuple[str, str]:
    account_value = current["account_value"]
    open_upnl = current["open_upnl"]
    negative_upnl = current["negative_upnl"]
    upnl_pct = (open_upnl / account_value) * 100 if account_value else 0.0
    negative_pct = (abs(negative_upnl) / account_value) * 100 if account_value else 0.0
    equity_delta_pct = 0.0
    upnl_delta = 0.0
    if previous:
        prev_equity = float(previous["account_value"] or 0)
        equity_delta_pct = ((account_value - prev_equity) / prev_equity) * 100 if prev_equity else 0.0
        upnl_delta = open_upnl - float(previous["open_upnl"] or 0)

    hot_move_usd = max(25_000, account_value * 0.05)
    danger_move_usd = max(50_000, account_value * 0.1)

    if current["open_positions"] == 0:
        return "flat", "Wallet is flat; no open PnL health signal."
    if upnl_pct <= -20 or negative_pct >= 25 or equity_delta_pct <= -15 or upnl_delta <= -danger_move_usd:
        return "self_imploding", "Open losses, equity damage, or uPnL deterioration crossed the self-implosion threshold."
    if upnl_pct <= -10 or negative_pct >= 10 or equity_delta_pct <= -7:
        return "implosion_watch", "Open losses or equity deterioration are large enough to watch closely."
    if upnl_pct >= 20 or open_upnl >= 250_000 or upnl_delta >= hot_move_usd:
        return "hot_streak", "Wallet is carrying outsized open profit or rapidly improving uPnL."
    if upnl_delta <= -hot_move_usd or equity_delta_pct <= -5:
        return "cooling_off", "Wallet is giving back meaningful open profit or equity."
    if upnl_pct >= 5 or upnl_delta >= 10_000:
        return "heating_up", "Wallet has positive open profit, but not enough for hot-streak status."
    return "stable", "No major PnL health change."


async def check_wallet_performance_health(
    row: dict,
    address: str,
    rank,
    positions: list[dict],
    account_value: float,
    seed_mode: bool,
) -> None:
    current = wallet_performance_snapshot(account_value, positions)
    previous = get_latest_wallet_performance(address)
    state, reason = classify_wallet_performance(current, previous)
    score = wallet_health_score(current, state)
    save_wallet_performance_snapshot(address=address, state=state, health_score=score, **current)

    if seed_mode or previous is None:
        return
    if state not in {"hot_streak", "cooling_off", "implosion_watch", "self_imploding"}:
        return

    alert_key = f"wallet_perf:{state}:{address}"
    legacy_prefix = f"{alert_key}:"
    if alert_already_sent("wallet_performance", alert_key, cooldown_minutes=WALLET_PERFORMANCE_COOLDOWN_MINUTES):
        return
    if get_recent_alerts_by_prefix("wallet_performance", legacy_prefix, cooldown_minutes=WALLET_PERFORMANCE_COOLDOWN_MINUTES):
        return

    msg = wallet_performance_alert(
        rank=rank,
        address=address,
        state=state,
        account_value=current["account_value"],
        exposure_total=current["exposure_total"],
        open_upnl=current["open_upnl"],
        negative_upnl=current["negative_upnl"],
        book_leverage=current["book_leverage"],
        reason=reason,
    )
    sent = await safe_send(msg, paid_only=True)
    if sent:
        record_alert("wallet_performance", alert_key)
        log.info("Wallet performance [%s]: %s %s", state, address[:10], reason)
        await asyncio.sleep(3)


def parse_positions(state: dict) -> list[dict]:
    positions = []
    for item in state.get("assetPositions", []):
        pos = item.get("position", {})
        if not pos or float(pos.get("szi", 0)) == 0:
            continue
        size = float(pos["szi"])
        entry_px = float(pos.get("entryPx", 0) or 0)
        notional = abs(size) * entry_px
        positions.append({
            "coin": pos["coin"],
            "side": "long" if size > 0 else "short",
            "size": abs(size),
            "notional_usd": notional,
            "entry_px": entry_px,
            "liq_px": float(pos.get("liquidationPx", 0) or 0),
            "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
        })
    return positions


async def safe_send(msg: str, paid_only: bool = False) -> bool:
    try:
        await send_alert(msg, paid_only=paid_only)
        return True
    except RetryAfter as e:
        log.warning(f"Telegram rate limit — sleeping {e.retry_after}s")
        await asyncio.sleep(e.retry_after)
        return False
    except Exception as e:
        log.error(f"Failed to send alert: {e}")
        return False


async def safe_send_photo(image_bytes: bytes, caption: str, paid_only: bool = False) -> bool:
    try:
        await send_photo_alert(image_bytes, caption, paid_only=paid_only)
        return True
    except RetryAfter as e:
        log.warning(f"Telegram rate limit — sleeping {e.retry_after}s")
        await asyncio.sleep(e.retry_after)
        return False
    except Exception as e:
        log.error(f"Failed to send photo: {e}")
        return False


async def check_whale_positions(
    leaderboard: list[dict],
    assets: list[dict],
    positions_by_address: dict,
    seed_mode: bool,
) -> None:
    mark_prices = {a["name"]: a["mark_px"] for a in assets}
    alerts_sent_this_cycle = 0

    for fallback_rank, row in enumerate(leaderboard, start=1):
        address = row["ethAddress"]
        rank = row.get("rank", fallback_rank)

        # Skip confirmed algo wallets — they generate noise not signal
        if is_algo(address):
            log.debug(f"Skipping algo wallet #{rank} {address[:10]}")
            continue

        current_positions = positions_by_address.get(address)
        if current_positions is None:
            continue

        perfs = dict(row["windowPerformances"])
        account_value = float(row["accountValue"])
        day_pnl = float(perfs.get("day", {}).get("pnl", 0))

        wallet_info = get_wallet_label(address)
        latest_position_snapshot_at = get_latest_position_snapshot_at(address)
        inactive_hours = hours_since_snapshot(latest_position_snapshot_at)
        whale_reactivated = (
            is_reactivation_candidate(wallet_info)
            and (
                inactive_hours is None
                or inactive_hours >= WHALE_REACTIVATION_LOOKBACK_HOURS
            )
        )
        reactivation_alert_sent = False

        prev_rows = get_previous_positions(address)
        prev_by_key = {f"{r['coin']}:{r['side']}": r for r in prev_rows}

        save_positions(address, current_positions)
        await check_wallet_performance_health(
            row=row,
            address=address,
            rank=rank,
            positions=current_positions,
            account_value=account_value,
            seed_mode=seed_mode,
        )

        if seed_mode:
            continue

        for pos in current_positions:
            if alerts_sent_this_cycle >= MAX_WHALE_ALERTS_PER_CYCLE:
                break
            allowed_tokens = allowed_watch_tokens(row)
            if allowed_tokens and pos["coin"].upper() not in allowed_tokens:
                continue
            watch_floor = watch_min_notional_change(row)
            threshold = watch_floor if watch_floor > 0 else config.WHALE_POSITION_THRESHOLD_USD
            if pos["notional_usd"] < threshold:
                continue

            position_key = f"{pos['coin']}:{pos['side']}"
            current_px = mark_prices.get(pos["coin"], 0.0)

            if whale_reactivated and reactivation_alert_sent:
                continue

            if whale_reactivated and not reactivation_alert_sent:
                alert_key = f"whale:reactivation:{address}:{pos['coin']}:{pos['side']}"
                if alert_already_sent(
                    "whale_reactivation",
                    alert_key,
                    cooldown_minutes=WHALE_REACTIVATION_COOLDOWN_MINUTES,
                ):
                    reactivation_alert_sent = True
                    continue

                else:
                    caption = whale_reactivation_alert(
                        rank=rank, address=address, coin=pos["coin"],
                        side=pos["side"], notional_usd=pos["notional_usd"],
                        account_value=account_value, day_pnl=day_pnl,
                        profile=wallet_profile_text(wallet_info),
                        inactive_for=format_inactive_for(inactive_hours),
                    )
                    chart = await generate_whale_chart(
                        coin=pos["coin"], side=pos["side"], rank=rank,
                        notional=pos["notional_usd"], entry_px=pos["entry_px"],
                        account_value=account_value, day_pnl=day_pnl,
                        address=address, current_px=current_px,
                    )
                    sent = await safe_send_photo(chart, caption, paid_only=True) if chart else await safe_send(caption, paid_only=True)
                    if sent:
                        record_alert("whale_reactivation", alert_key)
                        alerts_sent_this_cycle += 1
                        reactivation_alert_sent = True
                        log.info(
                            "Whale reactivation [PRO]: #%s %s %s $%s after %s",
                            rank,
                            pos["coin"],
                            pos["side"],
                            f"{pos['notional_usd']:,.0f}",
                            format_inactive_for(inactive_hours),
                        )
                        await asyncio.sleep(3)
                        continue
                    continue

            if position_key not in prev_by_key:
                # New position open
                alert_key = f"whale:new:{address}:{pos['coin']}:{pos['side']}"
                if alert_already_sent("whale", alert_key, cooldown_minutes=240):
                    continue

                wallet_is_vip = wallet_info and wallet_info["label"] == "vip"

                if wallet_is_vip:
                    strategy = wallet_info["notes"] or "Top-tier wallet"
                    rank_str = wallet_info["name"] or f"#{rank}"
                    caption = vip_whale_alert(
                        rank=rank_str, address=address, coin=pos["coin"],
                        side=pos["side"], notional_usd=pos["notional_usd"],
                        account_value=account_value, day_pnl=day_pnl,
                        strategy=strategy,
                    )
                else:
                    caption = whale_alert(
                        rank=rank, address=address, coin=pos["coin"],
                        side=pos["side"], notional_usd=pos["notional_usd"],
                        account_value=account_value, day_pnl=day_pnl,
                    )

                chart = await generate_whale_chart(
                    coin=pos["coin"], side=pos["side"], rank=rank,
                    notional=pos["notional_usd"], entry_px=pos["entry_px"],
                    account_value=account_value, day_pnl=day_pnl,
                    address=address, current_px=current_px,
                )
                sent = await safe_send_photo(chart, caption, paid_only=True) if chart else await safe_send(caption, paid_only=True)
                if sent:
                    record_alert("whale", alert_key)
                    alerts_sent_this_cycle += 1
                    tier = "VIP" if wallet_is_vip else "PRO"
                    log.info(f"Whale new [{tier}]: #{rank} {pos['coin']} {pos['side']} ${pos['notional_usd']:,.0f}")
                    await asyncio.sleep(3)

            else:
                # Existing position — check for size increase
                prev_notional = prev_by_key[position_key]["notional_usd"]
                if prev_notional <= 0:
                    continue
                pct_increase = ((pos["notional_usd"] - prev_notional) / prev_notional) * 100
                if pct_increase < SIZE_INCREASE_THRESHOLD_PCT:
                    continue
                if pos["notional_usd"] - prev_notional < watch_min_notional_change(row):
                    continue

                alert_key = f"whale:add:{address}:{pos['coin']}:{pos['side']}:{round(pos['notional_usd'], -4)}"
                if alert_already_sent("whale_add", alert_key, cooldown_minutes=120):
                    continue

                semantic_prefix = f"whale:add:{address}:{pos['coin']}:{pos['side']}:"
                recent_adds = get_recent_alerts_by_prefix(
                    "whale_add",
                    semantic_prefix,
                    cooldown_minutes=WHALE_ADD_SEMANTIC_COOLDOWN_MINUTES,
                )
                recent_keys = [row["key"] for row in recent_adds]
                if recent_keys and not is_material_whale_add_since_last_alert(pos["notional_usd"], recent_keys):
                    log.debug(
                        "Skipping repeated whale add: #%s %s %s $%s",
                        rank,
                        pos["coin"],
                        pos["side"],
                        f"{pos['notional_usd']:,.0f}",
                    )
                    continue

                if should_use_stress_watch(pos, current_px):
                    caption = whale_stress_watch_alert(
                        rank=rank, address=address, coin=pos["coin"],
                        side=pos["side"], notional_usd=pos["notional_usd"],
                        prev_notional=prev_notional, pct_increase=pct_increase,
                        account_value=account_value, day_pnl=day_pnl,
                        unrealized_pnl=pos["unrealized_pnl"],
                        entry_px=pos["entry_px"], curr_px=current_px,
                        liq_px=pos.get("liq_px", 0),
                    )
                    alert_log_type = "Whale stress watch"
                else:
                    caption = whale_size_increase_alert(
                        rank=rank, address=address, coin=pos["coin"],
                        side=pos["side"], notional_usd=pos["notional_usd"],
                        prev_notional=prev_notional, pct_increase=pct_increase,
                        account_value=account_value, day_pnl=day_pnl,
                    )
                    alert_log_type = "Whale add"
                chart = await generate_whale_chart(
                    coin=pos["coin"], side=pos["side"], rank=rank,
                    notional=pos["notional_usd"], entry_px=pos["entry_px"],
                    account_value=account_value, day_pnl=day_pnl,
                    address=address, current_px=current_px,
                )
                # Pro only — size increase is a premium signal
                sent = await safe_send_photo(chart, caption, paid_only=True) if chart else await safe_send(caption, paid_only=True)
                if sent:
                    record_alert("whale_add", alert_key)
                    alerts_sent_this_cycle += 1
                    log.info(f"{alert_log_type} [PRO]: #{rank} {pos['coin']} {pos['side']} +{pct_increase:.0f}%")
                    await asyncio.sleep(3)


async def check_whale_confluence(leaderboard: list[dict], assets: list[dict], seed_mode: bool) -> None:
    if seed_mode:
        return

    mark_prices = {a["name"]: a["mark_px"] for a in assets}
    # Exclude algos from confluence — only human wallets count
    top50 = {
        row["ethAddress"]: rank
        for rank, row in enumerate(leaderboard[:50], start=1)
        if not is_algo(row["ethAddress"])
    }
    rows = get_recent_positions_for_addresses(list(top50.keys()))

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = f"{row['coin']}:{row['side']}"
        groups[key].append({
            "address": row["address"],
            "rank": top50[row["address"]],
            "notional": row["notional_usd"],
            "entry_px": row["entry_px"],
        })

    for key, whales in groups.items():
        if len(whales) < 2:
            continue

        coin, side = key.split(":", 1)
        total_notional = sum(w["notional"] for w in whales)
        whale_count = len(whales)
        alert_key = f"confluence:{coin}:{side}:{whale_count}"

        semantic_prefix = f"confluence:{coin}:{side}:"
        if get_recent_alerts_by_prefix(
            "confluence",
            semantic_prefix,
            cooldown_minutes=CONFLUENCE_SEMANTIC_COOLDOWN_MINUTES,
        ):
            continue

        if alert_already_sent("confluence", alert_key, cooldown_minutes=120):
            continue

        # Full pro alert — chart + wallet details
        caption = confluence_alert(
            coin=coin, side=side, whale_count=whale_count,
            total_notional=total_notional, whales=whales,
            premium=whale_count >= 3,
        )
        chart = await generate_confluence_chart(
            coin=coin, side=side, whales=whales,
            current_px=mark_prices.get(coin, 0.0),
        )
        sent = await safe_send_photo(chart, caption, paid_only=True) if chart else await safe_send(caption, paid_only=True)

        if sent:
            record_alert("confluence", alert_key)
            log.info(f"Confluence [PRO{' +teaser' if whale_count == 2 else ''}]: {whale_count} whales {coin} {side} ${total_notional:,.0f}")
            await asyncio.sleep(3)


async def check_liquidation_risk(
    leaderboard: list[dict],
    assets: list[dict],
    positions_by_address: dict,
    seed_mode: bool,
) -> None:
    """Alert when a large human wallet position is within 10% of liquidation. Pro only."""
    if seed_mode:
        return

    mark_prices = {a["name"]: a["mark_px"] for a in assets}

    for fallback_rank, row in enumerate(leaderboard, start=1):
        rank = row.get("rank", fallback_rank)
        address = row["ethAddress"]
        if is_algo(address):
            continue

        positions = positions_by_address.get(address)
        if positions is None:
            continue

        for pos in positions:
            if pos["notional_usd"] < config.MIN_NOTIONAL_FOR_LIQ_ALERT:
                continue
            if pos["liq_px"] <= 0:
                continue

            curr_px = mark_prices.get(pos["coin"], 0)
            if curr_px <= 0:
                continue

            dist_pct = ((pos["liq_px"] - curr_px) / curr_px) * 100
            abs_dist = abs(dist_pct)

            # Only care if within threshold
            if abs_dist > config.LIQ_PROXIMITY_THRESHOLD_PCT:
                continue

            # Determine severity
            danger = abs_dist <= config.LIQ_PROXIMITY_DANGER_PCT
            cooldown = (
                LIQUIDATION_DANGER_COOLDOWN_MINUTES
                if danger
                else LIQUIDATION_WARNING_COOLDOWN_MINUTES
            )
            alert_key = f"liq:{'danger' if danger else 'warn'}:{address}:{pos['coin']}"

            if alert_already_sent("liquidation", alert_key, cooldown_minutes=cooldown):
                continue

            msg = liquidation_risk_alert(
                rank=rank, address=address, coin=pos["coin"],
                side=pos["side"], notional_usd=pos["notional_usd"],
                liq_px=pos["liq_px"], curr_px=curr_px,
                dist_pct=dist_pct, danger=danger,
            )
            sent = await safe_send(msg, paid_only=True)
            if sent:
                record_alert("liquidation", alert_key)
                level = "DANGER" if danger else "WARNING"
                log.info(f"Liq {level}: #{rank} {pos['coin']} {pos['side']} {abs_dist:.1f}% from liq")
                await asyncio.sleep(3)


async def check_funding_spikes(assets: list[dict], seed_mode: bool) -> None:
    if seed_mode:
        return
    for asset in assets:
        curr_rate = asset["funding"]

        # Must exceed the absolute threshold
        if abs(curr_rate) < config.FUNDING_RATE_SPIKE_THRESHOLD:
            continue

        # Must have moved meaningfully from previous scan (filters noise)
        prev = get_previous_funding(asset["name"])
        prev_rate = prev["funding_rate"] if prev else 0.0
        if abs(curr_rate - prev_rate) < config.FUNDING_RATE_SPIKE_THRESHOLD * 0.3:
            continue

        # Round to 5dp so small jitter doesn't create duplicate alerts
        alert_key = f"funding:{asset['name']}:{round(curr_rate, 5)}"
        if alert_already_sent("funding", alert_key, cooldown_minutes=120):
            continue

        msg = funding_spike_alert(
            asset=asset["name"], funding_rate=curr_rate,
            open_interest=asset["open_interest"],
            mark_px=asset["mark_px"], prev_rate=prev_rate,
        )
        sent = await safe_send(msg)
        if sent:
            record_alert("funding", alert_key)
            log.info(f"Funding spike: {asset['name']} {curr_rate*100:+.4f}%/8h")
            await asyncio.sleep(3)


async def check_oi_surges(assets: list[dict], seed_mode: bool) -> None:
    if seed_mode:
        return
    for asset in assets:
        curr_oi = asset["open_interest"]

        # Skip tiny markets
        if curr_oi < config.MIN_OI_FOR_SURGE:
            continue

        # Compare against 1-hour-ago snapshot — not last scan
        prev = get_funding_ago(asset["name"], minutes=60)
        if prev is None:
            continue

        prev_oi = prev["open_interest"]
        prev_px = prev["mark_px"]
        if prev_oi == 0:
            continue

        pct_change = ((curr_oi - prev_oi) / prev_oi) * 100
        if abs(pct_change) < config.OI_SURGE_PCT_THRESHOLD:
            continue

        alert_key = f"oi_surge:{asset['name']}"
        if alert_already_sent("oi_surge", alert_key, cooldown_minutes=240):
            continue

        msg = oi_surge_alert(
            asset=asset["name"], open_interest=curr_oi,
            prev_oi=prev_oi, pct_change=pct_change,
            mark_px=asset["mark_px"], prev_px=prev_px,
        )
        sent = await safe_send(msg)
        if sent:
            record_alert("oi_surge", alert_key)
            log.info(f"OI signal: {asset['name']} {pct_change:+.1f}% | px {'up' if asset['mark_px'] > prev_px else 'down'}")
            await asyncio.sleep(3)

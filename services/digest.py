import logging
from datetime import datetime, timedelta
from storage.database import get_conn
from bot.telegram import send_alert
from trackers.wallet_tracker import safe_send

log = logging.getLogger(__name__)


def _get_week_stats() -> dict:
    since = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with get_conn() as conn:
        # Top coins by whale activity (distinct addresses)
        top_coins = conn.execute(
            """
            SELECT coin, side, COUNT(DISTINCT address) as whale_count,
                   MAX(notional_usd) as peak_notional
            FROM position_snapshots
            WHERE snapshot_at > ?
            GROUP BY coin, side
            ORDER BY whale_count DESC, peak_notional DESC
            LIMIT 5
            """,
            (since,),
        ).fetchall()

        # Confluence events this week
        confluence_count = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM alerts_sent
            WHERE alert_type = 'confluence' AND sent_at > ?
            """,
            (since,),
        ).fetchone()["cnt"]

        # Total whale alerts fired
        whale_count = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM alerts_sent
            WHERE alert_type = 'whale' AND sent_at > ?
            """,
            (since,),
        ).fetchone()["cnt"]

        # Most active whale addresses with latest week PnL from leaderboard
        top_whales = conn.execute(
            """
            SELECT
                ps.address,
                COUNT(DISTINCT ps.coin) as coins_traded,
                MAX(ps.notional_usd) as peak_position,
                lb.week_pnl,
                lb.rank
            FROM position_snapshots ps
            LEFT JOIN (
                SELECT address, week_pnl, rank
                FROM leaderboard_snapshots
                WHERE (address, snapshot_at) IN (
                    SELECT address, MAX(snapshot_at)
                    FROM leaderboard_snapshots
                    GROUP BY address
                )
            ) lb ON ps.address = lb.address
            WHERE ps.snapshot_at > ?
            GROUP BY ps.address
            ORDER BY peak_position DESC
            LIMIT 5
            """,
            (since,),
        ).fetchall()

    return {
        "top_coins": top_coins,
        "confluence_count": confluence_count,
        "whale_alert_count": whale_count,
        "top_whales": top_whales,
    }


def _format_digest_free(stats: dict) -> str:
    """Summary digest for the free channel — no wallet PnL, creates FOMO for Pro."""
    week_str  = datetime.utcnow().strftime("%b %d")
    since_str = (datetime.utcnow() - timedelta(days=7)).strftime("%b %d")

    lines = [
        f"📊 <b>HL Intel Weekly Digest</b>",
        f"━━━━━━━━━━━━━━━━",
        f"Week of {since_str} — {week_str}",
        f"",
        f"🐋 <b>Whale Alerts:</b> {stats['whale_alert_count']}",
        f"🔀 <b>Confluence Events:</b> {stats['confluence_count']}",
        f"",
        f"<b>Most Active Markets:</b>",
    ]

    for row in stats["top_coins"]:
        side_emoji = "🟢" if row["side"] == "long" else "🔴"
        direction = "LONG" if row["side"] == "long" else "SHORT"
        lines.append(
            f"  {side_emoji} {row['coin']}-PERP {direction} | "
            f"{row['whale_count']} whales active"
        )

    lines += [
        f"",
        f"🔒 <b>Pro digest includes:</b> wallet PnL, peak positions,",
        f"individual whale performance, and full confluence log.",
        f"→ <b>@HLIntelPro</b>",
        f"━━━━━━━━━━━━━━━━",
        f"<i>Not financial advice. Data only.</i>",
    ]
    return "\n".join(lines)


def _format_digest(stats: dict) -> str:
    """Full digest for the Pro channel — includes wallet PnL."""
    week_str = datetime.utcnow().strftime("%b %d")
    since_str = (datetime.utcnow() - timedelta(days=7)).strftime("%b %d")

    lines = [
        f"📊 <b>HL Intel Pro — Weekly Digest</b>",
        f"━━━━━━━━━━━━━━━━",
        f"Week of {since_str} — {week_str}",
        f"",
        f"🐋 <b>Whale Alerts Fired:</b> {stats['whale_alert_count']}",
        f"🔀 <b>Confluence Events:</b> {stats['confluence_count']}",
        f"",
        f"<b>Most Active Markets:</b>",
    ]

    for row in stats["top_coins"]:
        side_emoji = "🟢" if row["side"] == "long" else "🔴"
        direction = "LONG" if row["side"] == "long" else "SHORT"
        lines.append(
            f"  {side_emoji} {row['coin']}-PERP {direction} | "
            f"{row['whale_count']} whales | "
            f"Peak ${row['peak_notional']:,.0f}"
        )

    lines += [
        f"",
        f"<b>Top Wallets This Week:</b>",
    ]

    for row in stats["top_whales"]:
        addr = row["address"]
        short = f"{addr[:6]}...{addr[-4:]}"
        rank_str = f"#{row['rank']} " if row["rank"] else ""
        week_pnl = row["week_pnl"] or 0
        pnl_str = f"+${week_pnl:,.0f}" if week_pnl >= 0 else f"-${abs(week_pnl):,.0f}"
        pnl_emoji = "📈" if week_pnl >= 0 else "📉"
        lines.append(
            f"  {rank_str}<code>{short}</code>\n"
            f"    {pnl_emoji} Week PnL: <b>{pnl_str}</b> | "
            f"Peak: ${row['peak_position']:,.0f} | "
            f"{row['coins_traded']} markets"
        )

    lines += [
        f"",
        f"━━━━━━━━━━━━━━━━",
        f"<i>Not financial advice. Data only.</i>",
    ]

    return "\n".join(lines)


async def maybe_send_weekly_digest() -> None:
    now = datetime.utcnow()
    # Send on Sundays between 08:00 and 09:00 UTC
    if now.weekday() != 6 or now.hour != 8:
        return

    # Prevent duplicate sends within the same hour
    with get_conn() as conn:
        recent = conn.execute(
            """
            SELECT sent_at FROM alerts_sent
            WHERE alert_type = 'weekly_digest'
            AND sent_at > datetime('now', '-2 hours')
            LIMIT 1
            """,
        ).fetchone()

    if recent:
        return

    log.info("Sending weekly digest...")
    stats = _get_week_stats()

    # Single destination (personal tool): send the full digest once.
    pro_msg = _format_digest(stats)
    sent = await safe_send(pro_msg)

    if sent:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO alerts_sent (alert_type, key, sent_at) VALUES (?, ?, datetime('now'))",
                ("weekly_digest", f"week_{now.strftime('%Y_%W')}"),
            )
        log.info("Weekly digest sent — free (summary) + pro (full).")

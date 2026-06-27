"""SQLite persistence + alert-dedup ledger.

Ported from the repo scanner's data/database.py with three audited fixes:
  W3 - all snapshot timestamps now written with SQLite datetime('now') so they
       compare correctly against the window queries (was Python isoformat 'T').
  W4 - get_previous_positions returns the single most-recent prior snapshot per
       (coin, side) instead of the oldest-of-50, so size-increase baselines are
       the immediately previous cycle.
  W5 - prune_old_data() retention job to stop unbounded table growth.

Plus two new tables for the personal-tool toggle:
  subscribers  - chat ids that have toggled the bot on (+ alerts pause flag)
  app_state    - small key/value store (e.g. seed bookkeeping)
"""
import sqlite3
from contextlib import contextmanager
from typing import Iterator

import config

DB_PATH = config.DB_PATH


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                rank INTEGER NOT NULL,
                account_value REAL NOT NULL,
                day_pnl REAL,
                week_pnl REAL,
                snapshot_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS position_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                size REAL NOT NULL,
                notional_usd REAL NOT NULL,
                entry_px REAL NOT NULL,
                liq_px REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL,
                snapshot_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oi_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                oi_usd REAL NOT NULL,
                funding REAL NOT NULL,
                mark_px REAL NOT NULL,
                snapshot_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS funding_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                funding_rate REAL NOT NULL,
                open_interest REAL NOT NULL,
                mark_px REAL NOT NULL,
                snapshot_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts_sent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                key TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_labels (
                address TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT 'unknown',
                name TEXT,
                notes TEXT,
                tagged_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_performance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                account_value REAL NOT NULL,
                exposure_total REAL NOT NULL,
                open_upnl REAL NOT NULL,
                negative_upnl REAL NOT NULL,
                open_positions INTEGER NOT NULL,
                book_leverage REAL NOT NULL,
                state TEXT NOT NULL,
                health_score REAL NOT NULL DEFAULT 50,
                smart_score REAL NOT NULL DEFAULT 0,
                snapshot_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                alerts_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_profiles (
                address TEXT PRIMARY KEY,
                codename TEXT,
                -- point in time (refreshed every cycle):
                smart_score REAL, skill_tier TEXT, state TEXT,
                account_value REAL, book_leverage REAL,
                day_roi REAL, week_roi REAL, month_roi REAL,
                day_pnl REAL, week_pnl REAL, month_pnl REAL,
                -- accumulated over observed history:
                cycles_observed INTEGER NOT NULL DEFAULT 0,
                sum_leverage REAL NOT NULL DEFAULT 0,
                adds_total INTEGER NOT NULL DEFAULT 0,
                adds_to_losers INTEGER NOT NULL DEFAULT 0,
                cuts_total INTEGER NOT NULL DEFAULT 0,
                cuts_in_loss INTEGER NOT NULL DEFAULT 0,
                closes_observed INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                sum_hold_seconds REAL NOT NULL DEFAULT 0,
                hold_samples INTEGER NOT NULL DEFAULT 0,
                max_drawdown_usd REAL NOT NULL DEFAULT 0,
                flips_total INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS wallet_open_lots (
                address TEXT NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
                opened_at TEXT NOT NULL, last_pnl REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (address, coin, side)
            );
            CREATE TABLE IF NOT EXISTS wallet_behavior_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL, event_type TEXT NOT NULL,
                coin TEXT, ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_behavior_addr_ts
                ON wallet_behavior_events(address, ts DESC);
            CREATE TABLE IF NOT EXISTS candidate_wallets (
                address TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'suggested',  -- suggested|tracked|rejected|retired
                smart_score REAL,
                week_roi REAL,
                month_roi REAL,
                leverage REAL,
                account_value REAL,
                reason TEXT,
                negative_streak INTEGER NOT NULL DEFAULT 0,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        try:
            conn.execute("ALTER TABLE position_snapshots ADD COLUMN liq_px REAL NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE wallet_performance_snapshots ADD COLUMN health_score REAL NOT NULL DEFAULT 50")
        except Exception:
            pass
        try:
            # smart_score: skill-weighted ranking (trailing ROI minus risk penalties).
            conn.execute("ALTER TABLE wallet_performance_snapshots ADD COLUMN smart_score REAL NOT NULL DEFAULT 0")
        except Exception:
            pass
        # --- pay-to-activate migrations (idempotent) ---
        try:
            # entitlement expiry; null = never paid / not entitled.
            conn.execute("ALTER TABLE subscribers ADD COLUMN paid_until TEXT")
        except Exception:
            pass
        try:
            # replay protection: each tx signature can be redeemed at most once.
            conn.execute("""CREATE TABLE IF NOT EXISTS used_payments (
                tx_signature TEXT PRIMARY KEY,
                chat_id INTEGER,
                used_at TEXT
            )""")
        except Exception:
            pass
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_leaderboard_address_snapshot
                ON leaderboard_snapshots(address, snapshot_at DESC);
            CREATE INDEX IF NOT EXISTS idx_position_address_snapshot
                ON position_snapshots(address, snapshot_at DESC);
            CREATE INDEX IF NOT EXISTS idx_position_address_coin_side_snapshot
                ON position_snapshots(address, coin, side, snapshot_at DESC);
            CREATE INDEX IF NOT EXISTS idx_position_snapshot
                ON position_snapshots(snapshot_at DESC);
            CREATE INDEX IF NOT EXISTS idx_funding_asset_snapshot
                ON funding_snapshots(asset, snapshot_at DESC);
            CREATE INDEX IF NOT EXISTS idx_oi_coin_snapshot
                ON oi_snapshots(coin, snapshot_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alerts_type_key_sent
                ON alerts_sent(alert_type, key, sent_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_performance_address_snapshot
                ON wallet_performance_snapshots(address, snapshot_at DESC);
        """)


# --------------------------- writes (W3: datetime('now')) ---------------------------
def save_leaderboard(rows: list[dict]) -> None:
    with get_conn() as conn:
        for rank, row in enumerate(rows, start=1):
            perfs = dict(row["windowPerformances"])
            conn.execute(
                """INSERT INTO leaderboard_snapshots
                   (address, rank, account_value, day_pnl, week_pnl, snapshot_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (
                    row["ethAddress"],
                    rank,
                    float(row["accountValue"]),
                    float(perfs.get("day", {}).get("pnl", 0)),
                    float(perfs.get("week", {}).get("pnl", 0)),
                ),
            )


def save_positions(address: str, positions: list[dict]) -> None:
    with get_conn() as conn:
        for pos in positions:
            conn.execute(
                """INSERT INTO position_snapshots
                   (address, coin, side, size, notional_usd, entry_px, liq_px, unrealized_pnl, snapshot_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    address,
                    pos["coin"],
                    pos["side"],
                    pos["size"],
                    pos["notional_usd"],
                    pos["entry_px"],
                    pos.get("liq_px", 0),
                    pos["unrealized_pnl"],
                ),
            )


def save_oi_snapshot(coin: str, oi_usd: float, funding: float, mark_px: float) -> None:
    """Append an open-interest snapshot for a coin (mirrors funding_snapshots)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO oi_snapshots (coin, oi_usd, funding, mark_px, snapshot_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (coin, oi_usd, funding, mark_px),
        )


def get_oi_ago(coin: str, minutes: int) -> sqlite3.Row | None:
    """Most-recent OI snapshot at least `minutes` old (None if no such history)."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM oi_snapshots
               WHERE coin = ? AND snapshot_at <= datetime('now', ?)
               ORDER BY snapshot_at DESC LIMIT 1""",
            (coin, f"-{int(minutes)} minutes"),
        ).fetchone()


def save_funding(assets: list[dict]) -> None:
    with get_conn() as conn:
        for a in assets:
            conn.execute(
                """INSERT INTO funding_snapshots
                   (asset, funding_rate, open_interest, mark_px, snapshot_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (a["name"], a["funding"], a["open_interest"], a["mark_px"]),
            )


def save_wallet_performance_snapshot(
    address: str, account_value: float, exposure_total: float, open_upnl: float,
    negative_upnl: float, open_positions: int, book_leverage: float, state: str,
    health_score: float = 50.0, smart_score: float = 0.0,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO wallet_performance_snapshots
               (address, account_value, exposure_total, open_upnl, negative_upnl,
                open_positions, book_leverage, state, health_score, smart_score, snapshot_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (address.lower(), account_value, exposure_total, open_upnl,
             negative_upnl, open_positions, book_leverage, state, health_score, smart_score),
        )


# --------------------------- reads ---------------------------
def get_latest_wallet_performance(address: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM wallet_performance_snapshots
               WHERE address = ? ORDER BY snapshot_at DESC, id DESC LIMIT 1""",
            (address.lower(),),
        ).fetchone()


def get_latest_scores(limit: int = 200) -> list[sqlite3.Row]:
    """Latest snapshot per wallet, ranked best -> worst by smart_score (skill)."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT w.* FROM wallet_performance_snapshots w
               INNER JOIN (
                   SELECT address, MAX(snapshot_at) AS latest
                   FROM wallet_performance_snapshots
                   GROUP BY address
               ) m ON w.address = m.address AND w.snapshot_at = m.latest
               ORDER BY w.smart_score DESC, w.health_score DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def get_latest_smart_scores(addresses: list[str]) -> dict[str, float]:
    """Latest smart_score per address (lower-cased keys). Missing → absent."""
    addrs = [a.lower() for a in addresses]
    if not addrs:
        return {}
    placeholders = ",".join("?" * len(addrs))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT w.address, w.smart_score FROM wallet_performance_snapshots w
                INNER JOIN (
                    SELECT address, MAX(snapshot_at) AS latest
                    FROM wallet_performance_snapshots
                    WHERE address IN ({placeholders})
                    GROUP BY address
                ) m ON w.address = m.address AND w.snapshot_at = m.latest""",
            addrs,
        ).fetchall()
    return {r["address"]: (r["smart_score"] if r["smart_score"] is not None else 0.0) for r in rows}


def get_previous_positions(address: str) -> list[sqlite3.Row]:
    """W4 fix: most-recent prior snapshot per (coin, side) for this address."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT ps.* FROM position_snapshots ps
               INNER JOIN (
                   SELECT coin, side, MAX(snapshot_at) AS latest_at
                   FROM position_snapshots WHERE address = ?
                   GROUP BY coin, side
               ) m ON ps.coin = m.coin AND ps.side = m.side
                  AND ps.snapshot_at = m.latest_at
               WHERE ps.address = ?""",
            (address, address),
        ).fetchall()


def get_last_snapshot_positions(address: str) -> list[sqlite3.Row]:
    """All open positions from the single most-recent snapshot batch for a wallet.

    Unlike get_previous_positions (latest-per-(coin,side) ever, which keeps stale
    long-closed coins around), this is exactly the previous cycle's holdings — the
    correct baseline for close/flip detection, so a coin closed cycles ago is not
    repeatedly re-reported. Call this BEFORE save_positions for the new cycle.
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT ps.* FROM position_snapshots ps
               WHERE ps.address = ?
                 AND ps.snapshot_at = (
                     SELECT MAX(snapshot_at) FROM position_snapshots WHERE address = ?
                 )""",
            (address, address),
        ).fetchall()


def get_latest_position_snapshot_at(address: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(snapshot_at) AS latest_at FROM position_snapshots WHERE address = ?",
            (address,),
        ).fetchone()
    return row["latest_at"] if row else None


def get_previous_funding(asset: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM funding_snapshots
               WHERE asset = ? ORDER BY snapshot_at DESC LIMIT 1""",
            (asset,),
        ).fetchone()


def get_funding_ago(asset: str, minutes: int = 60) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM funding_snapshots
               WHERE asset = ? AND snapshot_at <= datetime('now', ?)
               ORDER BY snapshot_at DESC LIMIT 1""",
            (asset, f"-{minutes} minutes"),
        ).fetchone()


def get_recent_positions_for_addresses(addresses: list[str], window_minutes: int = 10) -> list[sqlite3.Row]:
    if not addresses:
        return []
    placeholders = ",".join("?" * len(addresses))
    with get_conn() as conn:
        return conn.execute(
            f"""SELECT ps.* FROM position_snapshots ps
                INNER JOIN (
                    SELECT address, MAX(snapshot_at) AS latest_at
                    FROM position_snapshots
                    WHERE address IN ({placeholders})
                      AND snapshot_at > datetime('now', '-{window_minutes} minutes')
                    GROUP BY address
                ) latest ON ps.address = latest.address
                         AND ps.snapshot_at = latest.latest_at
                WHERE ps.notional_usd >= ?""",
            (*addresses, 500_000),
        ).fetchall()


# --------------------------- alert dedup ledger ---------------------------
def alert_already_sent(alert_type: str, key: str, cooldown_minutes: int = 60) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT sent_at FROM alerts_sent
               WHERE alert_type = ? AND key = ? AND sent_at > datetime('now', ?)
               ORDER BY sent_at DESC LIMIT 1""",
            (alert_type, key, f"-{cooldown_minutes} minutes"),
        ).fetchone()
    return row is not None


def get_recent_alerts_by_prefix(alert_type: str, key_prefix: str, cooldown_minutes: int = 60) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT key, sent_at FROM alerts_sent
               WHERE alert_type = ? AND key LIKE ? AND sent_at > datetime('now', ?)
               ORDER BY sent_at DESC""",
            (alert_type, f"{key_prefix}%", f"-{cooldown_minutes} minutes"),
        ).fetchall()


def record_alert(alert_type: str, key: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO alerts_sent (alert_type, key, sent_at) VALUES (?, ?, datetime('now'))",
            (alert_type, key),
        )


# --------------------------- wallet labels ---------------------------
def set_wallet_label(address: str, label: str, name: str = None, notes: str = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO wallet_labels (address, label, name, notes, tagged_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(address) DO UPDATE SET
                 label=excluded.label, name=excluded.name,
                 notes=excluded.notes, tagged_at=excluded.tagged_at""",
            (address.lower(), label, name, notes),
        )


def get_wallet_label(address: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM wallet_labels WHERE address = ?", (address.lower(),)
        ).fetchone()


def get_all_labels() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM wallet_labels ORDER BY label, address").fetchall()


def get_watch_wallets() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM wallet_labels
               WHERE label IN ('watch', 'stress_watch', 'vip')
               ORDER BY label, address"""
        ).fetchall()


def is_algo(address: str) -> bool:
    row = get_wallet_label(address)
    return row is not None and row["label"] == "algo"


def is_vip(address: str) -> bool:
    row = get_wallet_label(address)
    return row is not None and row["label"] == "vip"


# --------------------------- subscribers (toggle) ---------------------------
def activate_chat(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO subscribers (chat_id, active, alerts_enabled, updated_at)
               VALUES (?, 1, 1, datetime('now'))
               ON CONFLICT(chat_id) DO UPDATE SET active=1, updated_at=datetime('now')""",
            (chat_id,),
        )


def deactivate_chat(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscribers SET active=0, updated_at=datetime('now') WHERE chat_id=?",
            (chat_id,),
        )


def set_alerts_enabled(chat_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscribers SET alerts_enabled=?, updated_at=datetime('now') WHERE chat_id=?",
            (1 if enabled else 0, chat_id),
        )


def get_alerts_enabled(chat_id: int) -> bool:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT alerts_enabled FROM subscribers WHERE chat_id=? AND active=1", (chat_id,)
        ).fetchone()
    return bool(r["alerts_enabled"]) if r else False


def get_active_chats() -> list[int]:
    # "Active" now means: switched on AND holding a live paid_until entitlement,
    # so background work and alerts are driven by payment, not just the toggle.
    with get_conn() as conn:
        return [r["chat_id"] for r in conn.execute(
            """SELECT chat_id FROM subscribers
               WHERE active=1 AND paid_until IS NOT NULL
                 AND datetime(paid_until) > datetime('now')"""
        ).fetchall()]


def get_alert_chats() -> list[int]:
    with get_conn() as conn:
        return [r["chat_id"] for r in conn.execute(
            """SELECT chat_id FROM subscribers
               WHERE active=1 AND alerts_enabled=1 AND paid_until IS NOT NULL
                 AND datetime(paid_until) > datetime('now')"""
        ).fetchall()]


def is_any_active() -> bool:
    return len(get_active_chats()) > 0


# --------------------------- pay-to-activate (entitlement + replay) ---------------------------
def set_paid_until(chat_id: int, iso: str) -> None:
    """Set/extend the chat's entitlement expiry (ISO-8601 timestamp)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO subscribers (chat_id, active, alerts_enabled, paid_until, updated_at)
               VALUES (?, 1, 1, ?, datetime('now'))
               ON CONFLICT(chat_id) DO UPDATE SET
                 paid_until=excluded.paid_until, updated_at=datetime('now')""",
            (chat_id, iso),
        )


def get_paid_until(chat_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT paid_until FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["paid_until"] if row else None


def mark_payment_used(tx_signature: str, chat_id: int) -> None:
    """Record a redeemed tx signature (replay protection). Idempotent."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO used_payments (tx_signature, chat_id, used_at)
               VALUES (?, ?, datetime('now'))""",
            (tx_signature, chat_id),
        )


def is_payment_used(tx_signature: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM used_payments WHERE tx_signature=?", (tx_signature,)
        ).fetchone()
    return row is not None


def mark_free_used(chat_id: int) -> None:
    """Mark this chat's one free /scan as consumed (app_state kv — no active state)."""
    set_state(f"free_used:{chat_id}", "1")


def get_free_used(chat_id: int) -> bool:
    return get_state(f"free_used:{chat_id}") == "1"


# --------------------------- wallet profiles (identity + behavior) ---------------------------
# Counters that bump_profile_counters is allowed to increment (whitelist guards
# the dynamic SQL).
_PROFILE_COUNTERS = {
    "cycles_observed", "sum_leverage", "adds_total", "adds_to_losers",
    "cuts_total", "cuts_in_loss", "closes_observed", "wins", "losses",
    "sum_hold_seconds", "hold_samples", "flips_total",
}


def upsert_profile_point_in_time(
    address: str, codename: str, smart_score: float, skill_tier: str, state: str,
    account_value: float, book_leverage: float,
    day_roi: float, week_roi: float, month_roi: float,
    day_pnl: float, week_pnl: float, month_pnl: float,
) -> None:
    """Refresh the per-cycle (point-in-time) profile fields. Counters untouched.
    Sets first_seen on first insert."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO wallet_profiles
                 (address, codename, smart_score, skill_tier, state, account_value,
                  book_leverage, day_roi, week_roi, month_roi, day_pnl, week_pnl,
                  month_pnl, first_seen, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))
               ON CONFLICT(address) DO UPDATE SET
                 codename=excluded.codename, smart_score=excluded.smart_score,
                 skill_tier=excluded.skill_tier, state=excluded.state,
                 account_value=excluded.account_value, book_leverage=excluded.book_leverage,
                 day_roi=excluded.day_roi, week_roi=excluded.week_roi,
                 month_roi=excluded.month_roi, day_pnl=excluded.day_pnl,
                 week_pnl=excluded.week_pnl, month_pnl=excluded.month_pnl,
                 updated_at=datetime('now')""",
            (address.lower(), codename, smart_score, skill_tier, state, account_value,
             book_leverage, day_roi, week_roi, month_roi, day_pnl, week_pnl, month_pnl),
        )


def bump_profile_counters(address: str, deltas: dict,
                          drawdown_candidate: float | None = None) -> None:
    """Increment accumulated counters; track most-negative open uPnL as drawdown.
    Requires the profile row to exist (call upsert_profile_point_in_time first)."""
    sets, params = [], []
    for col, delta in deltas.items():
        if col not in _PROFILE_COUNTERS:
            raise ValueError(f"unknown profile counter: {col}")
        sets.append(f"{col} = {col} + ?")
        params.append(delta)
    if drawdown_candidate is not None:
        sets.append("max_drawdown_usd = MIN(max_drawdown_usd, ?)")
        params.append(float(drawdown_candidate))
    if not sets:
        return
    params.append(address.lower())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE wallet_profiles SET {', '.join(sets)}, updated_at=datetime('now') "
            f"WHERE address = ?",
            params,
        )


def get_wallet_profile(address: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM wallet_profiles WHERE address = ?", (address.lower(),)
        ).fetchone()


def get_wallet_profile_by_codename(codename: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM wallet_profiles WHERE LOWER(codename) = ?",
            (codename.strip().lower(),),
        ).fetchone()


# --- open-lot tracking (for hold duration + close outcome) ---
def upsert_open_lot(address: str, coin: str, side: str, opened_at: str, last_pnl: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO wallet_open_lots (address, coin, side, opened_at, last_pnl)
               VALUES (?,?,?,?,?)
               ON CONFLICT(address, coin, side) DO UPDATE SET last_pnl=excluded.last_pnl""",
            (address.lower(), coin, side, opened_at, last_pnl),
        )


def update_open_lot_pnl(address: str, coin: str, side: str, last_pnl: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE wallet_open_lots SET last_pnl=? WHERE address=? AND coin=? AND side=?",
            (last_pnl, address.lower(), coin, side),
        )


def get_open_lot(address: str, coin: str, side: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM wallet_open_lots WHERE address=? AND coin=? AND side=?",
            (address.lower(), coin, side),
        ).fetchone()


def remove_open_lot(address: str, coin: str, side: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM wallet_open_lots WHERE address=? AND coin=? AND side=?",
            (address.lower(), coin, side),
        )


# --- recent behavior events (for the flailing / chop signal) ---
def record_behavior_event(address: str, event_type: str, coin: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO wallet_behavior_events (address, event_type, coin, ts) "
            "VALUES (?,?,?, datetime('now'))",
            (address.lower(), event_type, coin),
        )


def count_behavior_events(address: str, event_types: list[str], minutes: int) -> int:
    if not event_types:
        return 0
    placeholders = ",".join("?" * len(event_types))
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS n FROM wallet_behavior_events
                WHERE address=? AND event_type IN ({placeholders})
                  AND ts > datetime('now', ?)""",
            (address.lower(), *event_types, f"-{int(minutes)} minutes"),
        ).fetchone()
    return row["n"] if row else 0


# --------------------------- app_state kv ---------------------------
def set_state(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO app_state (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value),
        )


def get_state(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# --------------------------- wallet discovery (candidate lifecycle) ---------------------------
def get_candidate(address: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM candidate_wallets WHERE address=?", (address.lower(),)
        ).fetchone()


def upsert_suggested_candidate(address: str, smart_score: float, week_roi: float,
                               month_roi: float, leverage: float,
                               account_value: float, reason: str) -> bool:
    """Insert a new 'suggested' candidate, or refresh metrics on an existing
    suggested/tracked one. Returns True only when a *new* suggestion is created
    (so the caller knows whether to alert). Never resurrects rejected/retired."""
    address = address.lower()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT status FROM candidate_wallets WHERE address=?", (address,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO candidate_wallets
                   (address, status, smart_score, week_roi, month_roi, leverage,
                    account_value, reason, negative_streak, discovered_at, updated_at)
                   VALUES (?, 'suggested', ?, ?, ?, ?, ?, ?, 0, datetime('now'), datetime('now'))""",
                (address, smart_score, week_roi, month_roi, leverage, account_value, reason),
            )
            return True
        # Refresh metrics for ones still in play; leave rejected/retired alone.
        if existing["status"] in ("suggested", "tracked"):
            conn.execute(
                """UPDATE candidate_wallets
                   SET smart_score=?, week_roi=?, month_roi=?, leverage=?,
                       account_value=?, reason=?, updated_at=datetime('now')
                   WHERE address=?""",
                (smart_score, week_roi, month_roi, leverage, account_value, reason, address),
            )
        return False


def set_candidate_status(address: str, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE candidate_wallets
               SET status=?, negative_streak=0, updated_at=datetime('now')
               WHERE address=?""",
            (status, address.lower()),
        )


def get_candidates_by_status(status: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM candidate_wallets WHERE status=?
               ORDER BY smart_score DESC""",
            (status,),
        ).fetchall()


def get_tracked_candidate_addresses() -> list[str]:
    with get_conn() as conn:
        return [r["address"] for r in conn.execute(
            "SELECT address FROM candidate_wallets WHERE status='tracked'"
        ).fetchall()]


def bump_candidate_negative_streak(address: str) -> int:
    """Increment and return the consecutive negative-window streak."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE candidate_wallets
               SET negative_streak = negative_streak + 1, updated_at=datetime('now')
               WHERE address=?""",
            (address.lower(),),
        )
        row = conn.execute(
            "SELECT negative_streak FROM candidate_wallets WHERE address=?", (address.lower(),)
        ).fetchone()
    return row["negative_streak"] if row else 0


def reset_candidate_negative_streak(address: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE candidate_wallets SET negative_streak=0, updated_at=datetime('now') WHERE address=?",
            (address.lower(),),
        )


# --------------------------- retention (W5) ---------------------------
def prune_old_data(days: int | None = None) -> None:
    days = config.RETENTION_DAYS if days is None else days
    cutoff = f"-{days} days"
    with get_conn() as conn:
        for table in (
            "leaderboard_snapshots", "position_snapshots",
            "funding_snapshots", "oi_snapshots",
            "wallet_performance_snapshots", "alerts_sent",
        ):
            ts_col = "sent_at" if table == "alerts_sent" else "snapshot_at"
            conn.execute(f"DELETE FROM {table} WHERE {ts_col} < datetime('now', ?)", (cutoff,))
        # Behavioral events feed only the short-window flailing signal; keep them brief.
        conn.execute("DELETE FROM wallet_behavior_events WHERE ts < datetime('now', ?)", (cutoff,))

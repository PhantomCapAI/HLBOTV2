"""Tests for the 'calm the firehose' noise-control pass.

Covers:
  * OI notional units fix — open interest is reported in coins; the figure shown
    and the size floor use USD notional (coins * markPx), not the raw coin count.
  * OI cold-start guard — no alert without a valid ~1h-ago baseline.
  * OI sanity bound — absurd notionals (bad data) are dropped.
  * OI per-cycle cap — only the top-N by magnitude fire in one cycle.
  * Wallet-health identity — HOT STREAK / SELF-IMPLODING carry the codename
    headline, not a bare rank + raw address.
  * Wallet-health per-cycle cap — held sends don't burn the cooldown.
  * Coin/wallet cold-start — the first cycle after process start is silent.

Run: pytest tests/test_alert_noise.py
"""
import asyncio

import pytest

import config
from storage import database as db
from trackers import wallet_tracker as wt


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


def _seed_old_funding(asset, oi_coins, mark_px, minutes_ago=70):
    """Insert a funding snapshot dated `minutes_ago` in the past (a baseline)."""
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO funding_snapshots
               (asset, funding_rate, open_interest, mark_px, snapshot_at)
               VALUES (?, ?, ?, ?, datetime('now', ?))""",
            (asset, 0.0, oi_coins, mark_px, f"-{minutes_ago} minutes"),
        )


def _asset(name, oi_coins, mark_px, funding=0.0):
    return {"name": name, "open_interest": oi_coins, "mark_px": mark_px,
            "funding": funding, "day_volume": 0.0}


def _patch_send(monkeypatch, sent):
    async def fake_send_alert(msg, paid_only=False):
        sent.append(msg)
    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(wt, "send_alert", fake_send_alert)
    monkeypatch.setattr(wt.asyncio, "sleep", no_sleep)


# --------------------------- OI notional units ---------------------------
def test_oi_notional_is_usd_not_raw_coins(tmp_db, monkeypatch):
    """A PUMP-like market: 24.7B coins @ $0.004 is a ~$99M book, not '$24.7B'."""
    monkeypatch.setattr(config, "OI_SURGE_PCT_THRESHOLD", 40.0)
    monkeypatch.setattr(config, "MIN_OI_FOR_SURGE", 50_000_000)
    px = 0.004
    _seed_old_funding("PUMP", oi_coins=15_000_000_000, mark_px=px)  # +64.7% to 24.7B
    cands = wt.collect_oi_surge_candidates([_asset("PUMP", 24_700_000_000, px)])
    assert len(cands) == 1
    c = cands[0]
    # notional = coins * px ≈ $98.8M, NOT the raw 24.7B coin count.
    assert abs(c["curr_notional"] - 24_700_000_000 * px) < 1.0
    assert c["curr_notional"] < 200_000_000
    assert round(c["pct_change"]) == 65

    sent = []
    _patch_send(monkeypatch, sent)
    asyncio.run(wt.check_oi_surges([_asset("PUMP", 24_700_000_000, px)], seed_mode=False))
    assert len(sent) == 1                        # ONE digest, not one-per-coin
    assert "OI FLOW" in sent[0]
    # The displayed dollar figure is the notional (~$98.8M), never the coin count.
    assert "24,700,000,000" not in sent[0]
    assert "98,800,000" in sent[0]


def test_oi_no_alert_without_baseline(tmp_db, monkeypatch):
    """First observation (no ~1h-ago snapshot) never fires."""
    monkeypatch.setattr(config, "OI_SURGE_PCT_THRESHOLD", 40.0)
    monkeypatch.setattr(config, "MIN_OI_FOR_SURGE", 50_000_000)
    cands = wt.collect_oi_surge_candidates([_asset("BTC", 100_000, 100_000)])
    assert cands == []


def test_oi_sanity_bound_drops_absurd_notional(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "OI_SURGE_PCT_THRESHOLD", 40.0)
    monkeypatch.setattr(config, "MIN_OI_FOR_SURGE", 50_000_000)
    monkeypatch.setattr(config, "OI_NOTIONAL_SANITY_MAX_USD", 100_000_000_000)
    # 2e9 coins @ $100k = $2e14 notional — impossible, must be dropped.
    _seed_old_funding("GLITCH", oi_coins=1_000_000_000, mark_px=100_000)
    cands = wt.collect_oi_surge_candidates([_asset("GLITCH", 2_000_000_000, 100_000)])
    assert cands == []


def test_oi_below_floor_and_threshold_skipped(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "OI_SURGE_PCT_THRESHOLD", 40.0)
    monkeypatch.setattr(config, "MIN_OI_FOR_SURGE", 50_000_000)
    # Below USD floor: $1M notional.
    _seed_old_funding("TINY", oi_coins=5_000, mark_px=100)
    assert wt.collect_oi_surge_candidates([_asset("TINY", 10_000, 100)]) == []
    # Above floor but only +10% change (< 40% threshold).
    _seed_old_funding("BIG", oi_coins=1_000_000, mark_px=100)
    assert wt.collect_oi_surge_candidates([_asset("BIG", 1_100_000, 100)]) == []


def test_oi_digest_is_one_message_top_n(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "OI_SURGE_PCT_THRESHOLD", 40.0)
    monkeypatch.setattr(config, "MIN_OI_FOR_SURGE", 50_000_000)
    monkeypatch.setattr(config, "OI_DIGEST_MAX", 2)
    px = 100.0
    # Four eligible markets with increasing magnitude.
    specs = [("AAA", 1_000_000, 1_500_000),   # +50%
             ("BBB", 1_000_000, 2_000_000),   # +100%
             ("CCC", 1_000_000, 3_000_000),   # +200%
             ("DDD", 1_000_000, 2_500_000)]   # +150%
    for name, base, _now in specs:
        _seed_old_funding(name, oi_coins=base, mark_px=px)
    assets = [_asset(name, now, px) for name, _b, now in specs]

    cands = wt.collect_oi_surge_candidates(assets)
    assert [c["name"] for c in cands] == ["CCC", "DDD", "BBB", "AAA"]  # by |%| desc

    sent = []
    _patch_send(monkeypatch, sent)
    asyncio.run(wt.check_oi_surges(assets, seed_mode=False))
    assert len(sent) == 1                        # ONE consolidated digest
    # Digest carries the top-2 by magnitude only.
    assert "CCC-PERP" in sent[0] and "DDD-PERP" in sent[0]
    assert "BBB-PERP" not in sent[0] and "AAA-PERP" not in sent[0]
    # And the cooldown is burned only for the included movers.
    assert db.alert_already_sent("oi_surge", "oi_surge:CCC", cooldown_minutes=240)
    assert not db.alert_already_sent("oi_surge", "oi_surge:AAA", cooldown_minutes=240)


# --------------------------- wallet-health identity + cap ---------------------------
def _imploding_positions(account_value):
    # Single big long deep underwater → self_imploding (upnl_pct <= -20%).
    notional = account_value * 2
    return [{"coin": "BTC", "side": "long", "size": 10.0,
             "notional_usd": notional, "entry_px": notional / 10.0,
             "liq_px": 0.0, "unrealized_pnl": -account_value * 0.35}]


def _seed_prev_health(addr, account_value):
    db.save_wallet_performance_snapshot(
        address=addr, account_value=account_value, exposure_total=0.0,
        open_upnl=0.0, negative_upnl=0.0, open_positions=0, book_leverage=0.0,
        state="stable",
    )


def test_wallet_health_flag_collected_with_identity(tmp_db):
    addr = "0x" + "d" * 40
    av = 2_000_000.0
    _seed_prev_health(addr, av)  # prior snapshot so `previous` exists
    flag = wt.collect_wallet_health_flag(
        row={"ethAddress": addr, "windowPerformances": {}}, address=addr, rank=7,
        positions=_imploding_positions(av), account_value=av, seed_mode=False)
    assert flag is not None
    assert flag["state"] == "self_imploding"
    from core import identity
    assert flag["codename"] == identity.codename_for(addr)  # identity carried


def test_wallet_health_digest_is_one_message_capped(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "WALLET_HEALTH_DIGEST_MAX", 2)
    av = 2_000_000.0
    flags = []
    for i in range(4):
        addr = "0x" + f"{i:040x}"
        _seed_prev_health(addr, av)
        flag = wt.collect_wallet_health_flag(
            row={"ethAddress": addr, "windowPerformances": {}}, address=addr, rank=i,
            positions=_imploding_positions(av), account_value=av, seed_mode=False)
        flags.append(flag)
    assert all(f is not None for f in flags)

    sent = []
    _patch_send(monkeypatch, sent)
    asyncio.run(wt.send_wallet_health_digest(flags))
    assert len(sent) == 1                         # ONE digest for all flagged wallets
    assert "WALLET HEALTH" in sent[0]
    assert "SELF-IMPLODING" in sent[0]
    assert "🪪" in sent[0]                         # identity in the digest
    # Only the included (top-2) wallets burn their per-state cooldown.
    burned = sum(
        1 for f in flags
        if db.alert_already_sent("wallet_performance", f["alert_key"], cooldown_minutes=360)
    )
    assert burned == 2


def test_wallet_health_cooldown_suppresses_repeat(tmp_db):
    addr = "0x" + "e" * 40
    av = 2_000_000.0
    _seed_prev_health(addr, av)
    flag = wt.collect_wallet_health_flag(
        row={"ethAddress": addr, "windowPerformances": {}}, address=addr, rank=7,
        positions=_imploding_positions(av), account_value=av, seed_mode=False)
    assert flag is not None
    db.record_alert("wallet_performance", flag["alert_key"])  # already alerted
    again = wt.collect_wallet_health_flag(
        row={"ethAddress": addr, "windowPerformances": {}}, address=addr, rank=7,
        positions=_imploding_positions(av), account_value=av, seed_mode=False)
    assert again is None                          # on cooldown → not re-flagged


# --------------------------- confluence quality floor + digest ---------------------------
def _whale(addr_tail, smart, notional=1_000_000.0, rank=1):
    addr = "0x" + addr_tail * 40
    return {"address": addr, "codename": None, "rank": rank,
            "notional": notional, "entry_px": 100.0, "smart": smart}


def test_confluence_quality_floor(monkeypatch):
    monkeypatch.setattr(config, "CONFLUENCE_MIN_COMBINED_SMART", 15.0)
    monkeypatch.setattr(config, "CONFLUENCE_STRONG_WALLET_SMART", 10.0)
    monkeypatch.setattr(config, "CONFLUENCE_MIN_DISTINCT_STRONG", 1)
    # "+13.9, same two wallets, one negative" — below the combined floor → dropped.
    noise = [_whale("a", 20.0), _whale("b", -6.1)]
    assert wt.confluence_group_passes_quality(13.9, noise) is False
    # Strong combined but no individually-strong wallet → dropped.
    weak = [_whale("c", 9.0), _whale("d", 9.0)]
    assert wt.confluence_group_passes_quality(18.0, weak) is False
    # Real edge with a strong wallet → passes.
    good = [_whale("e", 25.0), _whale("f", 5.0)]
    assert wt.confluence_group_passes_quality(30.0, good) is True


def test_confluence_digest_format_detail_vs_brief():
    from bot import formatting_wallet as fw
    detailed = [{"coin": "BTC", "side": "long", "whale_count": 3,
                 "combined_smart": 52.0, "total_notional": 12_000_000.0,
                 "whales": [_whale("e", 25.0, rank=3), _whale("f", 15.0, rank=8),
                            _whale("g", 12.0, rank=12)]}]
    brief = [{"coin": "ETH", "side": "short", "whale_count": 2,
              "combined_smart": 18.0, "total_notional": 4_000_000.0,
              "whales": [_whale("h", 10.0), _whale("i", 8.0)]}]
    msg = fw.confluence_digest(detailed, brief)
    assert "WHALE CONFLUENCE" in msg
    assert "BTC LONG" in msg and "ETH SHORT" in msg
    assert "Also aligned:" in msg                 # brief section present
    # Detailed group lists individual wallets; brief does not add wallet lines.
    assert msg.count("🪪") == 3                    # only the detailed group's 3 wallets


# --------------------------- coin cold-start ---------------------------
def test_coin_cold_start_is_silent_then_alerts(tmp_db, monkeypatch):
    """First coin_job after process start seeds dedup silently; the next alerts."""
    from services import cycles

    setups = [{"coin": "BTC", "score": 99.0, "setups": [{"direction": "long"}]}]

    async def fake_scan():
        return setups
    async def fake_corr_scan(coins):
        return []

    broadcasts = []
    recorded = []

    async def fake_maybe_send(alert_type, key, text, **kw):
        broadcasts.append(key)
        return True

    monkeypatch.setattr(cycles, "coin_scan", fake_scan)
    monkeypatch.setattr(cycles, "correlation_scan", fake_corr_scan)
    monkeypatch.setattr(cycles.alerts_svc, "maybe_send", fake_maybe_send)
    monkeypatch.setattr(cycles, "format_setup", lambda s: "setup")
    monkeypatch.setattr(cycles.corr, "current_wallet_confluence", lambda: [])
    monkeypatch.setattr(cycles.db, "record_alert",
                        lambda t, k: recorded.append(k))
    monkeypatch.setattr(config, "MIN_SCORE_FOR_ALERT", 80.0)
    # Force active so _should_run() is True without DB state.
    monkeypatch.setattr(cycles, "_should_run", lambda: True)
    cycles._coin_cold_start_pending = True

    asyncio.run(cycles.coin_job(None))
    assert broadcasts == []                      # silent on cold start
    assert "coin:BTC:long" in recorded           # but baseline seeded
    assert cycles._coin_cold_start_pending is False

    asyncio.run(cycles.coin_job(None))
    assert "coin:BTC:long" in broadcasts         # now it actually alerts

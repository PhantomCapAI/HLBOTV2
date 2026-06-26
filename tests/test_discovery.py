"""Tests for automated wallet discovery (skill-ranked promotion).

Covers:
  * market-maker / delta-neutral detection from current positions.
  * candidate lifecycle in the DB (suggest → track → retire, streaks).
  * the discovery cycle: multi-window pre-filter (week AND month ROI), MM skip,
    leverage cap, smart-score gate, exclusion of already-tracked wallets, and
    human-gated suggestion vs capped auto-add.
  * auto-retirement of discovered wallets after N negative cycles.
  * /candidates and /track operator commands.

Run: pytest tests/test_discovery.py
"""
import types
import asyncio

import pytest

import config
from storage import database as db
from trackers import wallet_tracker as wt
from services import cycles as cy


# --------------------------- fixtures ---------------------------
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


def _async(value=None):
    async def _coro(*a, **k):
        return value
    return _coro


# --------------------------- helpers to craft HL payloads ---------------------------
def _lb_row(address, week_roi, month_roi, account_value):
    """A leaderboard row in the real list-of-pairs windowPerformances shape."""
    return {
        "ethAddress": address,
        "accountValue": str(account_value),
        "windowPerformances": [
            ["day", {"pnl": "1", "roi": "0.001"}],
            ["week", {"pnl": "1", "roi": str(week_roi)}],
            ["month", {"pnl": "1", "roi": str(month_roi)}],
            ["allTime", {"pnl": "1", "roi": "0.1"}],
        ],
    }


def _state(positions):
    """A clearinghouseState-like dict from (coin, side, notional) tuples (entry=100)."""
    asset_positions = []
    for coin, side, notional in positions:
        size = notional / 100.0
        szi = size if side == "long" else -size
        asset_positions.append({"position": {
            "coin": coin, "szi": str(szi), "entryPx": "100",
            "liquidationPx": "0", "unrealizedPnl": "0",
        }})
    return {"assetPositions": asset_positions}


# --------------------------- MM detection ---------------------------
def test_market_maker_flagged():
    book = [(c, "long" if i % 2 == 0 else "short", 1_000_000)
            for i, c in enumerate(["BTC", "ETH", "SOL", "AVAX", "ARB", "OP", "DOGE", "LINK"])]
    positions = wt.parse_positions(_state(book))
    is_mm, reason = wt.looks_like_market_maker(positions, min_coins=6, max_net_gross_ratio=0.25)
    assert is_mm is True
    assert "delta-neutral" in reason


def test_directional_not_flagged():
    positions = wt.parse_positions(_state([("BTC", "long", 5_000_000), ("ETH", "long", 2_000_000)]))
    is_mm, _ = wt.looks_like_market_maker(positions, min_coins=6, max_net_gross_ratio=0.25)
    assert is_mm is False


def test_both_sided_but_net_directional_not_flagged():
    # A small hedge on an otherwise directional book → net/gross stays high.
    book = [("BTC", "long", 9_000_000), ("ETH", "short", 500_000)]
    positions = wt.parse_positions(_state(book))
    # Only 2 coins, so even ignoring the ratio it can't be MM (min_coins=6).
    is_mm, _ = wt.looks_like_market_maker(positions, min_coins=6, max_net_gross_ratio=0.25)
    assert is_mm is False


# --------------------------- candidate lifecycle ---------------------------
def test_candidate_upsert_and_status(tmp_db):
    assert db.upsert_suggested_candidate("0xAbC", 20.0, 0.1, 0.08, 5.0, 1e6, "why") is True
    # second time: not new (refresh), returns False
    assert db.upsert_suggested_candidate("0xabc", 22.0, 0.1, 0.08, 5.0, 1e6, "why2") is False
    cand = db.get_candidate("0xABC")
    assert cand["status"] == "suggested"
    assert cand["smart_score"] == 22.0
    assert [r["address"] for r in db.get_candidates_by_status("suggested")] == ["0xabc"]

    db.set_candidate_status("0xabc", "tracked")
    assert db.get_tracked_candidate_addresses() == ["0xabc"]
    assert db.get_candidates_by_status("suggested") == []


def test_rejected_candidate_not_resurrected(tmp_db):
    db.upsert_suggested_candidate("0xdead", 20.0, 0.1, 0.08, 5.0, 1e6, "why")
    db.set_candidate_status("0xdead", "rejected")
    # re-discovery must not flip it back to suggested
    assert db.upsert_suggested_candidate("0xdead", 30.0, 0.2, 0.1, 4.0, 1e6, "again") is False
    assert db.get_candidate("0xdead")["status"] == "rejected"


def test_negative_streak_bump_and_reset(tmp_db):
    db.upsert_suggested_candidate("0xs", 20.0, 0.1, 0.08, 5.0, 1e6, "why")
    db.set_candidate_status("0xs", "tracked")  # resets streak to 0
    assert db.bump_candidate_negative_streak("0xs") == 1
    assert db.bump_candidate_negative_streak("0xs") == 2
    db.reset_candidate_negative_streak("0xs")
    assert db.get_candidate("0xs")["negative_streak"] == 0


# --------------------------- discovery cycle ---------------------------
def _patch_cycle(monkeypatch, leaderboard, positions_by_addr, sent):
    monkeypatch.setattr(cy.hl, "get_leaderboard", _async(leaderboard))
    monkeypatch.setattr(cy.hl, "fetch_all_positions",
                        lambda addrs: _async({a: positions_by_addr.get(a) for a in addrs})())
    monkeypatch.setattr(cy.tg, "notify_owner",
                        lambda text: (sent.append(text), asyncio.sleep(0))[1])
    monkeypatch.setattr(cy.asyncio, "sleep", _async(None))
    # deterministic thresholds
    monkeypatch.setattr(config, "DISCOVERY_MIN_ACCOUNT_VALUE", 100_000)
    monkeypatch.setattr(config, "DISCOVERY_MIN_SMART_SCORE", 10.0)
    monkeypatch.setattr(config, "DISCOVERY_MAX_LEVERAGE", 20.0)
    monkeypatch.setattr(config, "DISCOVERY_MM_MIN_COINS", 6)
    monkeypatch.setattr(config, "DISCOVERY_MM_NET_GROSS_RATIO", 0.25)


def test_discovery_suggests_only_qualified(tmp_db, monkeypatch):
    # 50 dummy top rows (auto-tracked → excluded), then the candidates.
    dummies = [_lb_row(f"0xdummy{i:038d}", -0.01, -0.01, 5_000_000) for i in range(50)]
    good = "0x" + "a" * 40
    lucky = "0x" + "b" * 40     # week+, month- → fails multi-window
    mm = "0x" + "c" * 40        # delta-neutral book → skipped
    levered = "0x" + "d" * 40   # leverage over cap
    small = "0x" + "e" * 40     # account too small
    leaderboard = dummies + [
        _lb_row(good, 0.10, 0.08, 2_000_000),
        _lb_row(lucky, 0.10, -0.02, 2_000_000),
        _lb_row(mm, 0.10, 0.08, 2_000_000),
        _lb_row(levered, 0.10, 0.08, 1_000_000),
        _lb_row(small, 0.50, 0.50, 50_000),
    ]
    positions = {
        good: _state([("BTC", "long", 4_000_000)]),                 # 2x book
        mm: _state([(c, "long" if i % 2 == 0 else "short", 1_000_000)
                    for i, c in enumerate(["BTC", "ETH", "SOL", "AVAX", "ARB", "OP", "DOGE", "LINK"])]),
        levered: _state([("BTC", "long", 30_000_000)]),             # 30x book
    }
    sent = []
    _patch_cycle(monkeypatch, leaderboard, positions, sent)
    monkeypatch.setattr(config, "DISCOVERY_AUTO_ADD", False)

    asyncio.run(cy._discovery_cycle())

    suggested = [r["address"] for r in db.get_candidates_by_status("suggested")]
    assert suggested == [good.lower()]
    assert db.get_candidate(lucky) is None
    assert db.get_candidate(mm) is None
    assert db.get_candidate(levered) is None
    assert db.get_candidate(small) is None
    assert len(sent) == 1 and "DISCOVERY" in sent[0]


def test_discovery_excludes_already_tracked(tmp_db, monkeypatch):
    good = "0x" + "a" * 40
    db.upsert_suggested_candidate(good, 20.0, 0.1, 0.08, 2.0, 2e6, "seed")
    db.set_candidate_status(good, "tracked")  # already tracked → must be skipped
    dummies = [_lb_row(f"0xdummy{i:038d}", -0.01, -0.01, 5e6) for i in range(50)]
    leaderboard = dummies + [_lb_row(good, 0.10, 0.08, 2_000_000)]
    sent = []
    _patch_cycle(monkeypatch, leaderboard, {good: _state([("BTC", "long", 4_000_000)])}, sent)
    monkeypatch.setattr(config, "DISCOVERY_AUTO_ADD", False)

    asyncio.run(cy._discovery_cycle())
    assert db.get_candidates_by_status("suggested") == []
    assert sent == []


def test_discovery_auto_add_capped(tmp_db, monkeypatch):
    dummies = [_lb_row(f"0xdummy{i:038d}", -0.01, -0.01, 5e6) for i in range(50)]
    cands = ["0x" + ch * 40 for ch in "abcd"]
    leaderboard = dummies + [_lb_row(a, 0.30, 0.20, 2_000_000) for a in cands]
    positions = {a: _state([("BTC", "long", 4_000_000)]) for a in cands}
    sent = []
    _patch_cycle(monkeypatch, leaderboard, positions, sent)
    monkeypatch.setattr(config, "DISCOVERY_AUTO_ADD", True)
    monkeypatch.setattr(config, "DISCOVERY_AUTO_ADD_MIN_SMART", 25.0)  # smart=(0.5*100)=50 ≥ 25
    monkeypatch.setattr(config, "DISCOVERY_AUTO_ADD_MAX_PER_RUN", 2)

    asyncio.run(cy._discovery_cycle())
    tracked = db.get_tracked_candidate_addresses()
    assert len(tracked) == 2                       # capped at 2 auto-adds
    # remaining qualifiers fall back to suggestions
    assert len(db.get_candidates_by_status("suggested")) == 2


# --------------------------- retirement ---------------------------
def test_retire_after_negative_cycles(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_RETIRE_CYCLES", 2)
    addr = "0x" + "f" * 40
    db.upsert_suggested_candidate(addr, 20.0, 0.1, 0.08, 2.0, 2e6, "seed")
    db.set_candidate_status(addr, "tracked")
    sent = []
    monkeypatch.setattr(cy.tg, "notify_owner", lambda text: (sent.append(text), asyncio.sleep(0))[1])

    roi_neg = {addr: (-0.05, -0.03)}
    asyncio.run(cy._retire_stale_candidates(roi_neg))   # streak 1
    assert db.get_candidate(addr)["status"] == "tracked"
    asyncio.run(cy._retire_stale_candidates(roi_neg))   # streak 2 → retire
    assert db.get_candidate(addr)["status"] == "retired"
    assert any("retired" in s.lower() for s in sent)


def test_positive_window_resets_streak(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_RETIRE_CYCLES", 2)
    addr = "0x" + "9" * 40
    db.upsert_suggested_candidate(addr, 20.0, 0.1, 0.08, 2.0, 2e6, "seed")
    db.set_candidate_status(addr, "tracked")
    asyncio.run(cy._retire_stale_candidates({addr: (-0.05, -0.03)}))  # streak 1
    asyncio.run(cy._retire_stale_candidates({addr: (0.01, -0.03)}))   # week positive → reset
    assert db.get_candidate(addr)["negative_streak"] == 0
    assert db.get_candidate(addr)["status"] == "tracked"


def test_watchlist_never_retired(tmp_db, monkeypatch):
    # Only candidate_wallets rows are eligible for retirement; a hand-picked
    # wallet (never inserted as a candidate) is untouched by the retire pass.
    asyncio.run(cy._retire_stale_candidates({"0xhandpicked": (-0.9, -0.9)}))
    assert db.get_candidate("0xhandpicked") is None


# --------------------------- operator commands ---------------------------
class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, chat_id):
        self.effective_chat = types.SimpleNamespace(id=chat_id)
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []
        self.job_queue = None


def test_track_promotes_candidate(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    addr = "0x" + "a" * 40
    db.upsert_suggested_candidate(addr, 20.0, 0.1, 0.08, 2.0, 2e6, "why")
    upd, ctx = FakeUpdate(777), FakeContext(args=[addr])
    asyncio.run(h.track_cmd(upd, ctx))
    assert db.get_candidate(addr)["status"] == "tracked"
    assert any("tracking" in r.lower() for r in upd.message.replies)


def test_track_rejects_non_owner(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    addr = "0x" + "a" * 40
    db.upsert_suggested_candidate(addr, 20.0, 0.1, 0.08, 2.0, 2e6, "why")
    upd, ctx = FakeUpdate(123), FakeContext(args=[addr])   # not the owner
    asyncio.run(h.track_cmd(upd, ctx))
    assert db.get_candidate(addr)["status"] == "suggested"
    assert any("operator" in r.lower() for r in upd.message.replies)


def test_track_unknown_address(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    upd, ctx = FakeUpdate(777), FakeContext(args=["0x" + "0" * 40])
    asyncio.run(h.track_cmd(upd, ctx))
    assert any("isn't a discovery candidate" in r for r in upd.message.replies)


def test_candidates_lists_suggestions(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    db.upsert_suggested_candidate("0x" + "a" * 40, 20.0, 0.1, 0.08, 2.0, 2e6, "why")
    upd, ctx = FakeUpdate(777), FakeContext()
    asyncio.run(h.candidates_cmd(upd, ctx))
    assert any("Discovery suggestions" in r for r in upd.message.replies)

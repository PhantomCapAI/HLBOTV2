"""Tests for the discovery overhaul: raw-suggestion digest, proven-candidate
promotion, deep paged leaderboard sweep, and the [✅ Track] button.

Run: pytest tests/test_discovery_proven.py
"""
import types
import asyncio
from datetime import datetime, timedelta

import pytest

import config
from storage import database as db
from services import cycles as cy
from bot import formatting_wallet as fw


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


def _async(value=None):
    async def _coro(*a, **k):
        return value
    return _coro


def _lb_row(address, week_roi, month_roi, account_value):
    return {
        "ethAddress": address,
        "accountValue": str(account_value),
        "windowPerformances": [
            ["day", {"pnl": "1", "roi": "0.001"}],
            ["week", {"pnl": "1", "roi": str(week_roi)}],
            ["month", {"pnl": "1", "roi": str(month_roi)}],
        ],
    }


def _state(positions):
    aps = []
    for coin, side, notional in positions:
        size = notional / 100.0
        szi = size if side == "long" else -size
        aps.append({"position": {
            "coin": coin, "szi": str(szi), "entryPx": "100",
            "liquidationPx": "0", "unrealizedPnl": "0",
        }})
    return {"assetPositions": aps}


# --------------------------- evaluate_proven (pure) ---------------------------
def _obs(days_ago, week=0.1, month=0.08, lev=3.0, smart=20.0):
    return {
        "observed_at": (datetime.utcnow() - timedelta(days=days_ago)).isoformat(),
        "week_roi": week, "month_roi": month, "leverage": lev, "smart_score": smart,
    }


def test_proven_needs_enough_cycles():
    obs = [_obs(4), _obs(0)]                       # only 2 observations
    ok, _ = cy.evaluate_proven(obs, min_cycles=3, min_days=3, max_leverage=20)
    assert ok is False


def test_proven_needs_enough_days():
    obs = [_obs(0.1), _obs(0.05), _obs(0)]         # 3 obs, ~all same day
    ok, _ = cy.evaluate_proven(obs, min_cycles=3, min_days=3, max_leverage=20)
    assert ok is False


def test_proven_disqualified_by_one_bad_week():
    obs = [_obs(4, week=0.1), _obs(2, week=-0.01), _obs(0, week=0.2)]
    ok, _ = cy.evaluate_proven(obs, min_cycles=3, min_days=3, max_leverage=20)
    assert ok is False                            # dipped negative once → not proven


def test_proven_disqualified_by_over_leverage():
    obs = [_obs(4, lev=3), _obs(2, lev=99), _obs(0, lev=4)]
    ok, _ = cy.evaluate_proven(obs, min_cycles=3, min_days=3, max_leverage=20)
    assert ok is False


def test_proven_happy_path_stats():
    obs = [_obs(4, week=0.10, month=0.05, smart=18),
           _obs(2, week=0.15, month=0.09, smart=22),
           _obs(0, week=0.20, month=0.12, smart=30)]
    ok, stats = cy.evaluate_proven(obs, min_cycles=3, min_days=3, max_leverage=20)
    assert ok is True
    assert stats["times_seen"] == 3
    assert stats["span_days"] >= 3
    assert stats["smart_last"] == 30
    assert stats["week_roi_min"] == 0.10 and stats["week_roi_max"] == 0.20
    assert stats["consistency_pct"] == 100.0


# --------------------------- promotion ping (idempotent) ---------------------------
def _seed_observations(addr, count, span_days, week=0.1, month=0.08, lev=3.0, smart=20.0):
    with db.get_conn() as conn:
        for i in range(count):
            hours_ago = int((span_days - i * (span_days / max(count - 1, 1))) * 24)
            conn.execute(
                """INSERT INTO candidate_observations
                   (address, smart_score, week_roi, month_roi, leverage, account_value, observed_at)
                   VALUES (?,?,?,?,?,?, datetime('now', ?))""",
                (addr.lower(), smart, week, month, lev, 2e6, f"-{hours_ago} hours"),
            )


def test_promotion_fires_once_with_track_button(tmp_db, monkeypatch):
    addr = "0x" + "a" * 40
    db.upsert_suggested_candidate(addr, 20.0, 0.1, 0.08, 3.0, 2e6, "why")
    _seed_observations(addr, count=3, span_days=4)
    calls = []

    async def fake_notify(text, reply_markup=None):
        calls.append((text, reply_markup))
        return True

    monkeypatch.setattr(cy.tg, "notify_owner", fake_notify)
    monkeypatch.setattr(cy.asyncio, "sleep", _async(None))
    monkeypatch.setattr(config, "DISCOVERY_PROVEN_MIN_CYCLES", 3)
    monkeypatch.setattr(config, "DISCOVERY_PROVEN_MIN_DAYS", 3.0)

    promoted = asyncio.run(cy._maybe_promote_proven(addr, "SolarTuna-aaaa"))
    assert promoted is True
    assert db.get_candidate(addr)["status"] == "proven"
    assert len(calls) == 1
    text, markup = calls[0]
    assert "PROVEN" in text
    assert markup is not None                      # [✅ Track] button attached
    btn = markup.inline_keyboard[0][0]
    assert btn.callback_data == f"track:{addr}"

    # Idempotent: an already-proven candidate does not re-ping.
    again = asyncio.run(cy._maybe_promote_proven(addr, "SolarTuna-aaaa"))
    assert again is False
    assert len(calls) == 1


# --------------------------- raw digest (consolidated, optional) ---------------------------
def _patch_cycle(monkeypatch, leaderboard, positions_by_addr, sent):
    monkeypatch.setattr(cy.hl, "get_leaderboard", _async(leaderboard))

    async def fake_fetch(addrs):
        return {a: positions_by_addr.get(a) for a in addrs}
    monkeypatch.setattr(cy.hl, "fetch_all_positions", fake_fetch)

    async def fake_notify(text, reply_markup=None):
        sent.append((text, reply_markup))
        return True
    monkeypatch.setattr(cy.tg, "notify_owner", fake_notify)
    monkeypatch.setattr(cy.asyncio, "sleep", _async(None))
    monkeypatch.setattr(config, "DISCOVERY_MIN_ACCOUNT_VALUE", 100_000)
    monkeypatch.setattr(config, "DISCOVERY_MIN_SMART_SCORE", 10.0)
    monkeypatch.setattr(config, "DISCOVERY_MAX_LEVERAGE", 20.0)
    monkeypatch.setattr(config, "DISCOVERY_MM_MIN_COINS", 6)
    monkeypatch.setattr(config, "DISCOVERY_MM_NET_GROSS_RATIO", 0.25)
    monkeypatch.setattr(config, "DISCOVERY_AUTO_ADD", False)


def test_raw_digest_is_one_message_ranked_capped(tmp_db, monkeypatch):
    dummies = [_lb_row(f"0xdummy{i:038d}", -0.01, -0.01, 5e6) for i in range(50)]
    cands = ["0x" + ch * 40 for ch in "abcd"]
    # Different week ROI → different smart score, to assert ranking.
    rois = {cands[0]: 0.10, cands[1]: 0.30, cands[2]: 0.20, cands[3]: 0.05}
    leaderboard = dummies + [_lb_row(a, rois[a], 0.05, 2_000_000) for a in cands]
    positions = {a: _state([("BTC", "long", 4_000_000)]) for a in cands}
    sent = []
    _patch_cycle(monkeypatch, leaderboard, positions, sent)
    monkeypatch.setattr(config, "DISCOVERY_RAW_DIGEST_ENABLED", True)
    monkeypatch.setattr(config, "DISCOVERY_DIGEST_MAX", 3)

    asyncio.run(cy._discovery_cycle())
    assert len(sent) == 1                          # ONE consolidated digest
    text, _ = sent[0]
    assert "DISCOVERY" in text
    # Capped at 3: the weakest (cands[3], +0.05) is dropped.
    assert cands[3] not in text
    assert cands[1] in text and cands[2] in text and cands[0] in text
    # Ranked by smart: strongest (cands[1], +0.30) appears before cands[0] (+0.10).
    assert text.index(cands[1]) < text.index(cands[0])


def test_raw_digest_toggle_off_suppresses(tmp_db, monkeypatch):
    dummies = [_lb_row(f"0xdummy{i:038d}", -0.01, -0.01, 5e6) for i in range(50)]
    good = "0x" + "a" * 40
    leaderboard = dummies + [_lb_row(good, 0.10, 0.08, 2_000_000)]
    sent = []
    _patch_cycle(monkeypatch, leaderboard, {good: _state([("BTC", "long", 4_000_000)])}, sent)
    monkeypatch.setattr(config, "DISCOVERY_RAW_DIGEST_ENABLED", False)

    asyncio.run(cy._discovery_cycle())
    assert sent == []                              # feed off → no raw ping
    # …but the candidate is still recorded for /candidates + /track.
    assert [r["address"] for r in db.get_candidates_by_status("suggested")] == [good]
    assert len(db.get_candidate_observations(good)) == 1


# --------------------------- deep paged sweep ---------------------------
def test_deep_sweep_is_paged_across_cycles(tmp_db, monkeypatch):
    # 6 qualifying wallets, page size 2 → only 2 fetched per cycle, cursor advances.
    cands = ["0x" + f"{i:040x}" for i in range(6)]
    leaderboard = [_lb_row(a, 0.10, 0.08, 2_000_000) for a in cands]
    positions = {a: _state([("BTC", "long", 4_000_000)]) for a in cands}

    fetched_batches = []

    async def fake_fetch(addrs):
        fetched_batches.append(list(addrs))
        return {a: positions.get(a) for a in addrs}

    monkeypatch.setattr(cy.hl, "get_leaderboard", _async(leaderboard))
    monkeypatch.setattr(cy.hl, "fetch_all_positions", fake_fetch)
    monkeypatch.setattr(cy.tg, "notify_owner", _async(True))
    monkeypatch.setattr(cy.asyncio, "sleep", _async(None))
    # Isolate paging: don't let the top-50 auto-track exclusion swallow the small
    # synthetic leaderboard (with 6 rows, all 6 would count as "top 50").
    monkeypatch.setattr(cy, "_discovery_excluded_addresses", lambda lb: set())
    monkeypatch.setattr(config, "DISCOVERY_MIN_ACCOUNT_VALUE", 100_000)
    monkeypatch.setattr(config, "DISCOVERY_MIN_SMART_SCORE", 10.0)
    monkeypatch.setattr(config, "DISCOVERY_MAX_LEVERAGE", 20.0)
    monkeypatch.setattr(config, "DISCOVERY_AUTO_ADD", False)
    monkeypatch.setattr(config, "DISCOVERY_SCAN_TOP_N", 6)
    monkeypatch.setattr(config, "DISCOVERY_SCAN_PAGE_SIZE", 2)

    asyncio.run(cy._discovery_cycle())
    assert fetched_batches[-1] == cands[0:2]       # page 1
    assert db.get_state("discovery_scan_cursor") == "2"
    asyncio.run(cy._discovery_cycle())
    assert fetched_batches[-1] == cands[2:4]       # page 2
    asyncio.run(cy._discovery_cycle())
    assert fetched_batches[-1] == cands[4:6]       # page 3
    assert db.get_state("discovery_scan_cursor") == "0"  # wrapped
    asyncio.run(cy._discovery_cycle())
    assert fetched_batches[-1] == cands[0:2]       # back to page 1
    assert all(len(b) <= 2 for b in fetched_batches)  # never exceeds page size


# --------------------------- [✅ Track] button callback ---------------------------
class _FakeCbQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id))
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


class _FakeCbUpdate:
    def __init__(self, data, chat_id):
        self.callback_query = _FakeCbQuery(data, chat_id)
        self.effective_chat = types.SimpleNamespace(id=chat_id)


def test_track_button_promotes(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    addr = "0x" + "a" * 40
    db.upsert_suggested_candidate(addr, 20.0, 0.1, 0.08, 3.0, 2e6, "why")
    db.set_candidate_status(addr, "proven")
    upd = _FakeCbUpdate(f"track:{addr}", 777)
    asyncio.run(h.track_callback(upd, types.SimpleNamespace(args=[])))
    assert db.get_candidate(addr)["status"] == "tracked"
    assert any("tracking" in e.lower() for e in upd.callback_query.edits)


def test_track_button_rejects_non_owner(tmp_db, monkeypatch):
    import bot.handlers as h
    monkeypatch.setattr(config, "OWNER_CHAT_ID", 777)
    addr = "0x" + "a" * 40
    db.upsert_suggested_candidate(addr, 20.0, 0.1, 0.08, 3.0, 2e6, "why")
    upd = _FakeCbUpdate(f"track:{addr}", 123)      # not the owner
    asyncio.run(h.track_callback(upd, types.SimpleNamespace(args=[])))
    assert db.get_candidate(addr)["status"] == "suggested"

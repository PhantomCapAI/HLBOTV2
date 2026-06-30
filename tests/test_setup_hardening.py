"""Pre-public hardening tests for the setup-card path.

  * M5 — a neutral-lean (direction "none") coin must produce NO setup, never a
    coerced short.
  * M4 — a Grok setup whose levels are insane (entry far from mark, or direction
    opposite the deterministic lean) must fall back to the local deterministic
    levels rather than be shown.
  * Sizing — setup cards show a conviction-scaled risk % (HIGH 3 / MED 2 / LOW 1)
    and no operator-specific dollar size or unit count.

Run: pytest tests/test_setup_hardening.py
"""
import asyncio

import pytest

import config
import integrations.grok as grok
from bot.formatting import format_setup, _risk_pct_for


def _discovery(direction="long", entry=100.0):
    return {
        "coin": "BTC", "direction": direction, "score": 80,
        "entry": entry, "stop": 95.0, "targets": [110.0, 115.0, 120.0],
        "atr": 2.0, "micro": {"mark": 100.0},
        "regime_4h": "trend up", "adx_4h": 30.0, "structure_4h": "uptrend",
    }


# --------------------------- M5: neutral coins skipped ---------------------------
def test_neutral_direction_produces_no_setup(monkeypatch):
    monkeypatch.setattr(config, "GROK_API_KEY", "")   # deterministic fallback path
    out = asyncio.run(grok.generate_setups([_discovery(direction="none")]))
    assert out == []


def test_long_direction_still_produces_setup(monkeypatch):
    monkeypatch.setattr(config, "GROK_API_KEY", "")
    out = asyncio.run(grok.generate_setups([_discovery(direction="long")]))
    assert len(out) == 1
    assert out[0]["setups"][0]["direction"] == "long"


# --------------------------- M4: validate LLM levels ---------------------------
def test_grok_hallucinated_entry_falls_back(monkeypatch):
    monkeypatch.setattr(config, "GROK_API_KEY", "x")
    bad = {"coin": "BTC", "setups": [{
        "direction": "long", "entry": 99999.0, "stop": 1.0,
        "targets": [1.0, 2.0, 3.0], "confidence": "high",
        "rationale": "r", "invalidation": "i"}]}

    async def fake_call(session, prompt):
        return bad
    monkeypatch.setattr(grok, "_call_grok", fake_call)

    out = asyncio.run(grok.generate_setups([_discovery(direction="long", entry=100.0)]))
    assert len(out) == 1
    inner = out[0]["setups"][0]
    # Deterministic entry (100), not the hallucinated 99999.
    assert abs(float(inner["entry"]) - 100.0) < 1e-6


def test_grok_opposite_direction_falls_back(monkeypatch):
    monkeypatch.setattr(config, "GROK_API_KEY", "x")
    bad = {"coin": "BTC", "setups": [{
        "direction": "short", "entry": 100.0, "stop": 105.0,
        "targets": [90.0, 85.0, 80.0], "confidence": "high",
        "rationale": "r", "invalidation": "i"}]}

    async def fake_call(session, prompt):
        return bad
    monkeypatch.setattr(grok, "_call_grok", fake_call)

    out = asyncio.run(grok.generate_setups([_discovery(direction="long", entry=100.0)]))
    assert len(out) == 1
    # Deterministic direction wins over the contradicting LLM payload.
    assert out[0]["setups"][0]["direction"] == "long"


def test_grok_sane_setup_is_kept(monkeypatch):
    monkeypatch.setattr(config, "GROK_API_KEY", "x")
    good = {"coin": "BTC", "setups": [{
        "direction": "long", "entry": 100.5, "stop": 95.0,
        "targets": [110.0, 115.0, 120.0], "confidence": "med",
        "rationale": "sane", "invalidation": "i"}]}

    async def fake_call(session, prompt):
        return good
    monkeypatch.setattr(grok, "_call_grok", fake_call)

    out = asyncio.run(grok.generate_setups([_discovery(direction="long", entry=100.0)]))
    assert out[0]["setups"][0]["rationale"] == "sane"   # LLM kept when within bounds


# --------------------------- Sizing: conviction-scaled risk % ---------------------------
def test_risk_pct_mapping():
    assert _risk_pct_for("HIGH") == 3
    assert _risk_pct_for("MED") == 2
    assert _risk_pct_for("LOW") == 1
    assert _risk_pct_for("anything-else") == 1


def test_setup_card_shows_risk_not_dollars():
    s = {"coin": "BTC", "score": 80, "setups": [{
        "direction": "long", "confidence": "high", "entry": 100.0, "stop": 95.0,
        "targets": [110.0, 115.0, 120.0], "leverage_set": 5,
        "rationale": "r", "invalidation": "i"}]}
    msg = format_setup(s)
    assert "Risk: 3%" in msg
    assert "Size" not in msg        # no operator-equity dollar size
    assert "units" not in msg       # no unit count
    assert "Leverage: 5x" in msg

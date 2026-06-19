"""Grok (xAI) trade-setup generator — async port of the 5-file grok_layer.py.

Changes from the original: uses aiohttp instead of blocking requests, no stdout
dumps of raw responses, key/model/timeout come from config. Keeps the local
deterministic fallback when the API key is absent or the call fails.
"""
import json
import logging
from typing import Any

import aiohttp

import config

log = logging.getLogger(__name__)
XAI_URL = "https://api.x.ai/v1/chat/completions"


def _extract_mark(d: dict[str, Any]) -> float:
    for c in (d.get("micro", {}).get("mark"), d.get("mark"), d.get("markPx"),
              d.get("entry"), d.get("close")):
        try:
            if c is not None:
                val = float(c)
                if val > 0:
                    return val
        except (ValueError, TypeError):
            continue
    return 0.0


def _fallback_setup(coin, direction, entry, stop, targets, score, d) -> dict:
    return {
        "coin": coin,
        "score": score,
        "lean_disclosure": f"I lean {direction} on {coin} (score {score})",
        "setups": [{
            "direction": direction,
            "entry": round(entry, 6),
            "stop": round(stop, 6),
            "targets": [round(t, 6) for t in targets],
            "leverage_set": d.get("leverage_set", 5),
            "risk_pct_at_leverage": 1.0,
            "confidence": "med" if score > 45 else "low",
            "rationale": (
                f"{d.get('regime_4h', 'Trend')} regime | ADX {d.get('adx_4h', 0)} | "
                f"Funding {float(d.get('funding', 0) or 0) * 100:.4f}%/hr. "
                f"{d.get('structure_4h', 'Clean structure')} with good alignment on higher timeframes."
            ),
            "invalidation": "Break of recent swing structure or mark invalidation.",
        }],
    }


def _build_prompt(coin, context, direction, entry, stop, targets, score) -> str:
    return f"""You are Grok, built by xAI. You seek truth, avoid hype, and think from first principles.

Generate ONE high-conviction Hyperliquid perps setup for {coin}.

Market Context:
{json.dumps(context, indent=2)}

Rationale Guidelines (use your training):
- Be concise, insightful, and honest (1-2 sentences)
- Reference regime, ADX strength, funding pressure, structure, volume/OI
- Mention confluence, potential risks, or second-order effects
- Avoid slop - be decisive and truth-seeking

Return ONLY valid JSON:
{{
  "coin": "{coin}",
  "score": {score},
  "lean_disclosure": "short professional lean statement",
  "setups": [{{
    "direction": "{direction}",
    "entry": {entry},
    "stop": {stop},
    "targets": {targets},
    "leverage_set": 5,
    "risk_pct_at_leverage": 1.0,
    "confidence": "high" | "med" | "low",
    "rationale": "rich insightful 1-2 sentence rationale",
    "invalidation": "clear invalidation level or condition"
  }}]
}}"""


async def _call_grok(session: aiohttp.ClientSession, prompt: str) -> dict:
    headers = {"Authorization": f"Bearer {config.GROK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": config.GROK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 700,
    }
    async with session.post(XAI_URL, json=payload, headers=headers) as resp:
        resp.raise_for_status()
        data = await resp.json()
    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("```")[1].replace("json", "").strip()
    return json.loads(content)


async def generate_setups(discoveries: list[dict]) -> list[dict]:
    results: list[dict] = []
    timeout = aiohttp.ClientTimeout(total=config.GROK_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for d in discoveries:
            coin = d.get("coin", "UNKNOWN")
            mark = _extract_mark(d)
            direction = d.get("direction", "long").lower()
            side = 1 if direction == "long" else -1

            entry = float(d.get("entry") or mark)
            stop = d.get("stop")
            targets = d.get("targets")
            if not stop or not targets or float(stop) == 0:
                atr = float(d.get("atr", mark * 0.025) or mark * 0.025)
                stop_dist = atr * 1.5
                stop = round(entry - side * stop_dist, 6)
                targets = [round(entry + side * m * stop_dist, 6) for m in (2.0, 3.0, 4.0)]
            entry, stop = float(entry), float(stop)
            targets = [float(t) for t in targets]
            if entry == 0:
                continue

            score = d.get("score", 0)
            context = {
                "coin": coin, "mark_price": mark, "entry": entry, "stop": stop,
                "targets": targets, "score": score, "direction": direction,
                "4h_regime": d.get("regime_4h"), "adx": d.get("adx_4h"),
                "funding": d.get("funding"), "oi_usd": d.get("oi_usd"),
                "structure": d.get("structure_4h"), "notes": d.get("notes_4h"),
            }

            setup = None
            if config.GROK_API_KEY:
                try:
                    prompt = _build_prompt(coin, context, direction, entry, stop, targets, score)
                    setup = await _call_grok(session, prompt)
                    setup["score"] = score
                except Exception as e:
                    log.warning("Grok fallback for %s: %s", coin, e)
            if setup is None:
                setup = _fallback_setup(coin, direction, entry, stop, targets, score, d)
            results.append(setup)
    return results

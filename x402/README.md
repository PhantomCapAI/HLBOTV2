# x402 `whale-check` — Bankr Cloud endpoint

A **new, agent-facing** product: sells ONE point-in-time whale/wallet-health
reading per call, priced in USDC on Base. It is **separate from the Telegram
subscription** (that stays the human push-stream product) and **never touches
the live alert/push stream** — it only reads already-persisted snapshots.

This directory holds everything that can live in the repo. The actual
**login + deploy + live test must be run from your own machine** — see
[Blockers](#blockers-you-must-clear) below.

---

## Wiring decision — which of the two options, and why

The prompt offered two wiring options. Here's what the code actually allows:

| Option | Verdict |
| --- | --- |
| **2. Import the scoring logic as a module and call it directly** | ❌ **Impossible.** The scoring logic is **Python** (`trackers/`, `services/`, `scanner/`, reading a SQLite DB). The Bankr handler is **TypeScript/bun**. You can't `bun add` a Python module. Rejected on language grounds. |
| **1. Handler `fetch`es an internal HTTP endpoint** | ✅ **Chosen — but HLBOTV2 exposed no HTTP endpoint, so I built a minimal read-only one.** |

**What I built:** `services/whale_check_api.py` — a small `aiohttp` route that runs
**inside** the existing bot process (so it shares the SQLite volume the scanner
populates), reads snapshots, and returns one reading. It is **off by default**
and starts only when `WHALE_CHECK_API_ENABLED=true` **and** `WHALE_CHECK_API_KEY`
is set. Merging this code changes nothing about the running bot until you opt in.
The Bankr TS handler (`x402/whale-check.ts`) `fetch`es it with an internal key.

Data flow:

```
agent → Bankr x402 Cloud (hosted) → x402/whale-check.ts
      → GET https://<zeabur-host>/whale-check?target=…&chain=base  (X-Internal-Key)
      → services/whale_check_api.py  → reads SQLite snapshots → ONE reading
```

The handler computes **one reading** and throws on any non-200 (settle-after-
response: the caller is billed only on a clean reading — never for a stub).

---

## Score mapping (what the numbers mean)

Computed from the **existing** persisted scores — nothing is recomputed or
re-fetched per call in this MVP.

**Wallet target** (`0x…` 40-hex):
- `healthScore` — `wallet_health_score` (0–100, current-state gauge).
- `whaleCount` — tracked whales currently on the same coin+side as the wallet's
  largest position (its "company").
- `confluence` — combined `smart_score` (skill) of that group.
- `netFlow` — the wallet's `exposure_total` trend across its two latest
  snapshots (rising ≥5% → `accumulating`, falling ≥5% → `distributing`, else
  `neutral`).

**Coin/market target** (e.g. `HYPE`):
- dominant side = the side more tracked whales sit on right now.
- `whaleCount` — whales on that side; `confluence` — their combined `smart_score`.
- `healthScore` — notional-weighted average health of those whales.
- `netFlow` — dominant side + recent OI trend (`oi_snapshots`).

All thresholds are tunable constants at the top of `services/whale_check_api.py`
(`_MIN_WHALE_NOTIONAL`, `_CONFLUENCE_WINDOW_MIN`, `_NETFLOW_*`).

**Verified locally** against a seeded SQLite DB: real numbers come back, and
unknown/stale/missing/unauthorized targets return 401/404/400 (→ handler throws
→ caller not billed). It has **not** been run against the live Zeabur DB.

---

## ⚠️ Two caveats to decide on

1. **Coverage — cached vs live.** This MVP only has data for wallets/coins the
   scanner already tracks. An **untracked** target returns 404 (caller not
   billed) — fine for correctness, limited for a paid product. Upgrade path:
   have the endpoint **live-fetch from Hyperliquid and compute on demand** for
   any wallet/coin. That's the real product but more work, and it adds per-call
   marginal cost (HL API + optional Grok) — re-check the price if you do it.

2. **Chain mismatch.** The engine is **Hyperliquid-native** (perps wallets +
   HL coin symbols). The endpoint's `chain=base` / "token contract" framing
   doesn't map to a Base ERC-20 — there is no Base-token scoring here today.
   `chain` is currently echoed through. Decide whether the product is
   "Hyperliquid whale intel" (rename/clarify inputs) or whether you actually
   want Base-token coverage (a different data source entirely).

---

## Price — placeholder `$0.05`/call (TUNE)

- **Above marginal cost:** the cached-snapshot design does no per-call RPC or
  inference (it reads SQLite), so marginal cost ≈ Bankr's fee only. Any positive
  price clears it; `$0.05` is comfortably above.
- **Doesn't cannibalize the sub:** replicating the push stream by polling is far
  more expensive than the sub — e.g. polling every 3 min for a week ≈ 3,360
  calls ≈ **$168** vs the **$10** week sub. A one-off check stays cheap ($0.05).
- **Free tier:** first 1,000 req/month at 0% platform fee, 5% after (Bankr's
  cut, not the caller's price — confirm current numbers in the docs).
- If you move to live-fetch-any-wallet, **re-derive the floor** against the new
  per-call RPC/inference cost.

---

## Env vars you must set

**HLBOTV2 side** (Zeabur / Terminal Settings — never in chat, never committed):

| Var | Required | Notes |
| --- | --- | --- |
| `WHALE_CHECK_API_ENABLED` | to turn it on | `true` to start the endpoint |
| `WHALE_CHECK_API_KEY` | ✅ when enabled | long random shared secret |
| `WHALE_CHECK_API_HOST` | – | default `0.0.0.0` |
| `WHALE_CHECK_API_PORT` | – | default `8402` |
| `WHALE_CHECK_FRESHNESS_MINUTES` | – | default `30` |

**Bankr side** (`bankr x402 env set …` — set VALUES via CLI, never hardcode):

| Var | Notes |
| --- | --- |
| `WHALE_CHECK_URL` | public HTTPS URL of the endpoint, e.g. `https://<zeabur-host>/whale-check` |
| `WHALE_CHECK_INTERNAL_KEY` | **must equal** `WHALE_CHECK_API_KEY` on the bot side |

> **Reachability:** Bankr Cloud is hosted, so it must reach the endpoint over the
> public internet. The `whale-check` route must have a **public HTTPS ingress on
> Zeabur**, protected by the key (and ideally an IP allowlist). "Internal" here
> means auth-gated, not network-private. Exposing this route is a **change to the
> Zeabur service** — your call to make.

---

## Blockers you must clear (this session could not)

1. **`bankr.bot` is blocked by this environment's egress policy (403).** I could
   not read the full docs (only public search summaries), and **cannot run
   `bankr login`, `deploy`, `call`, or `schema` from here.** The whole Bankr
   half must run **from your machine**, where you can reach `bankr.bot`.
2. **`bankr login` is interactive and yours to complete** regardless.
3. **Enabling the endpoint on Zeabur** (public ingress + env vars) is a change to
   the running service — the prompt said don't deploy to Zeabur without you.

## Runbook (do this on your machine)

```sh
bun add -g @bankr/cli
bankr login                       # interactive — generates the Bankr wallet + bk_ key
bankr x402 init
bankr x402 add whale-check        # generates the real handler + config

# 1) paste the body logic from x402/whale-check.ts into the generated handler;
#    reconcile the signature (esp. ctx / ctx.askAgent) with the docs.
# 2) transcribe values from x402/bankr.x402.reference.json into the generated
#    config (name=whale-check, method=GET, price=0.05, input/output schema, env).
bankr x402 env set WHALE_CHECK_URL=https://<zeabur-host>/whale-check
bankr x402 env set WHALE_CHECK_INTERNAL_KEY=<same-as-WHALE_CHECK_API_KEY>

bankr x402 deploy

# On HLBOTV2 (Zeabur): set WHALE_CHECK_API_ENABLED=true, WHALE_CHECK_API_KEY=<secret>,
# expose the whale-check route over HTTPS, redeploy.

# Verify live (per CLI reference):
bankr x402 schema https://x402.bankr.bot/<wallet>/whale-check
bankr x402 call   https://x402.bankr.bot/<wallet>/whale-check -i    # try a known tracked wallet/coin
```

Confirm the returned numbers are real (not zeros), then note the live URL
`https://x402.bankr.bot/<wallet>/whale-check`, the env vars set, and the final
price.

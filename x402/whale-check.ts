/**
 * Bankr x402 Cloud handler — `whale-check`
 * ----------------------------------------
 * Sells ONE point-in-time whale/wallet-health reading per call, priced in USDC
 * on Base. This is the agent-facing product; it is separate from the Telegram
 * subscription and never touches the live alert/push stream.
 *
 * ⚠️ FORMAT NOTE: `bankr x402 add whale-check` GENERATES the real handler +
 * config scaffold. This environment's network policy blocks docs.bankr.bot and
 * the Bankr API (egress 403), so this file could NOT be reconciled against the
 * generated scaffold or the full docs. Treat it as the *body logic* to paste
 * into the generated handler, and verify the signature (esp. the `ctx` arg and
 * `ctx.askAgent`), the config filename/fields, and the schema format against:
 *   https://docs.bankr.bot/x402-cloud/quick-start
 *   https://docs.bankr.bot/x402-cloud/config-file
 *   https://docs.bankr.bot/x402-cloud/security
 * If any of those conflict with this file, the docs win.
 *
 * Settle-after-response: Bankr only bills the caller if this handler RETURNS
 * successfully. So on bad input or any upstream failure we THROW — never return
 * zeroed/placeholder data the caller would still pay for.
 *
 * Env vars this handler needs (set via `bankr x402 env set …`, never hardcoded):
 *   WHALE_CHECK_URL         Base URL of the HLBOTV2 internal endpoint, e.g.
 *                           https://<your-zeabur-host>/whale-check
 *   WHALE_CHECK_INTERNAL_KEY  Shared secret; MUST equal WHALE_CHECK_API_KEY set
 *                             on the HLBOTV2 side.
 */

// NOTE: `ctx` is optional and its exact shape must be confirmed against the
// docs. `ctx.askAgent` (the optional Telegram ping) is used defensively.
export default async function handler(req: Request, ctx?: any): Promise<unknown> {
  const url = new URL(req.url);
  const target = (url.searchParams.get("target") ?? "").trim();
  const chain = (url.searchParams.get("chain") ?? "base").trim().toLowerCase();

  // Bad input -> throw so the caller is NOT billed.
  if (!target) {
    throw new Error("`target` is required (a wallet address or coin/market symbol).");
  }

  const base = process.env.WHALE_CHECK_URL;
  const key = process.env.WHALE_CHECK_INTERNAL_KEY;
  if (!base || !key) {
    // Misconfiguration is our fault, not the caller's -> throw (no bill).
    throw new Error("Endpoint not configured: set WHALE_CHECK_URL and WHALE_CHECK_INTERNAL_KEY.");
  }

  const qs = new URLSearchParams({ target, chain });
  let res: Response;
  try {
    res = await fetch(`${base}?${qs}`, {
      method: "GET",
      headers: { "X-Internal-Key": key },
    });
  } catch (e) {
    throw new Error(`Upstream whale-check unreachable: ${(e as Error).message}`);
  }

  // Upstream 4xx/5xx (unknown target, stale data, error) -> throw. Settle-after-
  // response means the caller pays only on a clean 200 with a real reading.
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json())?.error ?? "";
    } catch {
      /* ignore parse failure */
    }
    throw new Error(
      `No reading for '${target}'${detail ? `: ${detail}` : ""} (upstream ${res.status}).`,
    );
  }

  const data = (await res.json()) as {
    healthScore?: number;
    confluence?: number;
    whaleCount?: number;
    netFlow?: string;
    timestamp?: string;
  };

  // Defensive: if the upstream somehow returned 200 without the core field,
  // treat it as no-data and throw rather than bill for an empty reading.
  if (typeof data.healthScore !== "number") {
    throw new Error(`No reading for '${target}' (empty upstream payload).`);
  }

  // Optional: fire a Telegram note to the operator on each PAID call. Kept
  // best-effort and non-blocking so it can never fail the paid response. If the
  // real `ctx.askAgent` API differs, delete this block — it is not required.
  try {
    await ctx?.askAgent?.(
      `💸 x402 whale-check paid — target=${target} chain=${chain} ` +
        `health=${data.healthScore} whales=${data.whaleCount} flow=${data.netFlow}`,
    );
  } catch {
    /* never let the notification affect the billable response */
  }

  return {
    target,
    chain,
    healthScore: data.healthScore,
    confluence: data.confluence,
    whaleCount: data.whaleCount,
    netFlow: data.netFlow, // accumulating | distributing | neutral
    timestamp: data.timestamp ?? new Date().toISOString(),
  };
}

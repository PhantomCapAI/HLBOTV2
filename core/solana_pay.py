"""On-chain USDC payment verification on Solana (fail-CLOSED).

Reimplements the check that phantom-x402-gateway performs in its
``verify_payment(tx_signature, expected_usd)`` so this bot does NOT depend on
the gateway being up. The gateway:

  * queried Solana RPC ``getTransaction`` (jsonParsed encoding,
    maxSupportedTransactionVersion=0),
  * rejected transactions whose ``meta.err`` was set (failed tx),
  * rejected transactions older than its PAYMENT_WINDOW (blockTime age),
  * scanned parsed (and inner) instructions for an SPL ``transfer`` /
    ``transferChecked`` whose amount (USDC = 6 decimals) met the expected
    base-unit amount, requiring the USDC mint for ``transferChecked``,
  * and confirmed the recipient only by checking that the treasury *wallet*
    pubkey appeared anywhere in ``accountKeys``.

That last recipient check is loose (an SPL transfer's ``destination`` is a token
account, not the wallet, and a plain ``transfer`` carries no mint), so for a
guard that protects money we verify recipient + mint + amount the robust way:
via ``meta.pre/postTokenBalances`` — the USDC balance *owned by our receiving
address* must increase by at least the price. Anything uncertain → ok=False.

This module guards money, so it FAILS CLOSED: any error, timeout, missing
field, parse ambiguity, or unmet condition returns ``{"ok": False, ...}``.
"""
from __future__ import annotations

import logging
import time

import aiohttp

import config

log = logging.getLogger(__name__)

# Solana mainnet USDC. Always 6 decimals. (Public constant, not a secret.)
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# Base58 alphabet (Bitcoin/Solana variant) — no 0, O, I, l.
_B58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

_RPC_TIMEOUT_SECONDS = 15.0


def _looks_like_signature(sig: str) -> bool:
    """Cheap shape check for a Solana tx signature (base58 of a 64-byte sig)."""
    if not isinstance(sig, str):
        return False
    sig = sig.strip()
    # 64 bytes base58-encode to 87-88 chars; allow a little slack, reject junk.
    if not (80 <= len(sig) <= 90):
        return False
    return all(c in _B58_ALPHABET for c in sig)


def _fail(reason: str) -> dict:
    return {"ok": False, "reason": reason}


def _usdc_owned_balance(entries, receiving_address: str):
    """Sum USDC base-units owned by ``receiving_address`` in a token-balance list.

    Returns (units_by_account_index, ok). ``ok`` is False if a USDC balance for
    our address reports an unexpected decimals (→ caller fails closed).
    """
    by_index: dict = {}
    for b in entries or []:
        if b.get("mint") != USDC_MINT:
            continue
        if b.get("owner") != receiving_address:
            continue
        ui = b.get("uiTokenAmount") or {}
        try:
            if int(ui.get("decimals", -1)) != USDC_DECIMALS:
                return {}, False  # not real USDC / unexpected — fail closed
            by_index[b.get("accountIndex")] = int(ui.get("amount", "0"))
        except (TypeError, ValueError):
            return {}, False
    return by_index, True


async def verify_usdc_payment(tx_signature: str) -> dict:
    """Verify a Solana USDC payment to our receiving address.

    Returns ``{"ok": bool, "reason": str}``. FAILS CLOSED on any uncertainty.

    Confirms the transaction:
      * has a well-formed signature,
      * exists and succeeded (``meta.err`` is None),
      * is no older than ``PAYMENT_VALIDITY_DAYS`` (blockTime freshness),
      * increased the USDC balance *owned by* ``PAYMENT_RECEIVING_ADDRESS`` by
        at least ``PAYMENT_PRICE_USD`` (mint + recipient + amount in one check).
    """
    receiving_address = (config.PAYMENT_RECEIVING_ADDRESS or "").strip()
    if not receiving_address:
        return _fail("receiving_address_not_configured")

    if not _looks_like_signature(tx_signature):
        return _fail("malformed_signature")

    sig = tx_signature.strip()
    expected_units = round(config.PAYMENT_PRICE_USD * (10 ** USDC_DECIMALS))
    max_age_seconds = config.PAYMENT_VALIDITY_DAYS * 86400

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            sig,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    }

    try:
        timeout = aiohttp.ClientTimeout(total=_RPC_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(config.SOLANA_RPC_URL, json=payload) as resp:
                if resp.status != 200:
                    return _fail(f"rpc_http_{resp.status}")
                data = await resp.json()
    except Exception as e:  # network error, timeout, bad JSON — fail closed
        log.warning("solana_pay RPC error for %s...: %s", sig[:8], e)
        return _fail("rpc_error")

    if not isinstance(data, dict) or data.get("error"):
        return _fail("rpc_error")

    result = data.get("result")
    if not result:
        return _fail("tx_not_found")

    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return _fail("tx_failed")

    block_time = result.get("blockTime")
    if not isinstance(block_time, (int, float)):
        return _fail("no_block_time")  # unconfirmed / missing — fail closed
    age = time.time() - block_time
    if age > max_age_seconds:
        return _fail("tx_too_old")
    if age < -300:  # clock skew guard: blockTime meaningfully in the future
        return _fail("bad_block_time")

    pre_map, ok_pre = _usdc_owned_balance(meta.get("preTokenBalances"), receiving_address)
    post_map, ok_post = _usdc_owned_balance(meta.get("postTokenBalances"), receiving_address)
    if not (ok_pre and ok_post):
        return _fail("unexpected_token_decimals")

    if not post_map:
        return _fail("no_usdc_to_recipient")

    # Net USDC received by our address = sum of (post - pre) over its USDC accounts.
    # A token account created in this tx has no pre entry (defaults to 0).
    received = 0
    for idx, post_amt in post_map.items():
        received += post_amt - pre_map.get(idx, 0)

    if received < expected_units:
        return _fail("amount_too_low")

    return {"ok": True, "reason": "payment_verified"}

# Security & Data Handling

This document explains how secrets and sensitive data are handled in this
project, and the operational steps required to keep them safe. It is proprietary
software — see [`LICENSE`](LICENSE).

## What sensitive data exists

| Item | Where it lives | In git? |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | environment variable only | ❌ never |
| `GROK_API_KEY` / `XAI_API_KEY` | environment variable only | ❌ never |
| `PAYMENT_RECEIVING_ADDRESS` | environment variable only (public receive address) | ❌ never |
| Subscriber list + payment ledger (chat IDs, `paid_until`, redeemed tx signatures) | SQLite DB at `HL_INTEL_DB_PATH` | ❌ gitignored |
| Market / wallet snapshots (public on-chain + market data) | SQLite DB | ❌ gitignored |

**This bot holds no private keys.** It never signs transactions, never custodies
funds, and never asks users for keys or seed phrases. It only *reads* public
on-chain and market data and *verifies* the one incoming USDC payment that
activates a pass. There is no wallet key material anywhere in the codebase — by
design.

## Rules

1. **Secrets are environment-only.** Every secret is read via `os.getenv`. Never
   hardcode a token, key, or private value in source. Never commit a real value
   in `.env.example` — it holds placeholders only.
2. **`.env` and databases are never committed.** They are gitignored, and a
   pre-commit hook (below) blocks them as a backstop.
3. **The database is PII.** It maps Telegram chat IDs to payment history. Store
   it on a private, access-controlled volume (`HL_INTEL_DB_PATH`). Do not share,
   export, or commit it. Back it up to private storage only.
4. **Keep the repository private.** A license grants legal recourse; a private
   repo is what actually prevents copying. This code and its trading/scoring
   logic are confidential trade secrets.

## Enable the secret-leak guard (once per clone)

A pre-commit hook in [`.githooks/`](.githooks) blocks committing `.env`, key/cert
files, database files, wallet/seed files, and secret-looking values. Turn it on:

```bash
git config core.hooksPath .githooks
```

This is a backstop, not a substitute for care. Bypassing it (`--no-verify`) to
commit a secret will leak it into history permanently.

## If a secret is ever exposed

Removing a secret from the latest commit is **not** enough — git history retains
it. If a token, key, or database is ever committed or leaked:

1. **Rotate it immediately** — the exposed value must be considered compromised
   forever.
   - Telegram bot token: revoke and reissue via [@BotFather](https://t.me/BotFather).
   - Grok/xAI key: revoke and reissue in the xAI console.
2. Purge it from history (`git filter-repo` or BFG) and force-push, then have all
   clones re-clone.
3. If the leaked item was the subscriber database, treat it as a PII incident.

## What "encrypting the build" can and cannot do

Encrypting the source so "no one can copy it" is not achievable: any server that
runs the code must decrypt it to execute, so anyone with access to the running
environment can recover it. Effective protection is operational, not
cryptographic on the source:

- **Private repository** + least-privilege collaborator access.
- **Secrets in the environment**, never in the artifact.
- **Database encryption at rest** is worthwhile for the one file that holds PII —
  achieve it at the infrastructure layer (an encrypted volume / managed disk) or,
  if stronger guarantees are needed, a SQLCipher build of SQLite. The repo itself
  contains no data to encrypt.

## Reporting

Found a vulnerability or an exposed secret? Contact the Owner privately. Do not
open a public issue.

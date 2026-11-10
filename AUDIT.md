# Launch80 v1.1 — import audit

Audit of the `launch80-bot-profit` package as imported from the uploaded
archive (commit "Import Launch80 v1.1 profit-gated launch-pool bot").

Scope: static review of all 12 modules and 5 test files, dependency/packaging
review, container and CI review, and verification of the hard-coded chain
constants. **Out of scope:** any funded end-to-end execution, and any claim
that the on-chain transactions actually enforce the minimum-output guard. The
existing `LIVE_READINESS_CHECKLIST.md` gates still apply in full; nothing here
moves this code closer to being approved for real funds.

Baseline: 36 tests passed on import (Python 3.13). After the fixes below:
56 tests pass.

---

## Fixed

### 1. Solana mainnet check could never pass (blocking, live Solana)

`SolanaExchange.validate_network()` compared `getGenesisHash` against
`5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`. That string is the **CAIP-2 chain
identifier** for Solana mainnet, which is only the first 32 characters of the
genesis hash. It base58-decodes to 23 bytes, so it cannot equal any 32-byte
genesis hash that an RPC will ever return.

Effect: with `LIVE=true` and `ENABLE_SOLANA=true`, `Engine.run()` calls
`validate_network()` on every start and would always raise
`RPC is not Solana mainnet-beta`. Live Solana trading was unreachable, and
`launch80 check` always reported a Solana preflight error.

Fix: use the full 44-character mainnet-beta genesis hash
(`5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d`, cross-checked against
QuickNode's `getGenesisHash` reference and GetBlock's Solana docs), hold it in
`Settings` as `solana_expected_genesis`, allow an operator override via
`SOLANA_EXPECTED_GENESIS_HASH`, and reject at config time any value that does
not base58-decode to 32 bytes — so a truncated constant fails loudly instead
of silently disabling the cluster guard. Operators must still verify the value
independently with `solana genesis-hash --url mainnet-beta`.

### 2. 0x settlement-target allowlist bypassed `Settings`

`RobinhoodExchange.__init__` read `ZEROX_APPROVED_TX_TARGETS` with
`__import__('os').getenv(...)` instead of taking it from the validated,
frozen `Settings`. The variable was documented in `.env.example` but was the
only trading-critical allowlist not represented in config, so
`Settings.validate()` could pass while the adapter held a different or empty
allowlist, and a missing allowlist raised a bare `ValueError` out of
`asyncio.run()` as a traceback rather than the `Invalid configuration:` path.

Fix: added `approved_tx_targets` to `Settings`, required it under
`LIVE=true` with `ENABLE_ROBINHOOD=true`, and sourced the adapter's allowlist
from config.

### 3. Unvalidated bounds allowed a self-inflicted hot loop

`MAX_EXIT_RETRY_SECONDS` and `MAX_CONSECUTIVE_FEED_FAILURES` were loaded but
never range-checked, unlike every other numeric setting.

- `MAX_EXIT_RETRY_SECONDS <= 0` makes `retry_sell()` and
  `defer_quote_unavailable()` schedule `next_sell_retry_at` in the past, so the
  exit loop re-quotes the venue continuously (rate-limit bans, cost).
- `MAX_CONSECUTIVE_FEED_FAILURES <= 0` latches the entry circuit breaker on the
  first poll, permanently pausing new buys.

Fix: bounds added (`1..3600` and `1..1000`).

### 4. Throttling response could crash a read instead of backing off

`Api.get` computed its backoff with
`float(response.headers.get('Retry-After', 0) or 0)`. RFC 9110 permits
`Retry-After` to be an HTTP-date; `float()` raises `ValueError` on that, which
is not caught by the loop's `except (TimeoutException, ConnectError)`. A
spec-legal 429 from GeckoTerminal, 0x or Jupiter would therefore propagate as
an unexpected error rather than retry — counting toward the feed circuit
breaker, and inside `Engine.enter()` landing in the generic `except Exception`
handler.

Fix: `_retry_after_seconds()` parses both forms, clamps to `[0, 8]`, and
returns `0.0` for anything unusable.

### 5. Non-ASCII `Authorization` header returned 500 instead of 401

`server.auth()` passed `str` operands to `hmac.compare_digest`, which raises
`TypeError: comparing strings with non-ASCII characters is not supported`.
Starlette decodes request headers as latin-1, so any client sending a high
byte in the header triggered an unhandled exception. Reproduced against the
original code; now covered by a regression test.

Fix: compare UTF-8-encoded bytes and reject a missing header first.

### 6. Packaging

- `launch80/__init__.py` declared `1.0.0` while `pyproject.toml` and the README
  say 1.1 — now `1.1.0`, and `server.py` reports it instead of a second
  hard-coded string.
- `exchange.py` imports `eth_account` directly but it was undeclared, relying
  on web3's transitive dependency. Added `eth-account>=0.13,<1.0`.
- CI tested only Python 3.12 although `requires-python` is `>=3.11,<3.14`.
  Added a 3.11/3.12/3.13 matrix and `permissions: contents: read`.
- Added `*.egg-info/`, `build/`, `dist/` to `.gitignore`.

---

## Reviewed and found sound

These were checked and are working as documented; they are listed so a later
reviewer does not have to redo the work.

- **Restart/recovery state machine.** Every status transition in `storage.py`
  is guarded by a `WHERE status=...` predicate, so a stale in-flight write
  cannot resurrect a closed position. `BUYING`/`SELLING` become
  `RECOVERY_BUY`/`RECOVERY_SELL` on restart and latch `paused`.
- **No blind replay.** Both adapters raise `UncertainExecution` on any
  ambiguous broadcast or confirmation, and no caller retries it.
- **Profit gate.** `min_exit_proceeds()` rounds *up*
  (`(spent*bps + 9999)//10000`), so rounding cannot favour selling at a loss.
  The engine checks the venue's slippage-protected minimum before signing,
  re-checks it on the firm quote, and quarantines the position if the reported
  fill lands under the floor.
- **Other chain constants.** Solana USDC/USDT/WSOL mints and the SPL Token
  program id all decode to 32 bytes and match the canonical values; the
  `MINT_EXCLUDE` entries in `discovery.py` agree with `config.py`.
- **Secrets hygiene.** `.env` is git-ignored, keys are only read from the
  environment, and no code path logs a key. `compose.yaml` publishes port 8088
  to `127.0.0.1` only and runs read-only with `cap_drop: ALL` and
  `no-new-privileges`.
- **Single-writer lock.** `main.py` holds an exclusive `flock` on
  `<db>.process.lock` for the whole `run` command.

---

## Known issues NOT fixed (flagged for the readiness gates)

Left alone deliberately: fixing these means guessing at third-party response
shapes, or changing trading behaviour, neither of which should happen without
the funded acceptance testing the checklist already requires.

1. **Garp strict mode is probably unusable.** `garp.enrich()` compares
   `r.get('chainId')` against the GeckoTerminal network slug (`'solana'` /
   `'robinhood'`). If Garp returns a numeric or differently-spelled chain id,
   every token mismatches. With the default `GARP_REQUIRE_LAUNCHPAD=false` this
   only yields `'unverified'`, but with it set to `true` the bot would likely
   reject every candidate. Verify the real response shape before enabling.
2. **0x responses are not checked for token echo.** `RobinhoodExchange.trade`
   validates `sellAmount`, the settlement target, the allowance spender and
   `minBuyAmount`, but not that the quote is for the intended `sellToken` /
   `buyToken`. The Solana V1 sell path does validate `inputMint`/`outputMint`.
   Closing this asymmetry needs the authoritative v2 response schema.
3. **`before_token_balance` is recorded but never used.** `claim()` stores the
   pre-trade balance; `exit()` compares the *total* wallet balance against
   `bought_units` without subtracting it. Conservative today (it can only
   over-estimate holdings), but dead state that invites a wrong future fix.
4. **In-flight entries contribute zero exposure.** `exposure_usd()` sums
   `buy_spent_units`, which is `0` for a `BUYING` row until the fill is
   journalled. Mitigated by the single `_trade_lock` and the `active_count`
   cap, so only one entry is ever in flight.
5. **Third-party safety oracle.** `token_safety` for Robinhood trusts
   `api.swarm0x.sh`. It fails closed, but it is an unvetted single point of
   trust on the entry path — as the README already warns.
6. **Partial legacy-DB migration.** `Store.__init__` adds only the v1.1
   underwater columns. A table missing core columns (`buy_spent_units`,
   `attempted_at`, …) is not repaired and will fail at runtime. The mode lock
   already pushes operators toward a fresh database, which is the right path.
7. **Jupiter V1 sell path** remains a compatibility route on an unmaintained
   API, as the author documents. Unchanged.

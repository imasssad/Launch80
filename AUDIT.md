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

## Second pass (line-by-line) — further fixes

A full file-by-file re-read after the first round of fixes. Five more issues
closed, each with a regression test verified to fail against the pre-fix code:

7. **Feed data could rewrite request URLs.** `parse_pool()` regex-validates
   the token mint but not the pool address, and `pool.pool` is interpolated
   into a URL path by `PaperExchange._spot`
   (`/networks/{chain}/pools/{pool.pool}`). A feed row whose pool id contained
   `/`, `?`, `#` or `%` would redirect the request. Pool addresses are hex or
   base58, so strictly alphanumeric: anything else is now rejected at parse
   time (`^[A-Za-z0-9]{1,64}$`).

8. **Live exit could liquidate pre-existing holdings.** `claim()` records the
   pre-buy token balance, but `exit()` compared the *total* wallet balance
   against `bought_units`. If the wallet already held the token, a partially
   missing position still passed the check — and the sell would have burned
   the pre-existing holding to cover the shortfall. The check is now
   `balance >= before_token_balance + bought_units`; anything less goes to
   `RECOVERY_SELL` for manual reconciliation. This also removes the dead
   `before_token_balance` state flagged in the first pass.

9. **In-flight and unreconciled buys carried zero exposure.** `claim()`
   inserted `buy_spent_units=0`, so `BUYING` and `RECOVERY_BUY` rows
   contributed nothing to `exposure_usd()` or `stats` until the fill was
   journalled. The intended spend is now recorded at claim time and replaced
   by the actual fill in `open_position()`.

10. **Token-echo gap closed conservatively** (flagged in the first pass).
    The Robinhood firm quote now rejects before signing when the venue
    explicitly echoes a `sellToken`/`buyToken` other than the intended pair,
    and the Solana V2 buy order does the same for `inputMint`/`outputMint`/
    `inAmount` — matching the validation the V1 sell path already had. The
    checks are conditional: an absent field keeps the previous behaviour, so
    no response-schema assumption is baked in. Full schema verification
    remains a readiness-gate item.

11. **Live RPC provider check matched anywhere in the URL.**
    `'alchemy' in robinhood_rpc` accepted `https://evil.example/alchemy`. The
    provider name is now matched against the URL's hostname.

Also: the pytest-asyncio fixture-loop-scope deprecation is pinned to
`function` in `pyproject.toml`, and the HTTP client User-Agent reports the
real package version.

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
2. **Third-party safety oracle.** `token_safety` for Robinhood trusts
   `api.swarm0x.sh`. It fails closed, but it is an unvetted single point of
   trust on the entry path — as the README already warns.
3. **Partial legacy-DB migration.** `Store.__init__` adds only the v1.1
   underwater columns. A table missing core columns (`buy_spent_units`,
   `attempted_at`, …) is not repaired and will fail at runtime. The mode lock
   already pushes operators toward a fresh database, which is the right path.
4. **Jupiter V1 sell path** remains a compatibility route on an unmaintained
   API, as the author documents. Unchanged.
5. **Signed transaction contents are not program-level verified.** On both
   chains the bot signs calldata/transactions built by the aggregator after
   allowlist, floor and echo checks; proving the on-chain min-output guard is
   a funded acceptance-test item, not a static one.
6. **Paper model uses a fixed 100 bps protected-output haircut** rather than
   the configured `SLIPPAGE_BPS`; it is an explicitly rough simulation, not a
   fill model.

---

## Third pass — full security audit (structured, with severities)

Method: re-read of every module highest-risk first (exchange → engine →
storage → config/preflight/server/main), verification of the 0x and Jupiter
integrations against their current public docs, gas-cost estimation, race and
restart analysis, `pip-audit`, `ruff`, `mypy`, and a paper-mode service run.
Baseline before this pass: 74 tests; after: 98. Every behavioural fix was
written as a failing test first.

### Findings fixed in this pass

| ID | Severity | Where | Finding |
|----|----------|-------|---------|
| H-1 | HIGH | `engine.min_exit_proceeds` | Floor excluded gas. Solana roundtrip fees (base + priority, ≤250k lamports/leg) plus never-recovered ATA rent (~0.00204 SOL) can exceed the $0.075 floor profit on a $5 trade; the EVM path permits up to 0.0007 ETH per tx. A floor-passing exit could lose money. Fixed: flat `EXIT_GAS_RESERVE_USD` (default 0.10) added to the floor; operator-measured, bounded 0..100. |
| H-2 | HIGH | `exchange.SolanaExchange` | Sell path depended on Jupiter V1 Metis, which Jupiter no longer maintains (lite-api already sunset; v6 retired). Current V2 `/order` *does* return `otherAmountThreshold` (confirmed against Jupiter's API reference and a CoW Protocol integration), so the stated reason for staying on V1 no longer holds. Fixed: both legs now use V2 `/order`+`/execute`; sells are signed only when the enforced threshold clears the floor; proceeds measured from real balance delta. NEEDS LIVE VALIDATION. |
| M-1 | MEDIUM | `main.py` reconcile | `--verify-onchain` was pure attestation even in live mode where RPC credentials exist. Fixed: live `buy-filled`/`sell-filled` now require the txid to exist on-chain and not have failed (`launch80/verify.py`), refusing when unverifiable. Amount fields remain operator attestation. NEEDS LIVE VALIDATION. |
| M-2 | MEDIUM | caps / stuck positions | A rug or honeypot holds forever by design, so 2 stuck positions permanently exhaust `MAX_OPEN_POSITIONS` and their principal pins `MAX_TOTAL_EXPOSURE_USD` — the bot ends up alive but unable to trade, with no invariant-preserving release. Fixed: `launch80 abandon <key> --confirm-total-loss --verify-onchain` — operator-only write-off to a terminal `ABANDONED` state that frees capacity and counts the full principal as realized loss (so the daily loss cap sees it). The engine never calls it: no automated loss-selling. |
| M-3 | MEDIUM | `engine.py`, `preflight.py` | Raw transport-error text was persisted to SQLite events/`last_error` and printed by `launch80 check`; httpx/web3 errors embed the full request URL, and RPC URLs routinely carry the API key in the path. Fixed: `Settings.scrub()` strips all configured keys/tokens and reduces RPC URLs to scheme+host at every persistence point. |
| L-1 | LOW | `Dockerfile` | No container healthcheck; compose `restart: unless-stopped` could keep a wedged process "up". Fixed: HEALTHCHECK against `/health`. |
| L-2 | LOW | repo-wide | 11 unused/redefined imports (ruff F401/F811) removed; `PaperExchange.trade` compared an Optional without a None guard (mypy) — guarded. Ruff configured to bug-level rules (E4,E9,F): the one-line `if x: y` style and broad fail-closed `except Exception` blocks are the author's deliberate idiom, left alone. |

### Checked and found sound in this pass

- **pip-audit:** no known vulnerabilities in the dependency set.
- **0x integration vs current docs:** AllowanceHolder v2 endpoints, required
  `taker`, `minBuyAmount`, approve-only-`issues.allowance.spender`/
  `allowanceTarget`, never-approve-Settler — all match. A null `minBuyAmount`
  (shown in the docs' own example) fail-closes: the engine holds and the firm
  path rejects. Robinhood chain id 4663 and 0x support for it: NEEDS LIVE
  VALIDATION.
- **Races:** concurrent exits on one key cannot double-sell
  (`mark_selling` is a status-guarded compare-and-swap; regression test
  added); entries are serialized by `_trade_lock` with a re-check before the
  claim; restart mid-buy/mid-sell quarantines to `RECOVERY_*` and pauses
  (existing tests).
- **Solana token_safety:** rejects Token-2022 and any mint with an active
  mint/freeze authority, uninitialized or non-mint accounts — now covered by
  unit tests. It cannot detect LP pulls, holder concentration or router
  blacklists; the $5K liquidity figure is reported by GeckoTerminal and can be
  wash-faked, mitigated only partially by `MAX_LIQUIDITY_FRACTION`, the
  roundtrip quote gate and (EVM) the simulation oracle.
- **Secrets at rest:** `.env` git-ignored, compose injects via `env_file`,
  keys never constructed into URLs by this code, monitor bound to
  `127.0.0.1`, control endpoints constant-time token, read endpoints
  local-only by deployment.

### Deferred (documented, not guessed at)

- mypy: 22 remaining errors — TypedDict friction with web3 and dict-shape
  reports in `preflight.py`; no behavioural defect found behind them.
- Program-level verification that the signed transactions enforce the minimum
  output on-chain (both chains): funded acceptance testing only.
- Garp `chainId` comparison, the `api.swarm0x.sh` oracle, and the partial
  legacy-DB migration: unchanged from pass two, still flagged.

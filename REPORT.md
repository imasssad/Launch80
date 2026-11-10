# Launch80 — security audit report

Audit and remediation of the `launch80` bot (GeckoTerminal discovery → ~$5
entries on Robinhood Chain via 0x and Solana via Jupiter → 80s timer →
profit-gated exits → SQLite journal). Full finding-by-finding detail lives in
`AUDIT.md` (three passes); this report is the summary deliverable.

**This report does not claim the bot is profitable or safe.** It records what
was found, what was fixed, what was verified, and what still blocks real
funds. The project's own stance stands: NOT APPROVED for unattended
real-money execution.

## Test results

| Stage | Tests |
|---|---|
| Baseline (as imported) | **36 passed** |
| After pass 1 (import-defect fixes) | 56 passed |
| After pass 2 (line-by-line loopholes) | 74 passed |
| After pass 3 (this audit's fixes) | **98 passed** |

Every behavioural fix was written as a failing test first and the test was
confirmed to fail against the pre-fix code. Also green: `ruff` (bug-level
rules), `pip-audit` (no known vulnerabilities), `python -m compileall`.
`mypy`: 22 non-behavioural errors deferred (see below). Paper-mode service
run verified: `/health`, `/positions`, `/events` respond; unauthenticated
`/pause` → 403; SIGTERM shuts down cleanly; `launch80 check` exits 2 on
failure.

## Findings fixed (by severity)

### HIGH
1. **Solana mainnet guard unsatisfiable** — the expected genesis hash was the
   32-char CAIP-2 chain id, which can never equal a real 32-byte hash; live
   Solana could not start. Fixed with the full hash + decode-length
   validation. (`exchange.py` / `config.py`)
2. **Gas-blind profit floor** — the 1.5% floor (= $0.075 on $5) ignored
   roundtrip fees and never-recovered Solana ATA rent (~0.00204 SOL), and the
   EVM path allows up to 0.0007 ETH/tx: a floor-passing exit could lose
   money. Fixed: flat `EXIT_GAS_RESERVE_USD` (default 0.10, operator-measured)
   added to the floor. (`engine.py` / `config.py`)
3. **Unmaintained Jupiter V1 sell path** — migrated both legs to current Swap
   V2 `/order`+`/execute`; sells are signed only when V2's
   `otherAmountThreshold` (the on-chain-enforced minimum) clears the floor,
   and proceeds are measured from the real balance delta. (`exchange.py`)
4. **Live exit could liquidate pre-existing holdings** — exit compared total
   wallet balance against bought units, so pre-existing tokens could mask a
   missing position and be sold to cover it. Fixed:
   `balance >= pre-buy baseline + bought`. (`engine.py`)

### MEDIUM
5. **Reconcile was attestation-only** — in live mode, `buy-filled`/
   `sell-filled` now also confirm the txid on-chain (exists, did not fail)
   and refuse otherwise; amounts remain operator attestation. (`verify.py`,
   `main.py`)
6. **Stuck positions permanently exhausted the 2-position/$20 caps** — two
   rugs leave the bot alive but unable to trade. Fixed with
   `launch80 abandon`: operator-only write-off to `ABANDONED` that frees
   capacity and counts the full principal as realized loss against the daily
   cap. The engine never sells at a loss and never calls it.
7. **Secrets could persist in error text** — httpx/web3 errors embed full
   request URLs (RPC keys ride in URL paths) and landed in SQLite
   events/`last_error` and `launch80 check` output. Fixed with
   `Settings.scrub()` at every persistence point.
8. **0x settlement-target allowlist bypassed validated config**; **token-pair
   echo unchecked on execution quotes**; **feed data could rewrite request
   URLs** (pool address charset); **RPC provider check matched the whole URL**
   (`https://evil.example/alchemy` passed); **non-ASCII Authorization header
   → 500**; **HTTP-date Retry-After crashed reads**; **unvalidated retry/
   circuit-breaker bounds** enabling a hot re-quote loop. All fixed in passes
   1–2 (details in `AUDIT.md`).

### LOW
9. Version mismatch (1.0.0 vs 1.1.0), undeclared direct dependency
   (`eth-account`), CI tested one Python of three supported (now a 3.11–3.13
   matrix + least-privilege permissions), in-flight buys invisible to
   exposure accounting, missing container healthcheck, unused imports, an
   Optional comparison without a None guard.

## Verified sound (so it is not re-audited from scratch)

- No code path retries an `UncertainExecution`; ambiguous broadcasts
  quarantine to `RECOVERY_*` and pause new entries.
- Every SQLite transition is status-guarded (CAS-style); restart mid-buy/
  mid-sell quarantines; the paper/live mode lock refuses mixed databases;
  concurrent exits cannot double-sell (tested).
- 0x usage matches current docs: AllowanceHolder v2, required `taker`,
  `minBuyAmount` honored fail-closed (holds when null), approvals exact and
  reset-to-zero first, spender/target allowlists, never approve Settler.
- Floor math rounds in the bot's favor; the firm quote is re-checked after
  any approval; fills reported under the floor quarantine instead of closing.
- Secrets: `.env` git-ignored; compose publishes 8088 on `127.0.0.1` only,
  read-only fs, `cap_drop: ALL`, non-root; control endpoints require a
  >=24-char token compared constant-time; single-writer process lock.
- `pip-audit`: clean.

## Deferred, with reasons

| Item | Reason |
|---|---|
| Program-level proof that signed transactions enforce the min output on both chains | Requires funded microtrades; no static analysis substitutes. The checklist gates on it. |
| Garp `chainId` comparison (strict mode likely rejects everything) | Garp's response schema is unverifiable from here; strict mode is off by default and fail-closed. |
| `api.swarm0x.sh` safety oracle trust | Fails closed, but vetting a third-party oracle is an operator decision, not a code change. |
| Robinhood chain id 4663, USDG address, 0x support for that chain | Not independently verifiable from this environment. |
| Liquidity spoofing (`reserve_in_usd` wash-faked) | Inherent to a reported-TVL feed; mitigations already present (`MAX_LIQUIDITY_FRACTION`, roundtrip gate, simulation oracle); no honest code fix exists. |
| mypy: 22 remaining errors | web3 TypedDict friction and dict-shape report-building in `preflight.py`; inspected, no behavioural defect behind them. |
| Legacy-DB migration only adds v1.1 columns | Mode lock already forces fresh databases; repairing arbitrary legacy schemas invites silent corruption. |
| E701/E702 one-line style, broad `except Exception` | The author's deliberate fail-closed idiom; churn without benefit. |

## Items blocking real funds (NEEDS LIVE VALIDATION)

1. Funded microtrade acceptance on **both** chains per
   `LIVE_READINESS_CHECKLIST.md` — every gate there is still open.
2. Jupiter Swap V2 integration: field names, `/execute` status semantics, and
   on-chain enforcement of `otherAmountThreshold`.
3. 0x on Robinhood Chain: chain id 4663 support, settlement/spender addresses
   from live quotes and receipts, `minBuyAmount` population.
4. Real roundtrip cost measurement per chain to set `EXIT_GAS_RESERVE_USD`
   from data (the 0.10 default is an estimate), including ATA rent and
   failed-transaction fees.
5. Live reconcile verification (`verify.py`) against real RPCs.
6. The independent review, wallet, RPC provenance, and operational gates the
   checklist already lists (unchanged).

Defaults remain safe: `LIVE=false`, `BUY_ENABLED=false`, both acknowledgment
strings required, no gate weakened, no keys added anywhere.

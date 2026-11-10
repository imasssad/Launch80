# Launch80 v1.1 — Profit-gated 80-second launch-pool bot

**Mode by default:** paper trading; `LIVE=false`; **new buys disabled** (`BUY_ENABLED=false`) until explicitly armed. **Real-funds status:** code and unit tests provided, but *not independently audited or end-to-end tested on funded wallets*. Never regard this package as guaranteeing a profit or safe to fund without the acceptance process below.

## What changed

**After each confirmed buy, the bot waits at least `HOLD_SECONDS=80` seconds.** At the first exit check and on subsequent checks:

- It requests a current sell quote for the complete recorded position and checks its **slippage-protected minimum stablecoin output** against entry cost plus a configurable floor.
- When that minimum is **below** the floor, it keeps the position `OPEN` and schedules another read-only price check in `PROFIT_RECHECK_SECONDS=12` seconds. It never sells merely because the timer expired.
- When that minimum clears the floor, it obtains another *firm, execution-ready* quote and only proceeds if the execution route also passes the floor. If the price has fallen before signing, the bot goes back to holding.
- If the asset stays underwater, its recorded position/exposure and timer survive restarts. After `UNDERWATER_ALERT_SECONDS=900` seconds, it emits a long-hold alert. At or above a `UNDERWATER_PAUSE_BPS=3500` (35%) conservative quote loss, it pauses **new buys**, not the sell-monitoring loop.
- Unknown transaction outcomes trigger `RECOVERY_BUY` / `RECOVERY_SELL` and require independent on-chain reconciliation. A trading-mode lock forbids reusing paper position data in live mode.

**Default floor:** The received USDG/USDC minimum must be at least **101.5% of principal** (`MIN_EXIT_PROFIT_BPS=100` + `EXIT_COST_BUFFER_BPS=50`). For a 5.000000 stablecoin-unit buy, the minimum is **5.075000 units**. The 0.5% cost buffer is configurable, NOT a reliable gas-fee estimate. Real net profit may be negative after ETH/SOL gas, priority fees, stablecoin depegs, and operational costs. Holdings can go to zero and never recover. There is no forced stop-loss under this policy.

## Networks and integrations

| Network | Discovery | Entry swap | Profit-gated exit |
|---|---|---|---|
| Robinhood Chain (4663) | GeckoTerminal `/new_pools`; optional Garp metadata | 0x Swap AllowanceHolder v2 USDG/token | 0x price + **firm `minBuyAmount`** checked against floor |
| Solana | GeckoTerminal `/new_pools`; optional Garp metadata | Jupiter Swap V2 USDC/token | Jupiter **Swap V1 Metis `otherAmountThreshold`** + bounded on-chain swap, because V2 Meta-Aggregator `/order` doesn't publish a directly verifiable min-output field |

The Jupiter V1 implementation is a **compatibility path**, and is no longer actively maintained by Jupiter. A qualified engineer should validate the transaction's actual program-level min-output guard in staging or replace it with a fully validated Jupiter V2 `/build` integration before meaningful mainnet funds are exposed. This is not a cross-chain transfer bot.

The scanner detects actual GeckoTerminal liquidity pools with **reported liquidity >=$5,000**, not every new bonding-curve mint. Pool TVL can be inaccurate, stale, pulled, or entirely outside the currently tradable price range. Initial latency is limited by `POLL_SECONDS=30` and public feed coverage; this is not a mempool sniper.

## Quick start — paper mode

```bash
git clone https://github.com/imasssad/Launch80.git
cd Launch80
cp .env.example .env
# LIVE=false; no wallet keys required.
# For paper trades, set BUY_ENABLED=true in .env first.
docker compose up --build -d
curl http://127.0.0.1:8088/health
curl http://127.0.0.1:8088/positions
curl http://127.0.0.1:8088/events
```

Or Python 3.11–3.13 (virtual environment):

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
PYTHONPATH=. pytest -q
launch80 check  # read-only; no orders
launch80 run  # reads BUY_ENABLED; paper trades only if explicitly enabled
```

**Important:** Existing paper databases with positions from v1.0 lack a mode tag and are intentionally rejected by v1.1. Use a **fresh database** for v1.1 paper tests and a separately named database/Compose project for live mode. Do not delete an old journal before backing it up and reconciling any real positions.

## Default configuration

The `.env.example` contains all settings; new orders remain disabled unless `BUY_ENABLED=true` is set explicitly. Key trading limits are:

| Setting | Default | Role |
|---|---:|---|
| `BUY_ENABLED` | false | No new entries until explicitly armed; exits continue regardless |
| `MIN_LIQUIDITY_USD` | 5000 | Discovery threshold (reported, not guaranteed exit liquidity) |
| `BUY_USD` | 5 | Fixed maximum entry in 6-decimal stablecoin atomic units |
| `MAX_LIQUIDITY_FRACTION` | 0.001 | Max order 0.1% of reported pool liquidity |
| `MAX_OPEN_POSITIONS` | 2 | Max tracked concurrent open/uncertain trades |
| `MAX_TOTAL_EXPOSURE_USD` | 20 | Tracked principal cap, excludes fees/unrealized movement |
| `HOLD_SECONDS` | 80 | Minimum delay until first profit evaluation, *not a sell deadline* |
| `MIN_EXIT_PROFIT_BPS` | 100 | Minimum desired stablecoin profit (1%) |
| `EXIT_COST_BUFFER_BPS` | 50 | Additional stablecoin proceeds buffer (0.5%) |
| `PROFIT_RECHECK_SECONDS` | 12 | Re-quote cadence when no profitable exit |
| `UNDERWATER_ALERT_SECONDS` | 900 | Long-hold monitoring event after 15 min |
| `UNDERWATER_PAUSE_BPS` | 3500 | Pause new buys after >=35% conservative quoted drawdown |
| `SLIPPAGE_BPS` | 100 | Max swap slippage setting (1%); not insurance |
| `MAX_ROUNDTRIP_COST_BPS` | 600 | Reject immediately unattractive entry quotes |
| `MAX_EXIT_ATTEMPTS` | 8 | Limit actual failed execution attempts, not losing-price observations |
| `MAX_DAILY_REALIZED_LOSS_USD` | 20 | Pauses new buys on realized stable-denominated losses; gas excluded |

`MAX_OPEN_POSITIONS` and `MAX_TOTAL_EXPOSURE_USD` include loss-holding positions and `MANUAL_EXIT` cases. A held position consumes exposure capacity until it sells or an operator independently reconciles it.

## Preparing to trade with real funds

**Do not place secret keys in ChatGPT, GitHub, logs, or a support ticket.** Use low-balance, dedicated hot wallets for each chain. Deploy server-side with private environment injection (prefer secret management/KMS). Never repurpose a treasury wallet. 0x addresses must be verified from official contracts and current firm quotes.

1. **Complete tests and code review.** Run `PYTHONPATH=. python -m pytest -q`. Have a reviewer independently inspect transaction signing, swap calldata/program, spending allowances, chain RPC provenance, `minBuyAmount` and `otherAmountThreshold` enforcement, replay/restart behavior, and the complete entry/sell balance accounting. The tests provided here are mocks, not a live guarantee.
2. **Validate read-only connectivity.** Set `LIVE=true`, `BUY_ENABLED=false`, `LIVE_ACK=I_ACCEPT_POSSIBLE_TOTAL_LOSS`. Supply your own paid RPCs, API keys, exact chain-verified 0x spender/settlement target allowlists, and dedicated test-wallet keys, then run `launch80 check` on the deployment host. It fetches feeds, confirms chain identifiers and stablecoin decimals, reads balances, and requests indicative buy and sell quotes only. A failure must block acceptance.
3. **Use a fresh volume and separate environment for live mode.** For Docker, shut down paper trading first and use a distinct project name (for example `docker compose -p launch80-live up --build -d`). Do not copy the paper SQLite DB into the live project. The mode lock refuses mismatches, including nonempty untagged v1.0 databases.
4. **Perform supervised funded microtrades** with fresh, audited test tokens on **each chain**. Check explorer receipts and token/stablecoin balance differences. Reproduce profitable sells, underwater holds, quote movements between preview and firm signing, RPC timeouts, single-process restarts, gas exhaustion, and ambiguous confirmations. Never assume a result is successful from an HTTP 200 alone.
5. **Enable unattended new buys only after sign-off.** Set `BUY_ENABLED=true` AND `LIVE_BUY_ACK=I_ENABLE_REAL_PURCHASES` with limits initially no higher than the shipped defaults, restart the service, and ensure an operator can use an external wallet to dispose of untradeable positions. Add paging for loss holds and circuit breakers, private DB backups, RPC fallback and KMS/HSM signing before scaling.

Example values (placeholders, not real credentials):

```dotenv
LIVE=true
LIVE_ACK=I_ACCEPT_POSSIBLE_TOTAL_LOSS
LIVE_BUY_ACK=
BUY_ENABLED=false
ROBINHOOD_RPC_URL=https://robinhood-mainnet.g.alchemy.com/v2/REPLACE_ME
SOLANA_RPC_URL=https://YOUR_PRIVATE_SOLANA_RPC
ZEROX_API_KEY=REPLACE_ME
JUPITER_API_KEY=REPLACE_ME
EVM_PRIVATE_KEY=REPLACE_ME
SOLANA_PRIVATE_KEY_B58=REPLACE_ME
ZEROX_APPROVED_SPENDERS=REPLACE_WITH_CHAIN_VERIFIED_0X_ALLOWANCE_HOLDER
ZEROX_APPROVED_TX_TARGETS=REPLACE_WITH_CHAIN_VERIFIED_0X_SETTLEMENT_TARGET
```

The separate `LIVE_BUY_ACK` value must be set to `I_ENABLE_REAL_PURCHASES` only after completing supervised acceptance and explicitly deciding to risk real funds. To stage only one chain, set `ENABLE_ROBINHOOD=false` or `ENABLE_SOLANA=false`. Keep `REQUIRE_ROBINHOOD_SIMULATION=true` until the safety provider is independently tested and vetted. The code should fail closed rather than silently bypass missing checks. `LIVE=true` alone never makes a codebase audited or risk-free.

### Monitoring and recovery

```bash
launch80 stats
launch80 positions
launch80 events
launch80 pause   # only new buys are paused; existing positions are still monitored
```

The HTTP monitor binds to `127.0.0.1:8088` through Compose. Do **not** expose it to the Internet; its `GET` routes display sensitive position data. To enable authenticated pause/resume endpoints, configure a unique `CONTROL_TOKEN` (>=24 chars). Pay attention to `HOLD_UNDERWATER`, `UNDERWATER_LONG_HOLD`, `UNDERWATER_RISK_PAUSE`, `SELL_QUOTE_RETRY`, `HOLD_PRICE_MOVED`, `SELL_UNCERTAIN`, `MANUAL_EXIT` and `FEED_CIRCUIT_BREAKER` events.

`OPEN` with `underwater_since` means no sell will be attempted until a minimum protected output clears the floor. `next_sell_retry_at` is the next evaluation time. When quote providers go offline, the position remains open; this is not proof that tokens are still transferable or can be liquidated. At a 35% loss, new buys pause; the re-quote loop continues. A manually initiated wallet sale must be reconciled in SQLite before automated exits can safely continue. A circuit-breaker pause is not a loss stop.

After uncertain execution, **do not blindly replay the transaction**. Inspect the actual chain signature/nonce and both token balances first. Supported manual commands:

```bash
launch80 reconcile 'solana:<pool-address>' buy-not-filled --verify-onchain
launch80 reconcile 'solana:<pool-address>' buy-filled \
  --txid '<verified-tx>' --spent-atomic 5000000 --received-atomic 123456789 \
  --buy-confirmed-at '<verified-unix-time>' --verify-onchain
launch80 reconcile 'solana:<pool-address>' sell-not-filled --verify-onchain
launch80 reconcile 'solana:<pool-address>' sell-filled \
  --txid '<verified-tx>' --received-atomic 5075000 --verify-onchain
launch80 resume
```

`--verify-onchain` is a *human attestation flag*, not a cryptographic verifier. Do not use it without independently checking transactions and balances. The current SQLite recovery workflow is not a substitute for a professional reconciliation service.

### Explicit unresolved risks

- **No guarantee against loss**: rug pulls, token transfer taxes/blacklists, dead liquidity, price crashes, relayers, reorgs, RPC and indexing failure, or a stablecoin losing its peg can strand positions permanently. A "wait until profitable" rule may never sell a token.
- **Profit metric is stablecoin-denominated before native gas fees.** An exit above the stablecoin principal can still lose real money. There is no marked-to-market loss cap and no forced liquidation.
- **0x** uses a verified allowed transaction target and exact allowances, but only on-chain receipts and a security review can establish correct output, calldata, nonce/fee handling and approvals for a particular live token.
- **Jupiter V1 sell path** was chosen for its explicit output threshold and is not actively maintained; its program-level slippage assertion needs live acceptance verification before nontrivial deployment.
- **New liquidity pool detection** is poll-based and the $5K liquidity figure does not guarantee usable exit liquidity. Garp and external token-risk scans are informative only and may be delayed or unavailable.
- **Unverified third-party response shapes.** `GARP_REQUIRE_LAUNCHPAD=true` compares Garp's `chainId` against the GeckoTerminal network slug and may therefore reject every candidate; confirm the real response before enabling it. Execution quotes now reject a mismatched token-pair echo on both chains, but only when the venue sends those fields — verify the live schemas include them. See `AUDIT.md`.
- Fees and priority costs on **failed swaps** are borne even if the protected transaction reverts. Gas is deliberately excluded from realized PnL. Hot-wallet custody and infrastructure failures are outside this bot's protection.

## Project files

Read `LIVE_READINESS_CHECKLIST.md` and complete every blocking gate before funded unattended execution. The bot is not live-certified. `AUDIT.md` records the static import audit: what was fixed, what was reviewed and found sound, and seven known issues deliberately left for the funded acceptance gates.


- `AUDIT.md`: static import audit — fixed defects, verified invariants, open issues
- `launch80/engine.py`: profit gate, independent exit and discovery loops, buy risk limits
- `launch80/exchange.py`: paper, 0x Robinhood and Jupiter Solana adapters with execution floor checks
- `launch80/storage.py`: SQLite state journal, mode lock, underwater and restart tracking
- `launch80/preflight.py`: read-only network, balance, token-decimal and quote readiness probes
- `launch80/server.py`: local monitoring and pause/resume controls
- `tests/`: deterministic regression tests for profit-only exits and uncertain execution
- `.env.example`, `compose.yaml`, `Dockerfile`: deployment configuration

## API references

- Robinhood Chain: https://docs.robinhood.com/chain/connecting/
- 0x supported chains: https://docs.0x.org/docs/introduction/supported-chains
- 0x price and minimum buy: https://docs.0x.org/docs/introduction/quickstart/swap-tokens-with-0x-swap-api
- Jupiter V2 order/execute: https://github.com/jup-ag/docs/blob/main/swap/order-and-execute.mdx
- Jupiter V1 Metis build/quote: https://developers.jup.ag/docs/guides/how-to-build-a-custom-swap-with-metis
- Garp metadata: https://www.usegarp.com/docs
- Pons launches: https://docs.ponsfamily.com/
- GeckoTerminal pools: https://api.geckoterminal.com/docs/index.html

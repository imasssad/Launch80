# Launch80 v1.1: Live-funds readiness and acceptance gates

## Current decision: NOT APPROVED for unattended real-money execution

This code has passing local/mock tests for profit-only exit, reboot recovery, risk pause,
rejection before execution and trading-mode separation. It has **not** been security-audited,
benchmarked on real liquidity or tested end-to-end with funded wallets on Robinhood Chain
and Solana. Treat it as a guarded staging release, not as a zero-loss trading system.

`LIVE=true` only constructs the live adapters. `BUY_ENABLED` defaults to false, and live
new purchases additionally require `LIVE_BUY_ACK=I_ENABLE_REAL_PURCHASES` and the distinct
`LIVE_ACK=I_ACCEPT_POSSIBLE_TOTAL_LOSS`. Keep the live wallet separate from paper-data volumes.

## Required sign-off gates

- [ ] Independent review of source, dependency lock, transitive supply chain and secret isolation.
- [ ] Dedicated disposable wallets funded for microtests only (never treasury wallets).
- [ ] Validate actual Robinhood Chain ID 4663, token contract/decimals, RPC provenance and gas behavior.
- [ ] Validate 0x AllowanceHolder/settlement target addresses against official live chain docs/receipts;
      never approve Settler. Test allowance reset and re-quote after approval.
- [ ] Validate Solana mainnet genesis, USDC mint, token program and wallet rent/fee coverage.
      Confirm the expected genesis hash with `solana genesis-hash --url mainnet-beta` and check it
      matches `SOLANA_MAINNET_GENESIS_HASH` in `launch80/config.py`. The imported code shipped the
      32-character CAIP-2 chain id here instead of the full 44-character hash, which made the
      mainnet guard unsatisfiable; see `AUDIT.md`. Never shorten this value to silence a mismatch.
- [ ] Confirm the Garp `chainId` comparison and the 0x quote token-echo gap in `AUDIT.md`
      ("Known issues NOT fixed") before enabling `GARP_REQUIRE_LAUNCHPAD` or scaling 0x size.
- [ ] Revalidate Jupiter's legacy Metis V1 sell path, min-output instruction and request format;
      upgrade to maintained V2 `/build` if appropriate before meaningful funds are used.
- [ ] Confirm that buy/sell balances, quote token units, taxes and gas are accurately reconciled.
- [ ] Confirm the chosen target profit exceeds **actual** buy + sell gas, priority fees and other
      overhead; 1.5% stablecoin proceeds margin alone is NOT a net-profit guarantee.
- [ ] Test $5K-liquidity filter against launch pools with withdrawn/out-of-range liquidity.
- [ ] Test buy and profitable sell for tiny amounts, then timed underwater holds and price recovery.
- [ ] Test quote deterioration between preview and firm order; confirm no loss-making sell is signed.
- [ ] Test restricted token transfers, LP rug, honeypot, dropped RPC, fee spikes and zero sale route.
- [ ] Test transaction submission timeout and crash immediately after send; confirm no blind replay.
- [ ] Test restart with real open and underwater holdings. Confirm fresh live database mode tag.
- [ ] Add independent quote watchdog / trade alert delivery, wallet balance reconciliation,
      onchain durable signature reconciliation, database backup and recovery runbook.
- [ ] Audit all HTTP controls, infrastructure access, provider rate limits and process supervision.
- [ ] Obtain operator sign-off on exposure, indefinite-hold possibility and emergency manual exit.

## Recommended staged execution

1. `LIVE=false`, `BUY_ENABLED=true`: several days of paper trading, review pool history and PnL.
2. `LIVE=true`, `BUY_ENABLED=false`: use dedicated wallets, paid RPC, API credentials and
   approved 0x address allowlists. Run `launch80 check` and inspect reported errors.
3. `LIVE=true`, `BUY_ENABLED=true`, `LIVE_BUY_ACK=I_ENABLE_REAL_PURCHASES`: *only after*
   independent security and operational sign-off. Start with just one chain and a nearly empty
   wallet; supervise each microtrade and independently confirm explorer receipts.
4. Increase position size and unattended hours only after formal acceptance. Set up
   automated notifications for `HOLD_UNDERWATER`, `MANUAL_EXIT`, `RECOVERY_SELL`,
   `RECOVERY_BUY` and `FEED_CIRCUIT_BREAKER`.

## Strategy limitations

The 80-second timer starts **after confirmed purchase**, then the bot checks the conservative
minimum sell proceeds. If below entry principal plus target and reserve it **holds**, requoting
every 12 seconds by default. This may lock capital indefinitely and can lose the entire stake.
A quote-minimum check prevents certain adverse fills, not all adverse outcomes: gas paid on
failed trades, token restrictions, rug pulls, provider malice, stablecoin depegs and protocol
bugs remain possible. For EVM and Solana, actual execution needs third-party testing to verify
that the claimed protected minimum really is enforced by the signed onchain transaction.

`launch80 pause` disables *new buys* and retains monitoring of held positions. For uncertain
transactions, never manually mark reconciliation complete without independent explorer and
balance verification. Do not delete/replace the database until all wallet movements reconcile.

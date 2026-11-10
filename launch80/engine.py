"""Restart-aware, bounded trade scheduler. Entries are optional, exits stay active."""
from __future__ import annotations
import asyncio
import logging
import time
from decimal import Decimal
from .config import Settings
from .discovery import GeckoPools
from .exchange import Exchange
from .models import Pool, RiskRejection, TradingError, UncertainExecution
from .storage import Store

LOG = logging.getLogger('launch80.engine')

class Engine:
    def __init__(self, config: Settings, store: Store, discovery: GeckoPools,
                 exchanges: dict[str,Exchange], garp=None):
        self.cfg, self.store, self.discovery, self.exchanges = config,store,discovery,exchanges
        self.garp = garp
        self.pending: dict[str,Pool] = {}
        self.feed_failures = 0
        self.last_poll = 0.0
        self._trade_lock = asyncio.Lock()
        self._exit_tasks: dict[str, asyncio.Task] = {}
        self._stopped = asyncio.Event()

    def eligible_size(self, pool: Pool, now=None) -> int:
        now = time.time() if now is None else now
        age = now-pool.created_at
        if age < self.cfg.min_age_seconds or age > self.cfg.max_age_seconds:
            raise RiskRejection('Pool age outside allowed entry window')
        if pool.liquidity_usd < self.cfg.min_liquidity_usd:
            raise RiskRejection('Pool below minimum USD liquidity')
        if self.store.get(pool.key):
            raise RiskRejection('Already attempted this pool (dedupe)')
        if self.store.flag('paused') == '1' or not self.cfg.buy_enabled:
            raise RiskRejection('New entries paused')
        if self.store.has_unknowns():
            raise RiskRejection('Unreconciled position or pending transaction exists')
        if self.store.active_count() >= self.cfg.max_open_positions:
            raise RiskRejection('Open position count limit')
        if self.store.hourly_attempts() >= self.cfg.max_buy_attempts_per_hour:
            raise RiskRejection('Hourly buy attempt rate limit')
        realized = Decimal(str(self.store.daily_realized_usd()))
        if realized <= -self.cfg.max_daily_realized_loss_usd:
            raise RiskRejection('Daily realized trading loss limit reached (excluding gas)')
        amount_usd = min(self.cfg.buy_usd,pool.liquidity_usd*self.cfg.max_buy_liquidity_fraction)
        if amount_usd < Decimal('1'):
            raise RiskRejection('Safe order size smaller than $1')
        if Decimal(str(self.store.exposure_usd()))+amount_usd > self.cfg.max_total_exposure_usd:
            raise RiskRejection('Total exposure cap')
        units = int(amount_usd * 1_000_000)
        if units <= 0:
            raise RiskRejection('Order too small')
        return units

    async def enter(self, pool: Pool) -> bool:
        async with self._trade_lock:
            try:
                amount=self.eligible_size(pool)
                backend = self.exchanges[pool.chain]
                if self.garp is not None:
                    source = await self.garp.enrich(pool)
                    self.store.log('GARP_SOURCE',pool.key,source)
                # Must be independently validated before money is put at risk.
                await backend.token_safety(pool)
                q_buy = await backend.quote(pool,'buy',amount)
                if q_buy.price_impact_bps is not None and q_buy.price_impact_bps > self.cfg.max_price_impact_bps:
                    raise RiskRejection('Buy price impact exceeds limit')
                # Indicative quote roundtrip only. Market can change within seconds.
                q_sell = await backend.quote(pool,'sell',q_buy.out_units)
                if q_sell.price_impact_bps is not None and q_sell.price_impact_bps > self.cfg.max_price_impact_bps:
                    raise RiskRejection('Sell price impact exceeds limit')
                if q_sell.out_units * 10000 < amount * (10000-self.cfg.max_roundtrip_cost_bps):
                    raise RiskRejection('Roundtrip quote too costly at current liquidity')
                # Recheck critical risk caps right before intent claim.
                self.eligible_size(pool)
                before = await backend.token_balance(pool)
                if not self.store.claim(pool,before,intended_spend_units=amount):
                    return False
                self.store.log('BUY_INTENT',pool.key,f'USD atomic units={amount}; live={self.cfg.live}')
                try:
                    fill=await backend.trade(pool,'buy',amount)
                except UncertainExecution as exc:
                    self.store.set_error(pool.key,'RECOVERY_BUY',self.cfg.scrub(exc))
                    self.store.set_flag('paused','1')
                    self.store.log('BUY_UNCERTAIN',pool.key,self.cfg.scrub(exc))
                    LOG.error('Buy uncertain for %s; new entries paused', pool.key)
                    return False
                except TradingError as exc:
                    self.store.set_error(pool.key,'FAILED_BUY',self.cfg.scrub(exc))
                    self.store.log('BUY_FAILED',pool.key,self.cfg.scrub(exc))
                    return False
                except Exception as exc:
                    # Unknown failure after intent; assume money might have moved.
                    self.store.set_error(pool.key,'RECOVERY_BUY',exc.__class__.__name__)
                    self.store.set_flag('paused','1')
                    self.store.log('BUY_UNCERTAIN',pool.key,'Unexpected error; manual reconciliation required')
                    LOG.exception('Unexpected buy error; entry disabled')
                    return False
                try:
                    self.store.open_position(pool.key,fill.spent_units,fill.received_units,
                                             fill.txid,self.cfg.hold_seconds,gas_units=fill.gas_units)
                except Exception:
                    self.store.set_flag('paused','1')
                    self.store.set_error(pool.key,'RECOVERY_BUY','Buy confirmed but journal update failed')
                    LOG.exception('Critical: buy confirmed but persistence failed')
                    return False
                self.store.log('BUY_CONFIRMED',pool.key,
                    f'tx={fill.txid}, token_units={fill.received_units}, earliest_profit_exit_in={self.cfg.hold_seconds}s')
                LOG.info('BUY %s %s $%.2f; first profit check in %ds',pool.chain,pool.symbol,amount/1e6,self.cfg.hold_seconds)
                return True
            except (RiskRejection, TradingError) as exc:
                # Stale/low-liquidity/untradeable candidates are not fatal to the engine.
                self.store.log('ENTRY_SKIPPED',pool.key,self.cfg.scrub(exc))
                return False
            except Exception as exc:
                self.store.log('ENTRY_ERROR',pool.key,exc.__class__.__name__)
                LOG.exception('Entry evaluation failed %s',pool.key)
                return False

    def min_exit_proceeds(self, spent_units: int) -> int:
        """Principal + profit target + bps buffer + flat gas reserve, in
        6-decimal stable units.

        The flat EXIT_GAS_RESERVE_USD term exists because network costs do not
        scale with trade size; it is an operator-set estimate, not an oracle,
        so this is still NOT true net USD PnL (actual gas, ATA rent and
        stablecoin depegs can differ). The venue's enforced minimum must
        reach this value before any sell is signed.
        """
        bps = 10000 + self.cfg.min_exit_profit_bps + self.cfg.exit_cost_buffer_bps
        gas_reserve = int(self.cfg.exit_gas_reserve_usd * 1_000_000)
        return (spent_units*bps + 9999)//10000 + gas_reserve

    def _hold_unprofitable(self, pool: Pool, current: dict, minimum: int,
                           conservative: int, reason: str = ''):
        row = self.store.defer_unprofitable(pool.key,conservative,
                            self.cfg.profit_recheck_seconds,minimum)
        if not row:
            return
        bps_loss = (max(0, int(current['buy_spent_units'])-conservative)*10000
                    // max(1,int(current['buy_spent_units'])))
        # A sustained hold or severe decline stops *new* buys, not forced sells.
        if bps_loss >= self.cfg.underwater_pause_bps and self.store.flag('paused') != '1':
            self.store.set_flag('paused','1')
            self.store.log('UNDERWATER_RISK_PAUSE',pool.key,
                           f'Conservative loss at least {bps_loss}bps; no forced liquidation')
        if (row['underwater_since'] and
            time.time()-row['underwater_since'] >= self.cfg.underwater_alert_seconds
            and not row['underwater_alerted']):
            self.store.mark_underwater_alerted(pool.key)
            self.store.log('UNDERWATER_LONG_HOLD',pool.key,
                    f'Below floor for {self.cfg.underwater_alert_seconds}s; manual review advised')
        # Keep audit log bounded during prolonged underwater holds.
        if row['underwater_checks'] in (1,5,20,100) or row['underwater_checks'] % 500 == 0:
            self.store.log('HOLD_UNDERWATER',pool.key,
                 f'conservative={conservative}, floor={minimum}, '
                 f'check={row["underwater_checks"]}; {reason}')

    async def exit(self,row: dict) -> bool:
        # 80 seconds is the earliest evaluation, NOT a forced market sale.
        current = self.store.get(row['key'])
        if not current or current['status'] != 'OPEN':
            return False
        backend = self.exchanges[current['chain']]
        pool = Pool(current['chain'],current['pool'],current['mint'],current['symbol'],
                    current['dex'],Decimal(current['liquidity_usd']),current['created_at'])
        intended = int(current['bought_units'])
        if current['sell_attempts'] >= self.cfg.max_exit_attempts:
            self.store.set_error(pool.key,'MANUAL_EXIT','Automated sell retries exhausted; human review required')
            self.store.set_flag('paused','1')
            self.store.log('MANUAL_EXIT',pool.key,'Automated sell retries exhausted')
            return False
        if intended <= 0 or int(current['buy_spent_units']) <= 0:
            self.store.set_error(pool.key,'RECOVERY_SELL','Missing confirmed fill information')
            self.store.set_flag('paused','1')
            return False
        try:
            if self.cfg.live:
                balance = await backend.token_balance(pool)
                # Compare against the pre-buy baseline PLUS the recorded fill:
                # a pre-existing holding of the same token must never mask a
                # missing position, or the sell would liquidate tokens this
                # position never bought.
                baseline = int(current['before_token_balance'] or 0)
                if balance < baseline + intended:
                    raise UncertainExecution('Token balance below pre-buy baseline plus recorded position; reconcile before exit')
            minimum = self.min_exit_proceeds(int(current['buy_spent_units']))
            preview = await backend.quote(pool,'sell',intended)
            if preview.out_units <= 0:
                raise TradingError('Exit quote unavailable')
            if preview.min_out_units is None or preview.min_out_units <= 0:
                raise TradingError('No venue-backed minimum sell proceeds; refusing unprotected execution')
            if preview.min_out_units < minimum:
                self._hold_unprofitable(pool,current,minimum,preview.min_out_units)
                return False
            self.store.record_acceptable_exit_quote(pool.key,preview.min_out_units)
        except UncertainExecution as exc:
            self.store.set_error(pool.key,'RECOVERY_SELL',self.cfg.scrub(exc))
            self.store.set_flag('paused','1')
            self.store.log('SELL_UNCERTAIN',pool.key,self.cfg.scrub(exc))
            return False
        except Exception as exc:
            # No sale initiated. Missing quote/route is NOT a sell attempt.
            delay = min(self.cfg.max_exit_retry_seconds,self.cfg.profit_recheck_seconds)
            self.store.defer_quote_unavailable(pool.key,exc.__class__.__name__+': '+self.cfg.scrub(exc),delay)
            self.store.log('SELL_QUOTE_RETRY',pool.key,f'{exc.__class__.__name__}; retry_in={delay}s')
            return False
        if not self.store.mark_selling(pool.key):
            return False
        self.store.log('SELL_INTENT',pool.key,f'token_units={intended}; minimum_stable_atomic={minimum}')
        try:
            # Critical: get a NEW executable quote and reject if its slippage-
            # protected minimum output is now below the original principal/floor.
            fill = await backend.trade(pool,'sell',intended,min_output_units=minimum)
        except RiskRejection as exc:
            # Explicitly rejected BEFORE sending swap tx; do not liquidate.
            self.store.reset_sell_after_price_change(pool.key,self.cfg.scrub(exc),self.cfg.profit_recheck_seconds)
            self.store.log('HOLD_PRICE_MOVED',pool.key,self.cfg.scrub(exc))
            return False
        except UncertainExecution as exc:
            self.store.set_error(pool.key,'RECOVERY_SELL',self.cfg.scrub(exc))
            self.store.set_flag('paused','1')
            self.store.log('SELL_UNCERTAIN',pool.key,self.cfg.scrub(exc))
            LOG.error('Exit uncertain for %s; manual reconciliation needed',pool.key)
            return False
        except TradingError as exc:
            delay = min(self.cfg.max_exit_retry_seconds,max(3,2**min(current['sell_attempts'],5)))
            self.store.retry_sell(pool.key,self.cfg.scrub(exc),delay)
            self.store.log('SELL_RETRY',pool.key,f'{self.cfg.scrub(exc)}; retry_in={delay}s')
            return False
        except Exception:
            self.store.set_error(pool.key,'RECOVERY_SELL','Unexpected execution error; reconcile')
            self.store.set_flag('paused','1')
            LOG.exception('Exit uncertain; requires reconciliation')
            return False
        try:
            # If provider reports below-threshold proceeds, quarantine instead
            # of silently pretending the profit-only invariant held.
            if fill.received_units < minimum:
                raise UncertainExecution(f'Sell proceeded below requested floor: {fill.received_units} < {minimum}; tx={fill.txid}')
            self.store.close_position(pool.key,fill.received_units,fill.txid,gas_units=fill.gas_units)
        except Exception:
            self.store.set_flag('paused','1')
            self.store.set_error(pool.key,'RECOVERY_SELL',
                f'Sell confirmed; inspect tx={fill.txid}, received={fill.received_units}, floor={minimum}')
            LOG.exception('Critical: sell confirmed but proceeds/journal not reconciled')
            return False
        result=(fill.received_units-int(current['buy_spent_units']))/1e6
        self.store.log('SELL_CONFIRMED',pool.key,
          f'tx={fill.txid}; trading_PnL_stable={result:.4f}, native gas excluded')
        LOG.info('SELL %s %s trading PnL %.4f stable units (gas excluded)',pool.chain,pool.symbol,result)
        if Decimal(str(self.store.daily_realized_usd())) <= -self.cfg.max_daily_realized_loss_usd:
            self.store.set_flag('paused','1')
            self.store.log('LOSS_LIMIT','','Daily trading loss cap reached; no new buys')
        return True

    async def poll(self):
        self.last_poll=time.time()
        chains=list(self.exchanges)
        results = await asyncio.gather(*[self.discovery.fetch(chain) for chain in chains],return_exceptions=True)
        good=0
        for chain,result in zip(chains,results):
            if isinstance(result,Exception):
                LOG.warning('Discovery feed failed for %s: %s',chain,result.__class__.__name__)
                self.store.log('FEED_ERROR','',chain)
                continue
            good += 1
            for pool in result:
                if pool.liquidity_usd >= self.cfg.min_liquidity_usd and not self.store.get(pool.key):
                    self.pending[pool.key] = pool
        if good != len(chains):
            self.feed_failures += 1
        else:
            self.feed_failures = 0
        if self.feed_failures >= self.cfg.max_consecutive_feed_failures:
            self.store.set_flag('paused','1')
            self.store.log('FEED_CIRCUIT_BREAKER','','New entries paused due to feed failures')
        now = time.time()
        self.pending = {k:p for k,p in self.pending.items()
                        if now-p.created_at <= self.cfg.max_age_seconds and not self.store.get(k)}
        # Queue bound: only youngest 120 candidates; prioritise liquidity after aging.
        if len(self.pending) > 120:
            newest=sorted(self.pending.values(),key=lambda p:p.created_at,reverse=True)[:120]
            self.pending={p.key:p for p in newest}

    async def _poll_loop(self):
        while not self._stopped.is_set():
            try:
                await self.poll()
            except Exception:
                self.feed_failures+=1
                self.store.log('FEED_ERROR','','Unhandled market data error')
                LOG.exception('Poll loop failure')
            if self.feed_failures >= self.cfg.max_consecutive_feed_failures:
                self.store.set_flag('paused','1')
            try:
                await asyncio.wait_for(self._stopped.wait(),timeout=self.cfg.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _entry_loop(self):
        while not self._stopped.is_set():
            # Give exits precedence over entries when both are due.
            if self.store.flag('paused') != '1' and self.cfg.buy_enabled and not self.store.due(time.time()):
                aged=sorted(self.pending.values(),key=lambda p:(-p.liquidity_usd,p.created_at))
                for p in aged[:4]:
                    if time.time()-p.created_at >= self.cfg.min_age_seconds:
                        self.pending.pop(p.key,None)
                        await self.enter(p)
                        break
            try:
                # Entry pacing is separate from the exit loop's fixed tick, so
                # slowing entries never delays an exit. Each entry evaluation
                # costs several price-feed requests, so a fast tick will
                # exhaust a rate-limited feed and starve the exit quotes that
                # actually matter.
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=self.cfg.entry_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _exit_loop(self):
        while not self._stopped.is_set():
            for key,task in list(self._exit_tasks.items()):
                if task.done():
                    try: task.result()
                    except Exception:
                        self.store.set_flag('paused','1')
                        self.store.log('EXIT_WORKER_ERROR',key,'Unexpected exit worker failure')
                        LOG.exception('Critical: exit worker error for %s',key)
                    self._exit_tasks.pop(key,None)
            for row in self.store.due(time.time(),limit=25):
                if row['key'] not in self._exit_tasks:
                    self._exit_tasks[row['key']] = asyncio.create_task(self.exit(row))
            try:
                await asyncio.wait_for(self._stopped.wait(),timeout=0.5)
            except asyncio.TimeoutError:
                pass
        # Don't cancel an in-flight signed transaction during normal shutdown.
        if self._exit_tasks:
            await asyncio.gather(*self._exit_tasks.values(),return_exceptions=True)

    async def run(self):
        self.store.verify_mode('live' if self.cfg.live else 'paper')
        self.store.pending_on_restart()
        if self.store.has_unknowns():
            self.store.set_flag('paused','1')
            self.store.log('RECOVERY_HALT','','Pending trade after restart: reconcile before resuming buys')
        for ex in self.exchanges.values():
            if hasattr(ex,'validate_network'):
                await ex.validate_network()
        self.store.log('ENGINE_START','',f'live={self.cfg.live}; hold={self.cfg.hold_seconds}s')
        try:
            await asyncio.gather(self._exit_loop(),self._poll_loop(),self._entry_loop())
        finally:
            self.store.log('ENGINE_STOP')

    def stop(self):
        self._stopped.set()

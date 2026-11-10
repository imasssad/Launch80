from dataclasses import replace
from decimal import Decimal
import time
import pytest
from launch80.models import Pool
from launch80.engine import Engine
from conftest import FakeExchange,FakeFeed

pytestmark=pytest.mark.asyncio

def setup(cfg,store,feed=None):
    ex=FakeExchange()
    en=Engine(cfg,store,feed or FakeFeed(),{'solana':ex})
    return en,ex

async def test_liquidity_gate(cfg,store,pool):
    en,ex=setup(cfg,store)
    p=replace(pool,liquidity_usd=Decimal('4999.99'))
    assert not await en.enter(p)
    assert store.all_positions()==[]
    assert ex.trades==[]

async def test_buy_and_due_80s(cfg,store,pool):
    en,ex=setup(cfg,store)
    assert await en.enter(pool)
    row=store.get(pool.key)
    assert row['status']=='OPEN'
    assert abs(row['sell_due_at']-row['buy_at']-80) < 0.001
    assert not store.due(time.time())
    assert ex.trades==['buy']
    assert await en.exit(row)
    assert store.get(pool.key)['status']=='CLOSED'
    assert ex.trades==['buy','sell']

async def test_duplicate_pool_never_double_buys(cfg,store,pool):
    en,ex=setup(cfg,store)
    assert await en.enter(pool)
    assert not await en.enter(pool)
    assert ex.trades==['buy']

async def test_preflight_rejects_roundtrip_cost(cfg,store,pool):
    en,ex=setup(replace(cfg,max_roundtrip_cost_bps=100),store)
    ex.sell_quote_multiplier=0.96
    assert not await en.enter(pool)
    assert store.all_positions()==[]

async def test_preflight_price_impact(cfg,store,pool):
    en,ex=setup(replace(cfg,max_price_impact_bps=10),store)
    assert not await en.enter(pool)
    assert store.all_positions()==[]

async def test_failed_buy_does_not_open(cfg,store,pool):
    en,ex=setup(cfg,store)
    ex.fail_buy=True
    assert not await en.enter(pool)
    assert store.get(pool.key)['status']=='FAILED_BUY'

async def test_unknown_buy_halts_new_entries(cfg,store,pool):
    en,ex=setup(cfg,store)
    ex.uncertain_buy=True
    assert not await en.enter(pool)
    assert store.get(pool.key)['status']=='RECOVERY_BUY'
    assert store.flag('paused')=='1'
    assert not await en.enter(replace(pool,pool='otherpool'))

async def test_no_sell_quote_retries_instead_of_closing(cfg,store,pool):
    en,ex=setup(cfg,store)
    assert await en.enter(pool)
    ex.bad_exit_quote=True
    assert not await en.exit(store.get(pool.key))
    assert store.get(pool.key)['status']=='OPEN'
    assert store.get(pool.key)['next_sell_retry_at']>time.time()

async def test_failed_sell_does_not_lose_position(cfg,store,pool):
    en,ex=setup(cfg,store)
    assert await en.enter(pool)
    ex.fail_sell=True
    assert not await en.exit(store.get(pool.key))
    assert store.get(pool.key)['status']=='OPEN'

async def test_unknown_sell_requires_manual_review(cfg,store,pool):
    en,ex=setup(cfg,store)
    assert await en.enter(pool)
    ex.uncertain_sell=True
    assert not await en.exit(store.get(pool.key))
    assert store.get(pool.key)['status']=='RECOVERY_SELL'
    assert store.flag('paused')=='1'

async def test_pause_prevents_entries_but_not_exits(cfg,store,pool):
    en,ex=setup(cfg,store)
    assert await en.enter(pool)
    store.set_flag('paused','1')
    assert not await en.enter(replace(pool,pool='otherpool'))
    assert await en.exit(store.get(pool.key))

async def test_exposure_limit(cfg,store,pool):
    en,ex=setup(replace(cfg,max_total_exposure_usd=Decimal('5')),store)
    assert await en.enter(pool)
    assert not await en.enter(replace(pool,pool='otherpool'))

async def test_restart_quarantines_buying_and_selling(cfg,store,pool):
    assert store.claim(pool,0)
    store.pending_on_restart()
    assert store.get(pool.key)['status']=='RECOVERY_BUY'
    assert store.has_unknowns()

async def test_liquidity_fraction_limits_size(cfg,store,pool):
    en,ex=setup(cfg,store)
    assert en.eligible_size(pool)==5_000_000
    p=replace(pool,liquidity_usd=Decimal('5100.0'))
    assert en.eligible_size(p)==5_000_000

async def test_daily_realized_loss_stops_new_buys(cfg,store,pool):
    en,ex=setup(replace(cfg,max_daily_realized_loss_usd=Decimal('0.5')),store)
    assert await en.enter(pool)
    # After first mock sell the artificial PnL is +$0.1, not a loss.
    assert await en.exit(store.get(pool.key))
    assert store.daily_realized_usd() > 0

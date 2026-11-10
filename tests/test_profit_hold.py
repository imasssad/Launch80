"""Regression tests for profit-only exit invariants and indefinite underwater holds."""
from dataclasses import replace
from decimal import Decimal
import asyncio
import time
import sqlite3
import pytest
from launch80.engine import Engine
from launch80.models import Quote, Fill, RiskRejection
from launch80.exchange import RobinhoodExchange, SolanaExchange
from conftest import FakeFeed, FakeExchange

pytestmark=pytest.mark.asyncio


def engine(cfg, store):
    ex = FakeExchange()
    return Engine(cfg,store,FakeFeed(),{'solana':ex}),ex


async def test_underwater_after_timer_remains_open_and_rechecks(cfg,store,pool):
    en,ex=engine(cfg,store)
    assert await en.enter(pool)
    ex.sell_quote_multiplier=0.80
    assert not await en.exit(store.get(pool.key))
    r=store.get(pool.key)
    assert r['status']=='OPEN'
    assert r['underwater_checks']==1
    assert r['underwater_since']>0
    assert r['last_exit_quote_units'] is not None
    assert r['next_sell_retry_at']>time.time()
    assert r['sell_attempts']==0
    assert store.active_count()==1
    assert store.exposure_usd()==5
    assert ex.trades==['buy']
    assert not store.due(time.time())


async def test_recovers_price_later_and_sells_at_profit(cfg,store,pool):
    en,ex=engine(cfg,store)
    assert await en.enter(pool)
    ex.sell_quote_multiplier=0.65
    assert not await en.exit(store.get(pool.key))
    # In the real service, the exit scheduler calls due() after this interval.
    store.db.execute('UPDATE positions SET next_sell_retry_at=?,sell_due_at=? WHERE key=?',
                     (time.time()-1,time.time()-1,pool.key))
    assert store.due(time.time())
    ex.sell_quote_multiplier=1.045
    assert await en.exit(store.get(pool.key))
    r=store.get(pool.key)
    assert r['status']=='CLOSED'
    assert r['underwater_since'] is None
    assert r['sell_received_units'] >= en.min_exit_proceeds(r['buy_spent_units'])
    assert store.active_count()==0
    assert ex.trades==['buy','sell']


async def test_unprofitable_hold_survives_restart(cfg,store,pool):
    en,ex=engine(cfg,store)
    assert await en.enter(pool)
    ex.sell_quote_multiplier=0.80
    await en.exit(store.get(pool.key))
    before=store.get(pool.key)
    store.pending_on_restart()
    after=store.get(pool.key)
    assert after['status']=='OPEN'
    assert after['next_sell_retry_at']==before['next_sell_retry_at']
    assert after['underwater_checks']==1
    assert not store.has_unknowns()


async def test_severe_unrealized_loss_pauses_entries_without_forcing_sell(cfg,store,pool):
    en,ex=engine(cfg,store)
    assert await en.enter(pool)
    ex.sell_quote_multiplier=0.5
    assert not await en.exit(store.get(pool.key))
    assert store.flag('paused')=='1'
    assert store.get(pool.key)['status']=='OPEN'
    assert 'sell' not in ex.trades
    assert any(e['kind']=='UNDERWATER_RISK_PAUSE' for e in store.events())


async def test_long_underwater_hold_alerts_once(cfg,store,pool):
    en,ex=engine(replace(cfg,underwater_alert_seconds=30),store)
    assert await en.enter(pool)
    ex.sell_quote_multiplier=0.95
    assert not await en.exit(store.get(pool.key))
    store.db.execute('UPDATE positions SET underwater_since=? WHERE key=?',(time.time()-40,pool.key))
    assert not await en.exit(store.get(pool.key))
    assert store.get(pool.key)['underwater_alerted']==1
    assert len([e for e in store.events() if e['kind']=='UNDERWATER_LONG_HOLD'])==1
    assert not await en.exit(store.get(pool.key))
    assert len([e for e in store.events() if e['kind']=='UNDERWATER_LONG_HOLD'])==1


async def test_missing_protected_minimum_fails_closed_without_attempt(cfg,store,pool):
    en,ex=engine(cfg,store)
    assert await en.enter(pool)
    async def missing_minimum(p,side,amount):
        if side=='sell': return Quote(10_000_000,20,None)
        return Quote(amount*1000,20)
    ex.quote=missing_minimum
    assert not await en.exit(store.get(pool.key))
    assert store.get(pool.key)['status']=='OPEN'
    assert store.get(pool.key)['sell_attempts']==0
    assert ex.trades==['buy']


async def test_firm_price_drops_after_preview_keep_open(cfg,store,pool):
    en,ex=engine(cfg,store)
    assert await en.enter(pool)
    original=ex.trade
    async def firm_drops(p,side,amount,min_output_units=None):
        if side=='sell': raise RiskRejection('Firm quote now below entry price')
        return await original(p,side,amount,min_output_units)
    ex.trade=firm_drops
    assert not await en.exit(store.get(pool.key))
    r=store.get(pool.key)
    assert r['status']=='OPEN'
    assert r['sell_attempts']==0
    assert r['next_sell_retry_at']>time.time()
    assert ex.trades==['buy']


async def test_provider_reports_under_floor_quarantines(cfg,store,pool):
    en,ex=engine(cfg,store)
    assert await en.enter(pool)
    original=ex.trade
    async def dishonest_backend(p,side,amount,min_output_units=None):
        if side=='sell': return Fill('MOCK-REAL-LOW',amount,amount//1000)
        return await original(p,side,amount,min_output_units)
    ex.trade=dishonest_backend
    assert not await en.exit(store.get(pool.key))
    r=store.get(pool.key)
    assert r['status']=='RECOVERY_SELL'
    assert 'MOCK-REAL-LOW' in r['last_error']
    assert store.flag('paused')=='1'
    assert store.has_unknowns()


async def test_exit_floor_integer_rounding_and_limits(cfg,store):
    en,_=engine(cfg,store)
    assert en.min_exit_proceeds(5_000_000)==5_075_000
    assert en.min_exit_proceeds(1)==2
    with pytest.raises(ValueError,match='MIN_EXIT_PROFIT_BPS'):
        replace(cfg,min_exit_profit_bps=-1).validate()
    with pytest.raises(ValueError,match='EXIT_COST_BUFFER_BPS'):
        replace(cfg,exit_cost_buffer_bps=5001).validate()


async def test_robinhood_firm_rejects_loss_before_sending(cfg,pool):
    from launch80.models import Pool
    e=RobinhoodExchange.__new__(RobinhoodExchange)
    e.lock=asyncio.Lock()
    e.cfg=cfg
    e.allowed_spenders=set()
    e.allowed_targets=set()
    e._asset=lambda pool,side: ('0x111','0x222')
    e._balance=lambda token: 0
    async def firm(pool,side,amount,firm):
        return {'liquidityAvailable':True,'minBuyAmount':'1000000','buyAmount':'1100000'}
    e._zero_x=firm
    async def no_send(*args,**kwargs):
        raise AssertionError('No transaction should be sent at loss')
    e._send=no_send
    mockpool=Pool('robinhood','0xabc','0xdef','T','dex',Decimal('5000'),time.time())
    with pytest.raises(RiskRejection,match='below profit floor'):
        await e.trade(mockpool,'sell',100_000,min_output_units=5_075_000)


async def test_solana_quote_guard_refuses_unbounded_sell(cfg,pool):
    e=SolanaExchange.__new__(SolanaExchange)
    e.cfg=cfg
    e.wallet='fake-wallet'
    async def low_order(p,side,amount,execute):
        return {'requestId':'r','transaction':'t','inputMint':pool.mint,
                'outputMint':cfg.solana_usdc,'inAmount':'500000',
                'otherAmountThreshold':'100'}
    e._order=low_order
    async def no_balance():
        raise AssertionError('Should reject before token balance lookup')
    e._stable_balance=no_balance
    with pytest.raises(RiskRejection,match='below profit floor'):
        await e._sell_with_minimum(pool,500_000,5_075_000)


async def test_schema_migration_adds_underwater_fields(tmp_path):
    # An existing SQLite DB with original columns is augmented in-place.
    from launch80.storage import Store
    path=tmp_path/'legacy.sqlite'
    conn=sqlite3.connect(str(path))
    conn.execute('CREATE TABLE positions(key TEXT PRIMARY KEY,status TEXT,sell_due_at REAL)')
    conn.commit();conn.close()
    s=Store(str(path))
    cols={x['name'] for x in s.db.execute('PRAGMA table_info(positions)')}
    assert {'underwater_since','last_exit_quote_units','underwater_checks','underwater_alerted'}<=cols
    s.close()

async def test_mode_lock_fails_closed_for_paper_to_live(store):
    store.verify_mode('paper')
    with pytest.raises(RuntimeError,match='refusing live'):
        store.verify_mode('live')

async def test_legacy_db_refuses_implicit_live_conversion(store,pool):
    assert store.claim(pool,0)
    with pytest.raises(RuntimeError,match='Legacy database'):
        store.verify_mode('live')

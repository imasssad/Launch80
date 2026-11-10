"""Regression tests for the second (line-by-line) audit pass."""
import asyncio
import time
from dataclasses import replace
from decimal import Decimal

import pytest

from launch80.discovery import parse_pool
from launch80.engine import Engine
from launch80.exchange import RobinhoodExchange, SolanaExchange
from launch80.models import Pool, RiskRejection
from conftest import FakeExchange, FakeFeed


def engine(cfg, store):
    ex = FakeExchange()
    return Engine(cfg, store, FakeFeed(), {'solana': ex}), ex


# --- Feed data cannot rewrite request URLs ----------------------------------

def _raw(pool_address):
    base = '0xabcde00000000000000000000000000000012345'
    return {'id': 'robinhood_' + pool_address,
            'attributes': {'reserve_in_usd': '6000', 'pool_created_at': '2026-11-09T00:00:00Z',
                           'address': pool_address, 'name': 'X / USDG'},
            'relationships': {'base_token': {'data': {'id': 'robinhood_' + base}},
                              'quote_token': {'data': {'id': 'robinhood_0x5fc5360d0400a0fd4f2af552add042d716f1d168'}},
                              'dex': {'data': {'id': 'uniswap-v4'}}}}


@pytest.mark.parametrize('address', [
    '0x123/../../networks/evil', '0x123?page=9', '0x123#frag', 'a%2Fb', 'x' * 65, ''])
def test_pool_address_with_url_metacharacters_rejected(address):
    # pool.pool is interpolated into URL paths (paper spot lookups); anything
    # beyond plain alphanumerics must never become a Pool.
    assert parse_pool(_raw(address), {}, 'robinhood') is None


def test_plain_alphanumeric_pool_address_accepted():
    pool = parse_pool(_raw('0xAbC123'), {}, 'robinhood')
    assert pool is not None and pool.pool == '0xAbC123'


# --- Live exit never dips into pre-existing holdings ------------------------

async def test_live_exit_short_balance_quarantines(cfg, store, pool):
    en, ex = engine(replace(cfg, live=True), store)
    assert await en.enter(pool)
    intended = int(store.get(pool.key)['bought_units'])

    async def short(p):
        return intended - 1
    ex.token_balance = short
    assert not await en.exit(store.get(pool.key))
    assert store.get(pool.key)['status'] == 'RECOVERY_SELL'
    assert store.flag('paused') == '1'


async def test_live_exit_counts_prebuy_baseline(cfg, store, pool):
    # Wallet held 500 units before the buy. If the bought tokens are gone, a
    # total balance equal to `intended` must NOT pass: selling would burn the
    # pre-existing holding, not the position.
    en, ex = engine(replace(cfg, live=True), store)
    assert await en.enter(pool)
    store.db.execute("UPDATE positions SET before_token_balance='500' WHERE key=?", (pool.key,))
    intended = int(store.get(pool.key)['bought_units'])

    async def total(p):
        return intended  # baseline drained; only `intended` remains
    ex.token_balance = total
    assert not await en.exit(store.get(pool.key))
    assert store.get(pool.key)['status'] == 'RECOVERY_SELL'
    assert 'sell' not in ex.trades


async def test_live_exit_proceeds_with_baseline_plus_position(cfg, store, pool):
    en, ex = engine(replace(cfg, live=True), store)
    assert await en.enter(pool)
    store.db.execute("UPDATE positions SET before_token_balance='500' WHERE key=?", (pool.key,))
    intended = int(store.get(pool.key)['bought_units'])

    async def total(p):
        return 500 + intended
    ex.token_balance = total
    assert await en.exit(store.get(pool.key))
    assert store.get(pool.key)['status'] == 'CLOSED'


# --- In-flight buys count toward exposure ------------------------------------

async def test_inflight_buy_counts_toward_exposure(cfg, store, pool):
    assert store.claim(pool, 0, intended_spend_units=5_000_000)
    assert store.exposure_usd() == 5
    store.pending_on_restart()  # BUYING -> RECOVERY_BUY keeps the exposure
    assert store.exposure_usd() == 5
    store.reconcile_failed_buy(pool.key, 'operator verified no fill')
    assert store.exposure_usd() == 0


async def test_open_position_replaces_intent_with_actual_fill(cfg, store, pool):
    assert store.claim(pool, 0, intended_spend_units=5_000_000)
    store.open_position(pool.key, 4_900_000, 10, 'tx', 80)
    assert store.get(pool.key)['buy_spent_units'] == 4_900_000


# --- Venue token-echo validation ---------------------------------------------

def _robinhood(cfg):
    e = RobinhoodExchange.__new__(RobinhoodExchange)
    e.lock = asyncio.Lock()
    e.cfg = cfg
    e.allowed_spenders = set()
    e.allowed_targets = {'0xtarget'}
    e._asset = lambda pool, side: ('0x111', '0x222')
    e._balance = lambda token: 0

    async def no_send(*args, **kwargs):
        raise AssertionError('No transaction may be sent for a mismatched pair')
    e._send = no_send
    return e


def _mockpool():
    return Pool('robinhood', '0xabc', '0xdef', 'T', 'dex', Decimal('5000'), time.time())


async def test_robinhood_rejects_wrong_selltoken_echo(cfg):
    e = _robinhood(cfg)

    async def firm(pool, side, amount, firm):
        return {'liquidityAvailable': True, 'buyAmount': '1', 'sellAmount': '100000',
                'sellToken': '0x999', 'buyToken': '0x222'}
    e._zero_x = firm
    with pytest.raises(RiskRejection, match='sellToken'):
        await e.trade(_mockpool(), 'buy', 100_000)


async def test_robinhood_rejects_wrong_buytoken_echo(cfg):
    e = _robinhood(cfg)

    async def firm(pool, side, amount, firm):
        return {'liquidityAvailable': True, 'buyAmount': '1', 'sellAmount': '100000',
                'sellToken': '0x111', 'buyToken': '0x999'}
    e._zero_x = firm
    with pytest.raises(RiskRejection, match='buyToken'):
        await e.trade(_mockpool(), 'buy', 100_000)


async def test_solana_buy_order_mint_echo_mismatch_rejected(cfg, pool):
    e = SolanaExchange.__new__(SolanaExchange)
    e.cfg = cfg
    e.wallet = 'w'
    e.signer = None
    e.lock = asyncio.Lock()

    async def rpc(method, params):
        return {'value': 10 ** 9}
    e._rpc = rpc

    async def order(pool, side, amount, execute):
        return {'requestId': 'r', 'transaction': 't',
                'inputMint': cfg.solana_usdc, 'outputMint': 'WrongMint1111111111111111111111111111111111'}
    e._order = order

    def must_not_sign(*args):
        raise AssertionError('Mismatched order must never be signed')
    e._sign_transaction = must_not_sign
    with pytest.raises(RiskRejection, match='output mint'):
        await e.trade(pool, 'buy', 1_000_000)


async def test_solana_buy_order_amount_echo_mismatch_rejected(cfg, pool):
    e = SolanaExchange.__new__(SolanaExchange)
    e.cfg = cfg
    e.wallet = 'w'
    e.signer = None
    e.lock = asyncio.Lock()

    async def rpc(method, params):
        return {'value': 10 ** 9}
    e._rpc = rpc

    async def order(p, side, amount, execute):
        return {'requestId': 'r', 'transaction': 't',
                'inputMint': cfg.solana_usdc, 'outputMint': pool.mint, 'inAmount': '999'}
    e._order = order
    e._sign_transaction = lambda *a: pytest.fail('must not sign')
    with pytest.raises(RiskRejection, match='input amount'):
        await e.trade(pool, 'buy', 1_000_000)


# --- RPC provider check matches the hostname, not the whole URL ---------------

def _live_robinhood(cfg, rpc):
    return replace(cfg, live=True, live_ack='I_ACCEPT_POSSIBLE_TOTAL_LOSS',
                   live_buy_ack='I_ENABLE_REAL_PURCHASES', enable_solana=False,
                   zero_x_key='k', evm_private_key='k',
                   approved_allowance_spenders=('0xaaa',), approved_tx_targets=('0xbbb',),
                   robinhood_rpc=rpc)


def test_provider_name_in_path_does_not_pass(cfg):
    with pytest.raises(ValueError, match='production Robinhood RPC'):
        _live_robinhood(cfg, 'https://evil.example/alchemy').validate()


def test_provider_name_in_hostname_passes(cfg):
    _live_robinhood(cfg, 'https://robinhood-mainnet.g.alchemy.com/v2/key').validate()

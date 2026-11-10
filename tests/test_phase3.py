"""Phase-3 fixes: written as failing tests first, then implemented.

Covers: gas-aware exit floor, Jupiter V2 sell migration, on-chain reconcile
verification, operator write-off (ABANDONED), and secret scrubbing.
"""
import asyncio
import time
from dataclasses import replace
from decimal import Decimal

import pytest

from launch80.config import Settings
from launch80.engine import Engine
from launch80.exchange import SolanaExchange
from launch80.models import Pool, RiskRejection, TradingError, UncertainExecution
from conftest import FakeExchange, FakeFeed


def engine(cfg, store):
    ex = FakeExchange()
    return Engine(cfg, store, FakeFeed(), {'solana': ex}), ex


# --- F1: gas-aware exit floor ------------------------------------------------

def test_default_exit_gas_reserve_is_nonzero(monkeypatch):
    monkeypatch.delenv('EXIT_GAS_RESERVE_USD', raising=False)
    assert Settings.load().exit_gas_reserve_usd == Decimal('0.10')


def test_floor_includes_flat_gas_reserve(cfg, store):
    en, _ = engine(replace(cfg, exit_gas_reserve_usd=Decimal('0.10')), store)
    # 5.000000 principal * 101.5% + 0.100000 flat reserve
    assert en.min_exit_proceeds(5_000_000) == 5_075_000 + 100_000


def test_gas_reserve_zero_keeps_pure_bps_floor(cfg, store):
    en, _ = engine(replace(cfg, exit_gas_reserve_usd=Decimal('0')), store)
    assert en.min_exit_proceeds(5_000_000) == 5_075_000


@pytest.mark.parametrize('bad', ['-0.01', '101'])
def test_gas_reserve_bounds(cfg, bad):
    with pytest.raises(ValueError, match='EXIT_GAS_RESERVE_USD'):
        replace(cfg, exit_gas_reserve_usd=Decimal(bad)).validate()


async def test_exit_holds_when_quote_covers_bps_but_not_gas(cfg, store, pool):
    # min_out ~= 5.148; clears the 5.075 bps floor but NOT 5.075 + 0.10 gas.
    en, ex = engine(replace(cfg, exit_gas_reserve_usd=Decimal('0.10')), store)
    assert await en.enter(pool)
    assert not await en.exit(store.get(pool.key))
    assert store.get(pool.key)['status'] == 'OPEN'
    assert ex.trades == ['buy']


# --- F2: Jupiter V2 sell path with enforced threshold -------------------------

def _solana(cfg):
    e = SolanaExchange.__new__(SolanaExchange)
    e.cfg = cfg
    e.wallet = 'w'
    e.signer = None
    e.lock = asyncio.Lock()

    async def rpc(method, params):
        return {'value': 10 ** 9}
    e._rpc = rpc
    return e


async def test_v2_sell_below_floor_rejected_before_signing(cfg, pool):
    e = _solana(cfg)

    async def order(p, side, amount, execute):
        assert side == 'sell' and execute
        return {'requestId': 'r', 'transaction': 't', 'inputMint': pool.mint,
                'outputMint': cfg.solana_usdc, 'inAmount': str(amount),
                'otherAmountThreshold': '100'}
    e._order = order
    e._sign_transaction = lambda *a: pytest.fail('must not sign a below-floor sell')
    with pytest.raises(RiskRejection, match='below profit floor'):
        await e.trade(pool, 'sell', 500_000, min_output_units=5_075_000)


async def test_v2_sell_missing_threshold_rejected(cfg, pool):
    e = _solana(cfg)

    async def order(p, side, amount, execute):
        return {'requestId': 'r', 'transaction': 't', 'inputMint': pool.mint,
                'outputMint': cfg.solana_usdc, 'inAmount': str(amount)}
    e._order = order
    e._sign_transaction = lambda *a: pytest.fail('must not sign without a threshold')
    with pytest.raises(RiskRejection, match='below profit floor'):
        await e.trade(pool, 'sell', 500_000, min_output_units=5_075_000)


async def test_v2_sell_executes_and_measures_balance_delta(cfg, pool):
    e = _solana(cfg)
    balances = iter([1_000_000, 6_200_000])

    async def stable():
        return next(balances)
    e._stable_balance = stable

    async def order(p, side, amount, execute):
        return {'requestId': 'r', 'transaction': 't', 'inputMint': pool.mint,
                'outputMint': cfg.solana_usdc, 'inAmount': str(amount),
                'otherAmountThreshold': '5200000'}
    e._order = order
    e._sign_transaction = lambda tx, signer: 'signed'

    class PostApi:
        async def post(self, url, *, data, headers=None):
            assert data == {'signedTransaction': 'signed', 'requestId': 'r'}
            return {'status': 'Success', 'code': 0, 'signature': 'sig123'}
    e.api = PostApi()
    fill = await e.trade(pool, 'sell', 500_000, min_output_units=5_075_000)
    assert fill.txid == 'sig123'
    assert fill.received_units == 5_200_000  # real balance diff, not the quote


async def test_v2_sell_execute_failure_is_uncertain(cfg, pool):
    e = _solana(cfg)

    async def stable():
        return 0
    e._stable_balance = stable

    async def order(p, side, amount, execute):
        return {'requestId': 'r', 'transaction': 't', 'inputMint': pool.mint,
                'outputMint': cfg.solana_usdc, 'inAmount': str(amount),
                'otherAmountThreshold': '9999999'}
    e._order = order
    e._sign_transaction = lambda tx, signer: 'signed'

    class PostApi:
        async def post(self, url, *, data, headers=None):
            return {'status': 'Failed', 'code': 7}
    e.api = PostApi()
    with pytest.raises(UncertainExecution):
        await e.trade(pool, 'sell', 500_000, min_output_units=5_075_000)


async def test_v2_sell_quote_exposes_threshold_as_minimum(cfg, pool):
    e = _solana(cfg)

    async def order(p, side, amount, execute):
        assert not execute
        return {'outAmount': '6000000', 'otherAmountThreshold': '5900000',
                'priceImpactPct': '0.01'}
    e._order = order
    q = await e.quote(pool, 'sell', 500_000)
    assert q.min_out_units == 5_900_000


# --- F3: on-chain verification for live reconcile -----------------------------

async def test_verify_solana_confirmed(cfg):
    from launch80 import verify

    class Ex:
        async def _rpc(self, method, params):
            assert method == 'getTransaction'
            return {'meta': {'err': None}}
    assert await verify.confirmed_onchain(cfg, 'solana', 'sig', exchange=Ex())


async def test_verify_solana_not_found_or_failed(cfg):
    from launch80 import verify

    class NotFound:
        async def _rpc(self, method, params):
            return None

    class Failed:
        async def _rpc(self, method, params):
            return {'meta': {'err': {'InstructionError': [0, {}]}}}
    assert not await verify.confirmed_onchain(cfg, 'solana', 'sig', exchange=NotFound())
    assert not await verify.confirmed_onchain(cfg, 'solana', 'sig', exchange=Failed())


async def test_verify_robinhood_receipt_status(cfg):
    import types
    from launch80 import verify
    good = types.SimpleNamespace(w3=types.SimpleNamespace(eth=types.SimpleNamespace(
        get_transaction_receipt=lambda tx: {'status': 1})))
    reverted = types.SimpleNamespace(w3=types.SimpleNamespace(eth=types.SimpleNamespace(
        get_transaction_receipt=lambda tx: {'status': 0})))

    def missing(tx):
        raise LookupError('not found')
    gone = types.SimpleNamespace(w3=types.SimpleNamespace(eth=types.SimpleNamespace(
        get_transaction_receipt=missing)))
    assert await verify.confirmed_onchain(cfg, 'robinhood', '0xt', exchange=good)
    assert not await verify.confirmed_onchain(cfg, 'robinhood', '0xt', exchange=reverted)
    assert not await verify.confirmed_onchain(cfg, 'robinhood', '0xt', exchange=gone)


# --- F4: operator write-off (ABANDONED) ---------------------------------------

async def test_abandon_frees_caps_and_counts_as_realized_loss(cfg, store, pool):
    en, ex = engine(cfg, store)
    assert await en.enter(pool)
    assert store.active_count() == 1 and store.exposure_usd() == 5
    assert store.abandon(pool.key, 'operator verified total loss')
    row = store.get(pool.key)
    assert row['status'] == 'ABANDONED'
    assert store.active_count() == 0
    assert store.exposure_usd() == 0
    assert store.daily_realized_usd() == -5.0  # principal is a realized loss


async def test_abandon_only_from_open_or_manual_exit(cfg, store, pool):
    en, ex = engine(cfg, store)
    assert await en.enter(pool)
    assert await en.exit(store.get(pool.key))  # CLOSED
    assert not store.abandon(pool.key, 'nope')
    assert store.get(pool.key)['status'] == 'CLOSED'


async def test_abandoned_loss_trips_daily_loss_gate(cfg, store, pool):
    en, ex = engine(replace(cfg, max_daily_realized_loss_usd=Decimal('4')), store)
    assert await en.enter(pool)
    assert store.abandon(pool.key, 'written off')
    assert not await en.enter(replace(pool, pool='otherpool2'))
    events = [e['detail'] for e in store.events() if e['kind'] == 'ENTRY_SKIPPED']
    assert any('loss limit' in d for d in events)


# --- F5: secrets never persist in error text ----------------------------------

def test_scrub_removes_keys_and_rpc_urls(cfg):
    c = replace(cfg, jupiter_key='sk-TESTSECRET42', zero_x_key='zx-KEY-9',
                solana_rpc='https://rpc.example.com/v2/PATHSECRET')
    msg = ('HTTPStatusError for url https://rpc.example.com/v2/PATHSECRET '
           'with key sk-TESTSECRET42 and zx-KEY-9')
    out = c.scrub(msg)
    assert 'sk-TESTSECRET42' not in out
    assert 'zx-KEY-9' not in out
    assert 'PATHSECRET' not in out
    assert 'rpc.example.com' in out  # host stays for diagnostics


def test_scrub_handles_empty_secrets(cfg):
    assert replace(cfg, jupiter_key='', zero_x_key='').scrub('plain') == 'plain'


async def test_exit_quote_error_is_scrubbed_before_persisting(cfg, store, pool):
    secret = 'sk-LEAKME-123'
    en, ex = engine(replace(cfg, jupiter_key=secret), store)
    assert await en.enter(pool)

    async def leaky_quote(p, side, amount):
        if side == 'sell':
            raise TradingError(f'request to https://x/{secret} failed')
        return await FakeExchange.quote(ex, p, side, amount)
    ex.quote = leaky_quote
    assert not await en.exit(store.get(pool.key))
    row = store.get(pool.key)
    assert secret not in (row['last_error'] or '')


# --- F7: safety-check and race coverage ---------------------------------------

async def _safety(cfg, account_value):
    e = _solana(cfg)

    async def rpc(method, params):
        return {'value': account_value}
    e._rpc = rpc
    p = Pool('solana', 'pool1', 'Mint1111111111111111111111111111111111', 'X',
             'pumpswap', Decimal('5000'), time.time())
    return e, p


async def test_token_safety_rejects_active_mint_authority(cfg):
    e, p = await _safety(cfg, {
        'owner': SolanaExchange.TOKEN_PROGRAM,
        'data': {'parsed': {'type': 'mint', 'info': {
            'mintAuthority': 'Attacker111', 'freezeAuthority': None, 'isInitialized': True}}}})
    with pytest.raises(RiskRejection, match='authority'):
        await e.token_safety(p)


async def test_token_safety_rejects_token2022(cfg):
    e, p = await _safety(cfg, {
        'owner': 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
        'data': {'parsed': {'type': 'mint', 'info': {'isInitialized': True}}}})
    with pytest.raises(RiskRejection, match='token program'):
        await e.token_safety(p)


async def test_token_safety_accepts_renounced_mint(cfg):
    e, p = await _safety(cfg, {
        'owner': SolanaExchange.TOKEN_PROGRAM,
        'data': {'parsed': {'type': 'mint', 'info': {
            'mintAuthority': None, 'freezeAuthority': None, 'isInitialized': True}}}})
    assert await e.token_safety(p) is None


async def test_concurrent_exits_sell_exactly_once(cfg, store, pool):
    en, ex = engine(cfg, store)
    assert await en.enter(pool)
    row = store.get(pool.key)
    results = await asyncio.gather(en.exit(row), en.exit(row))
    assert results.count(True) == 1
    assert ex.trades.count('sell') == 1
    assert store.get(pool.key)['status'] == 'CLOSED'


# --- F8: paper transport errors must not fake an uncertain execution ---------

async def test_paper_feed_error_is_confirmed_failure_not_uncertain(cfg, store, pool):
    """A 429 from the price feed inside PaperExchange.trade must NOT quarantine.

    Paper execution broadcasts nothing, so a failed simulation is a confirmed
    non-execution. Leaking a raw transport error made the engine record
    RECOVERY_BUY and halt all trading for onchain reconciliation of a trade
    that never existed.
    """
    import httpx
    from launch80.exchange import PaperExchange
    from launch80.models import TradingError

    ex = PaperExchange('solana', api=None)

    async def rate_limited(p, side, amount):
        request = httpx.Request('GET', 'https://api.geckoterminal.com/x')
        raise httpx.HTTPStatusError('429 Too Many Requests', request=request,
                                    response=httpx.Response(429, request=request))
    ex.quote = rate_limited
    with pytest.raises(TradingError, match='Paper fill unavailable'):
        await ex.trade(pool, 'buy', 5_000_000)


async def test_engine_marks_failed_buy_not_recovery_on_paper_feed_error(cfg, store, pool):
    import httpx
    from launch80.exchange import PaperExchange

    ex = PaperExchange('solana', api=None)

    async def ok_quote(p, side, amount):
        from launch80.models import Quote
        return Quote(amount * 1000, 20, amount * 1000)

    async def fail_on_trade(p, side, amount):
        request = httpx.Request('GET', 'https://api.geckoterminal.com/x')
        raise httpx.HTTPStatusError('429', request=request,
                                    response=httpx.Response(429, request=request))

    async def balance(p):
        return 0

    async def safety(p):
        return None
    ex.token_balance = balance
    ex.token_safety = safety
    en = Engine(cfg, store, FakeFeed(), {'solana': ex})

    calls = {'n': 0}

    async def quote_then_fail(p, side, amount):
        # risk-gate quotes succeed; the quote inside trade() rate-limits
        calls['n'] += 1
        if calls['n'] <= 2:
            return await ok_quote(p, side, amount)
        return await fail_on_trade(p, side, amount)
    ex.quote = quote_then_fail

    assert not await en.enter(pool)
    row = store.get(pool.key)
    assert row['status'] == 'FAILED_BUY'          # not RECOVERY_BUY
    assert store.flag('paused') == '0'            # trading not halted
    assert not store.has_unknowns()


# --- F9: entry pacing is configurable and independent of exits ---------------

def test_entry_interval_default_and_bounds(monkeypatch, cfg):
    monkeypatch.delenv('ENTRY_INTERVAL_SECONDS', raising=False)
    assert Settings.load().entry_interval_seconds == Decimal('2')
    for bad in ('0', '0.05', '301'):
        with pytest.raises(ValueError, match='ENTRY_INTERVAL_SECONDS'):
            replace(cfg, entry_interval_seconds=Decimal(bad)).validate()
    replace(cfg, entry_interval_seconds=Decimal('0.1')).validate()
    replace(cfg, entry_interval_seconds=Decimal('300')).validate()


async def test_entry_loop_waits_the_configured_interval(cfg, store):
    """The entry loop must pace on cfg, not a hardcoded 0.5s tick."""
    en, _ = engine(replace(cfg, entry_interval_seconds=Decimal('7')), store)
    waits = []
    real_wait_for = asyncio.wait_for

    async def record(aw, timeout=None):
        waits.append(timeout)
        en._stopped.set()           # stop after the first tick
        return await real_wait_for(aw, timeout=0.1)
    asyncio.wait_for = record
    try:
        await en._entry_loop()
    finally:
        asyncio.wait_for = real_wait_for
    assert waits == [Decimal('7')]


async def test_exit_loop_tick_is_unaffected_by_entry_interval(cfg, store):
    """Slowing entries must never slow exits: they are separate loops."""
    en, _ = engine(replace(cfg, entry_interval_seconds=Decimal('300')), store)
    waits = []
    real_wait_for = asyncio.wait_for

    async def record(aw, timeout=None):
        waits.append(timeout)
        en._stopped.set()
        return await real_wait_for(aw, timeout=0.1)
    asyncio.wait_for = record
    try:
        await en._exit_loop()
    finally:
        asyncio.wait_for = real_wait_for
    assert waits == [0.5]


# --- F10: a 429 without Retry-After must not be hammered --------------------

async def test_429_without_retry_after_fails_fast(monkeypatch):
    """One logical request must not become three while quota is exhausted."""
    import httpx
    from launch80.http import Api

    attempts = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts['n'] += 1
        return httpx.Response(429)

    api = Api()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await api.get('https://api.geckoterminal.com/x', retries=2)
    finally:
        await api.close()
    assert attempts['n'] == 1, f'expected a single attempt, made {attempts["n"]}'


async def test_429_with_retry_after_is_honoured(monkeypatch):
    """A server-supplied delay is trustworthy, so retrying is still correct."""
    import httpx
    from launch80.http import Api

    attempts = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts['n'] += 1
        if attempts['n'] == 1:
            return httpx.Response(429, headers={'Retry-After': '1'})
        return httpx.Response(200, json={'ok': True})

    api = Api()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await api.get('https://api.geckoterminal.com/x', retries=2) == {'ok': True}
    finally:
        await api.close()
    assert attempts['n'] == 2


async def test_5xx_still_retries_with_backoff(monkeypatch):
    """The fail-fast rule is 429-specific; transient 5xx must still retry."""
    import httpx
    from launch80.http import Api

    attempts = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts['n'] += 1
        if attempts['n'] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={'ok': True})

    api = Api()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await api.get('https://api.geckoterminal.com/x', retries=2) == {'ok': True}
    finally:
        await api.close()
    assert attempts['n'] == 3

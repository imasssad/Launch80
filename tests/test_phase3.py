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


async def test_entry_loop_actually_runs_with_the_configured_interval(cfg, store):
    """Regression: asyncio.wait_for adds the timeout to loop.time(), so a
    Decimal interval raised TypeError and killed the engine on its first tick.
    Mocking wait_for hid that, so this runs the real loop."""
    en, _ = engine(replace(cfg, entry_interval_seconds=Decimal('0.1')), store)

    async def stop_soon():
        await asyncio.sleep(0.25)
        en.stop()
    await asyncio.gather(en._entry_loop(), stop_soon())   # must not raise


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


# --- F11: stop wasting the feed quota on duplicate spot lookups -------------

async def test_entry_evaluation_fetches_each_pool_price_once(cfg, store, pool):
    """One entry used to cost three identical spot requests.

    quote(buy), quote(sell) and trade()->quote() all hit the same pool within
    about a second. On a rate-limited feed that tripled the cost of every
    candidate and was the dominant reason entries failed.
    """
    from launch80.exchange import PaperExchange
    ex = PaperExchange('solana', api=None)
    calls = {'n': 0}

    async def counting_fetch(p):
        calls['n'] += 1
        return Decimal('0.001'), Decimal('50000')
    ex._fetch_spot = counting_fetch

    await ex.quote(pool, 'buy', 5_000_000)
    q = await ex.quote(pool, 'sell', 5_000_000_000)
    await ex.trade(pool, 'buy', 5_000_000)
    assert calls['n'] == 1, f'expected 1 upstream fetch per entry, made {calls["n"]}'
    assert q.out_units > 0


async def test_spot_cache_expires(cfg, pool):
    from launch80.exchange import PaperExchange
    ex = PaperExchange('solana', api=None)
    ex.SPOT_TTL_SECONDS = 0.01
    calls = {'n': 0}

    async def counting_fetch(p):
        calls['n'] += 1
        return Decimal('0.001'), Decimal('50000')
    ex._fetch_spot = counting_fetch
    await ex._spot(pool)
    await asyncio.sleep(0.05)
    await ex._spot(pool)
    assert calls['n'] == 2, 'a stale price must be refetched'


async def test_spot_cache_is_per_pool(cfg, pool):
    from launch80.exchange import PaperExchange
    ex = PaperExchange('solana', api=None)
    calls = {'n': 0}

    async def counting_fetch(p):
        calls['n'] += 1
        return Decimal('0.001'), Decimal('50000')
    ex._fetch_spot = counting_fetch
    await ex._spot(pool)
    await ex._spot(replace(pool, pool='differentpool'))
    assert calls['n'] == 2, 'different pools must not share a cached price'


# --- F12: pace requests so bursts never exceed the feed's limit -------------

async def test_api_paces_requests(cfg):
    """Average volume was already inside quota; bursts caused the rejections."""
    import time as _time

    import httpx
    from launch80.http import Api

    api = Api(min_interval=0.12)
    api.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'ok': True})))
    try:
        start = _time.monotonic()
        for _ in range(4):
            await api.get('https://api.geckoterminal.com/x')
        elapsed = _time.monotonic() - start
    finally:
        await api.close()
    assert elapsed >= 0.33, f'4 paced requests took only {elapsed:.3f}s'


async def test_pacing_disabled_by_default_is_not_slow(cfg):
    import time as _time

    import httpx
    from launch80.http import Api

    api = Api()
    api.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'ok': True})))
    try:
        start = _time.monotonic()
        for _ in range(5):
            await api.get('https://api.geckoterminal.com/x')
        assert _time.monotonic() - start < 0.2
    finally:
        await api.close()


def test_feed_min_interval_bounds(cfg):
    with pytest.raises(ValueError, match='FEED_MIN_INTERVAL_SECONDS'):
        replace(cfg, feed_min_interval_seconds=Decimal('61')).validate()
    replace(cfg, feed_min_interval_seconds=Decimal('0')).validate()


# --- F13: one flaky chain must not pin the circuit breaker on ---------------

class PartialFeed:
    """Discovery where one chain fails and the other returns pools."""

    def __init__(self, failing):
        self.failing = failing

    async def fetch(self, chain):
        if chain == self.failing:
            raise TradingError('feed down for ' + chain)
        return []


async def test_partial_feed_failure_does_not_trip_the_breaker(cfg, store):
    """One chain failing every poll used to pause entries forever."""
    ex = FakeExchange()
    en = Engine(replace(cfg, max_consecutive_feed_failures=2), store,
                PartialFeed('robinhood'), {'robinhood': ex, 'solana': ex})
    for _ in range(6):
        await en.poll()
    assert en.feed_failures == 0, 'a chain that still answers means we are not blind'
    assert store.flag('paused') != '1', 'entries must stay armed on a partial feed'


async def test_total_feed_failure_still_trips_the_breaker(cfg, store):
    ex = FakeExchange()

    class DeadFeed:
        async def fetch(self, chain):
            raise TradingError('feed down')

    en = Engine(replace(cfg, max_consecutive_feed_failures=2), store,
                DeadFeed(), {'robinhood': ex, 'solana': ex})
    for _ in range(3):
        await en.poll()
    assert store.flag('paused') == '1', 'blind on every chain must pause entries'


async def test_breaker_resets_once_a_chain_recovers(cfg, store):
    ex = FakeExchange()

    class Flaky:
        def __init__(self):
            self.alive = False

        async def fetch(self, chain):
            if not self.alive:
                raise TradingError('down')
            return []

    feed = Flaky()
    en = Engine(replace(cfg, max_consecutive_feed_failures=2), store, feed,
                {'robinhood': ex, 'solana': ex})
    for _ in range(3):
        await en.poll()
    assert en.feed_failures >= 2
    feed.alive = True
    await en.poll()
    assert en.feed_failures == 0, 'recovery must clear the counter'


# --- F14: a position on a disabled chain must not spin the exit loop -------

async def test_disabled_chain_position_quarantines_instead_of_raising(cfg, store, pool):
    """Observed in production: 17,100 identical KeyError tracebacks.

    ENABLE_ROBINHOOD=false removed the adapter while a Robinhood position was
    still OPEN. exit() raised KeyError, the handler popped the task, due()
    returned the same row 0.5s later, and the loop wrote a traceback twice a
    second until the host killed the process.
    """
    en, ex = engine(cfg, store)          # exchanges has 'solana' only
    assert await en.enter(pool)
    orphan = replace(pool, chain='robinhood', pool='0xabc')
    store.claim(orphan, 0, intended_spend_units=5_000_000)
    store.open_position(orphan.key, 5_000_000, 10, 'tx', 80)

    assert not await en.exit({'key': orphan.key})          # must not raise
    row = store.get(orphan.key)
    assert row['status'] == 'MANUAL_EXIT'
    assert 'disabled' in row['last_error']


async def test_quarantined_orphan_leaves_the_due_queue(cfg, store, pool):
    """MANUAL_EXIT is terminal for due(), which is what stops the hot loop."""
    en, ex = engine(cfg, store)
    orphan = replace(pool, chain='robinhood', pool='0xabc')
    store.claim(orphan, 0, intended_spend_units=5_000_000)
    store.open_position(orphan.key, 5_000_000, 10, 'tx', 0)   # due immediately
    assert any(r['key'] == orphan.key for r in store.due(time.time()))
    await en.exit({'key': orphan.key})
    assert not any(r['key'] == orphan.key for r in store.due(time.time())), \
        'a quarantined position must stop being rescheduled'


async def test_startup_quarantines_orphaned_chains(cfg, store, pool):
    en, ex = engine(cfg, store)
    orphan = replace(pool, chain='robinhood', pool='0xabc')
    store.claim(orphan, 0, intended_spend_units=5_000_000)
    store.open_position(orphan.key, 5_000_000, 10, 'tx', 80)
    en.quarantine_orphaned_chains()
    assert store.get(orphan.key)['status'] == 'MANUAL_EXIT'
    assert store.flag('paused') == '1'


async def test_exit_worker_failure_backs_the_key_off(cfg, store, pool):
    """Any repeating worker failure must stop being retried every 0.5s."""
    en, ex = engine(cfg, store)
    assert await en.enter(pool)
    store.db.execute('UPDATE positions SET sell_due_at=? WHERE key=?',
                     (time.time() - 1, pool.key))
    assert any(r['key'] == pool.key for r in store.due(time.time()))

    async def boom(row):
        raise RuntimeError('worker exploded')
    en.exit = boom

    task = asyncio.create_task(en._exit_loop())
    await asyncio.sleep(1.2)
    en.stop()
    await task

    row = store.get(pool.key)
    assert row['next_sell_retry_at'] > time.time(), 'failed key must be deferred'
    errors = [e for e in store.events() if e['kind'] == 'EXIT_WORKER_ERROR']
    assert len(errors) <= 3, f'hot loop: {len(errors)} failures in ~1.2s'


# --- F15: a dead pool must back off, not hammer the feed forever -----------

async def test_quote_failures_back_off_exponentially(cfg, store, pool):
    """A rugged pool returns no liquidity and never will again.

    Re-asking every PROFIT_RECHECK_SECONDS forever burned the feed quota that
    live positions need to exit, and buried real events under thousands of
    identical SELL_QUOTE_RETRY entries.
    """
    en, ex = engine(replace(cfg, profit_recheck_seconds=10,
                            quote_backoff_cap_seconds=600), store)
    assert await en.enter(pool)
    ex.bad_exit_quote = True

    delays = []
    for _ in range(6):
        before = time.time()
        store.db.execute('UPDATE positions SET sell_due_at=?,next_sell_retry_at=0 WHERE key=?',
                         (before - 1, pool.key))
        await en.exit(store.get(pool.key))
        delays.append(round(store.get(pool.key)['next_sell_retry_at'] - before))

    assert delays[0] <= 11, f'first retry should be prompt, got {delays[0]}s'
    assert delays == sorted(delays), f'delays must not decrease: {delays}'
    assert delays[-1] > delays[0] * 4, f'no meaningful backoff: {delays}'
    assert delays[-1] <= 600, 'cap must hold'


async def test_quote_failure_run_resets_when_the_pool_answers(cfg, store, pool):
    en, ex = engine(cfg, store)
    assert await en.enter(pool)
    ex.bad_exit_quote = True
    store.db.execute('UPDATE positions SET sell_due_at=? WHERE key=?', (time.time() - 1, pool.key))
    await en.exit(store.get(pool.key))
    assert store.get(pool.key)['quote_failures'] >= 1

    ex.bad_exit_quote = False
    ex.sell_quote_multiplier = 0.80          # answers, but below the floor
    store.db.execute('UPDATE positions SET next_sell_retry_at=0 WHERE key=?', (pool.key,))
    await en.exit(store.get(pool.key))
    assert store.get(pool.key)['quote_failures'] == 0, 'a live quote clears the run'


async def test_repeated_quote_failures_are_not_logged_every_time(cfg, store, pool):
    """The dashboard must show a trail, not a flood."""
    en, ex = engine(cfg, store)
    assert await en.enter(pool)
    ex.bad_exit_quote = True
    for _ in range(12):
        store.db.execute('UPDATE positions SET sell_due_at=?,next_sell_retry_at=0 WHERE key=?',
                         (time.time() - 1, pool.key))
        await en.exit(store.get(pool.key))
    logged = [e for e in store.events(300) if e['kind'] == 'SELL_QUOTE_RETRY']
    assert len(logged) <= 4, f'{len(logged)} retry events for 12 failures is a flood'


async def test_persistent_failure_raises_one_alert(cfg, store, pool):
    en, ex = engine(replace(cfg, quote_failure_alert_after=5), store)
    assert await en.enter(pool)
    ex.bad_exit_quote = True
    for _ in range(9):
        store.db.execute('UPDATE positions SET sell_due_at=?,next_sell_retry_at=0 WHERE key=?',
                         (time.time() - 1, pool.key))
        await en.exit(store.get(pool.key))
    alerts = [e for e in store.events(300) if e['kind'] == 'QUOTE_UNAVAILABLE_LONG']
    assert len(alerts) == 1, f'expected exactly one alert, got {len(alerts)}'

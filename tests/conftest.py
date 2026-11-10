from decimal import Decimal
import time
import pytest
from launch80.config import Settings
from launch80.models import Fill, Pool, Quote, UncertainExecution, TradingError
from launch80.storage import Store

@pytest.fixture
def cfg(monkeypatch,tmp_path):
    monkeypatch.setenv('LIVE','false')
    monkeypatch.setenv('BUY_ENABLED','true')
    monkeypatch.setenv('DB_PATH',str(tmp_path/'test.db'))
    monkeypatch.setenv('MIN_POOL_AGE_SECONDS','10')
    monkeypatch.setenv('MAX_POOL_AGE_SECONDS','150')
    monkeypatch.setenv('BUY_USD','5')
    # Legacy tests exercise the pure bps floor; the flat gas reserve has its
    # own dedicated tests (test_phase3.py) and a nonzero production default.
    monkeypatch.setenv('EXIT_GAS_RESERVE_USD','0')
    return Settings.load()

@pytest.fixture
def store(cfg):
    instance=Store(cfg.db_path)
    yield instance
    instance.close()

@pytest.fixture
def pool():
    return Pool('solana','pool123','Mint1111111111111111111111111111111111','NEW','pumpswap',
                 Decimal('5000.01'),time.time()-25)

class FakeExchange:
    def __init__(self):
        self.safety_ok=True
        self.trades=[]
        self.fail_buy=False
        self.fail_sell=False
        self.uncertain_buy=False
        self.uncertain_sell=False
        self.bad_exit_quote=False
        self.sell_quote_multiplier=1.04
        self.sell_execution_multiplier=1.02
        self.sell_min_factor=0.99

    async def token_safety(self,pool):
        if not self.safety_ok: raise TradingError('unsafe mint')

    async def token_balance(self,pool):
        return 0

    async def quote(self,pool,side,amount):
        if side=='buy': return Quote(amount*1000,20)
        if self.bad_exit_quote: raise TradingError('no route')
        out=int(amount*0.001*self.sell_quote_multiplier)
        return Quote(out,20,int(out*self.sell_min_factor))

    async def trade(self,pool,side,amount,min_output_units=None):
        if side=='buy':
            if self.uncertain_buy: raise UncertainExecution('broadcast unknown')
            if self.fail_buy: raise TradingError('confirmed failure')
            out=amount*1000
        else:
            if self.uncertain_sell: raise UncertainExecution('sell unknown')
            if self.fail_sell: raise TradingError('confirmed failure')
            out=int(amount*0.001*self.sell_execution_multiplier)
            if min_output_units is not None and out < min_output_units:
                raise TradingError('Mock execution below requested minimum')
        self.trades.append(side)
        return Fill('MOCK-'+side,amount,out)

class FakeFeed:
    def __init__(self,items=None): self.items=items or []
    async def fetch(self,chain): return self.items

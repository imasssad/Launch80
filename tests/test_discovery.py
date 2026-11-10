from decimal import Decimal
from launch80.discovery import parse_pool

BASE = '0xabcde00000000000000000000000000000012345'
QUOTE = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'

def test_parse_robinhood_pool():
    raw={'id':'robinhood_0x123','attributes':{
      'reserve_in_usd':'5762.50','pool_created_at':'2026-11-09T00:00:00Z',
      'address':'0x123','name':'WUFF / USDG'},
      'relationships':{'base_token':{'data':{'id':'robinhood_'+BASE}},
                       'quote_token':{'data':{'id':'robinhood_'+QUOTE}},
                       'dex':{'data':{'id':'robinhood_uniswap-v4'}}}}
    pool=parse_pool(raw,{},'robinhood')
    assert pool.mint==BASE
    assert pool.liquidity_usd==Decimal('5762.50')
    assert pool.chain=='robinhood'

def test_reject_zero_liquidity():
    raw={'attributes':{'reserve_in_usd':'0','pool_created_at':'2026-11-09T00:00:00Z'}}
    assert parse_pool(raw,{},'robinhood') is None

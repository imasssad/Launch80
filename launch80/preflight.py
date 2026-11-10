"""Read-only connectivity/balance checks. No trades, approvals, or signing."""
from __future__ import annotations
import asyncio
import json
from .config import Settings
from .exchange import RobinhoodExchange,SolanaExchange
from .discovery import GeckoPools
from .http import Api
from .models import Pool
from decimal import Decimal
import time

async def check(cfg: Settings):
    api=Api()
    outcome={'mode':'live' if cfg.live else 'paper', 'chains':{}, 'read_only':True}
    try:
        discover=GeckoPools(api,1)
        chains=([('robinhood',cfg.enable_robinhood),('solana',cfg.enable_solana)])
        for name,enabled in chains:
            if not enabled: continue
            row={'network':name}
            outcome['chains'][name]=row
            try:
                pools=await discover.fetch(name)
                row['gecko_feed_ok']=True
                row['pools_returned']=len(pools)
                row['pools_above_threshold']=sum(x.liquidity_usd >= cfg.min_liquidity_usd for x in pools)
            except Exception as e:
                row['gecko_feed_ok']=False
                row['feed_error']=str(type(e).__name__)
            if not cfg.live: continue
            try:
                if name == 'robinhood':
                    ex=RobinhoodExchange(cfg,api)
                    await ex.validate_network()
                    row['wallet']=ex.wallet
                    row['gas_eth_wei']=str(await asyncio.to_thread(ex.w3.eth.get_balance,ex.wallet))
                    row['usdg_atomic_balance']=str(await asyncio.to_thread(ex._balance,cfg.robinhood_usdg))
                    # Quote check without approving or executing (may error if no funds).
                    sample=Pool('robinhood','diagnostic',
                        '0x0bd7d308f8e1639fab988df18a8011f41eacad73','WETH','',Decimal('0'),time.time())
                    quote=await ex.quote(sample,'buy',1_000_000)
                    row['zero_x_read_only_quote_ok']=quote.out_units>0
                    sell=await ex.quote(sample,'sell',10**15)
                    row['zero_x_sell_minimum_quote_ok']=bool(sell.min_out_units and sell.min_out_units>0)
                else:
                    ex=SolanaExchange(cfg,api)
                    await ex.validate_network()
                    row['wallet']=ex.wallet
                    row['gas_sol_lamports']=str((await ex._rpc('getBalance',[ex.wallet]))['value'])
                    sample=Pool('solana','diagnostic',
                       'So11111111111111111111111111111111111111112','SOL','',Decimal('0'),time.time())
                    quote=await ex.quote(sample,'buy',1_000_000)
                    row['jupiter_read_only_quote_ok']=quote.out_units>0
                    sell=await ex.quote(sample,'sell',100_000_000)
                    row['jupiter_v1_sell_minimum_quote_ok']=bool(sell.min_out_units and sell.min_out_units>0)
            except Exception as e:
                row['live_preflight_error']=type(e).__name__+': '+str(e)[:200]
        outcome['ok']=all(r.get('gecko_feed_ok') and 'live_preflight_error' not in r
              and (not cfg.live or (
                  r.get('zero_x_sell_minimum_quote_ok') if r['network']=='robinhood'
                  else r.get('jupiter_v1_sell_minimum_quote_ok')))
                          for r in outcome['chains'].values())
        return outcome
    finally:
        await api.close()

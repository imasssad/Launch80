"""Optional Garp token intelligence (not an execution interface or live pool feed)."""
from __future__ import annotations
from .http import Api
from .models import Pool, RiskRejection

class Garp:
    def __init__(self,api: Api, require_source: bool = False):
        self.api=api
        self.require_source=require_source

    async def enrich(self,pool: Pool) -> str:
        # A freshly created token may not yet be indexed. Optional strict mode
        # blocks unmatched launches; a missing API response never authorizes risk.
        try:
            r=await self.api.get(f'https://www.usegarp.com/api/v1/token/{pool.mint}',
                                 params={'deep':0}, retries=0)
        except Exception:
            if self.require_source:
                raise RiskRejection('Garp source verification unavailable')
            return 'unverified'
        if r.get('chainId') != pool.chain or str(r.get('address','')).lower() != pool.mint.lower():
            if self.require_source:
                raise RiskRejection('Garp chain or mint mismatch')
            return 'unverified'
        source=str(r.get('launchpad') or 'unknown').lower()
        if self.require_source:
            allowed={'robinhood':{'pons-v1','pons-v2'}, 'solana':{'pumpfun'}}
            if source not in allowed[pool.chain]:
                raise RiskRejection('Pool is not from the selected launchpads according to Garp')
        return source

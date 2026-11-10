"""Discover *actual pools* (not mints or pre-graduation bonding curves)."""
from __future__ import annotations
import asyncio
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from .models import Pool
from .http import Api

BASE = 'https://api.geckoterminal.com/api/v2'
MINT_EXCLUDE = {
    'robinhood': {
        '0x5fc5360d0400a0fd4f2af552add042d716f1d168', # USDG
        '0x0bd7d308f8e1639fab988df18a8011f41eacad73', # WETH
    },
    'solana': {
        'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', # USDC
        'So11111111111111111111111111111111111111112', # WSOL
        'Es9vMFrzaCERmJfrF4H2FYD4KCoNKYdD9s8dA7xqCjpk', # USDT
    },
}
EVM_RE = re.compile(r'^0x[a-fA-F0-9]{40}$')
SOL_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def parse_pool(raw: dict, included: dict, chain: str) -> Pool | None:
    try:
        att = raw['attributes']
        liquidity = Decimal(str(att['reserve_in_usd']))
        if not liquidity.is_finite() or liquidity <= 0:
            return None
        created = datetime.fromisoformat(att['pool_created_at'].replace('Z', '+00:00')).timestamp()
        if created > time.time() + 60:
            return None
        rel = raw['relationships']
        br = rel['base_token']['data']['id']
        qr = rel['quote_token']['data']['id']
        get_address = lambda ref: included.get(ref, {}).get('attributes', {}).get('address', ref.split('_', 1)[-1])
        base = get_address(br)
        quote = get_address(qr)
        excluded = MINT_EXCLUDE[chain]
        if base.lower() in {s.lower() for s in excluded}:
            base = quote
        if base.lower() in {s.lower() for s in excluded}:
            return None
        if chain == 'robinhood' and not EVM_RE.fullmatch(base):
            return None
        if chain == 'solana' and not SOL_RE.fullmatch(base):
            return None
        pool_address = att.get('address') or raw['id'].split('_', 1)[-1]
        if not pool_address:
            return None
        dex = (rel.get('dex', {}).get('data') or {}).get('id', 'unknown')
        t = included.get(br, {}).get('attributes', {})
        symbol = str(t.get('symbol') or att.get('name') or '?')[:40]
        return Pool(chain, pool_address, base, symbol, str(dex), liquidity, created)
    except (ValueError, TypeError, KeyError, InvalidOperation, AttributeError):
        return None


class GeckoPools:
    def __init__(self, api: Api, pages: int):
        self.api = api
        self.pages = pages

    async def fetch(self, chain: str) -> list[Pool]:
        # Public API is rate-limited. Per-chain page requests are deliberately sequential.
        result = {}
        for page in range(1, self.pages + 1):
            data = await self.api.get(f'{BASE}/networks/{chain}/new_pools',
                                      params={'page': page, 'include': 'base_token,quote_token,dex'})
            includes = {row['id']: row for row in data.get('included', [])}
            for raw in data.get('data', []):
                pool = parse_pool(raw, includes, chain)
                if pool is not None:
                    result[pool.key] = pool
            if not data.get('data'):
                break
        return sorted(result.values(), key=lambda p: p.created_at, reverse=True)

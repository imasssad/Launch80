"""Bounded HTTP with backoff only for safe reads. Never retry signed execution POSTs."""
from __future__ import annotations
import asyncio
import random
import httpx

class Api:
    def __init__(self, timeout=12.0):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False,
                                        headers={'User-Agent': 'Launch80/1.0'})

    async def close(self):
        await self.client.aclose()

    async def get(self, url, *, params=None, headers=None, retries=2):
        for n in range(retries + 1):
            try:
                response = await self.client.get(url, params=params, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504) and n < retries:
                    delay = min(float(response.headers.get('Retry-After', 0) or 0), 8) or (0.8 * (2 ** n))
                    await asyncio.sleep(delay + random.random() / 5)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.ConnectError):
                if n == retries:
                    raise
                await asyncio.sleep(0.8 * (2 ** n))
        raise RuntimeError('unreachable')

    async def post(self, url, *, data, headers=None):
        """Critical execution: one POST only, even on a timeout. Caller must reconcile."""
        response = await self.client.post(url, json=data, headers=headers)
        response.raise_for_status()
        return response.json()

"""Bounded HTTP with backoff only for safe reads. Never retry signed execution POSTs."""
from __future__ import annotations
import asyncio
import random
import httpx


def _retry_after_seconds(raw: str | None, cap: float = 8.0) -> float:
    """Seconds from a Retry-After header, or 0 when absent/unusable.

    RFC 9110 permits an HTTP-date as well as a delay in seconds. Parsing the
    date form with float() raises ValueError, which would escape the read
    retry loop and turn a throttling response into a hard feed failure.
    """
    if not raw:
        return 0.0
    try:
        return max(0.0, min(float(raw.strip()), cap))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, min((when-datetime.now(timezone.utc)).total_seconds(), cap))
    except Exception:
        return 0.0


class Api:
    def __init__(self, timeout=12.0):
        from . import __version__
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False,
                                        headers={'User-Agent': f'Launch80/{__version__}'})

    async def close(self):
        await self.client.aclose()

    async def get(self, url, *, params=None, headers=None, retries=2):
        for n in range(retries + 1):
            try:
                response = await self.client.get(url, params=params, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504) and n < retries:
                    delay = _retry_after_seconds(response.headers.get('Retry-After'))
                    if response.status_code == 429 and not delay:
                        # Rate limited with no server-supplied delay. The
                        # sub-second backoff below would turn one logical
                        # request into three within ~2.4s, all near-certain to
                        # fail, burning the very quota that is exhausted. Fail
                        # fast instead: every caller already retries on a much
                        # slower cadence (the engine re-quotes an exit after
                        # PROFIT_RECHECK_SECONDS, entries retry next poll),
                        # which is the right place to wait out a quota window.
                        response.raise_for_status()
                    await asyncio.sleep((delay or 0.8 * (2 ** n)) + random.random() / 5)
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

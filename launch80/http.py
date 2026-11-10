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
    """Bounded HTTP client with optional client-side pacing.

    ``min_interval`` enforces a minimum gap between outbound requests. A
    free-tier price feed rejects bursts even when the per-minute average is
    well inside its quota, and every rejection costs a trade; spacing requests
    keeps demand under the limit instead of discovering it by being refused.
    """

    def __init__(self, timeout=12.0, min_interval: float = 0.0):
        from . import __version__
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False,
                                        headers={'User-Agent': f'Launch80/{__version__}'})
        self.min_interval = max(0.0, float(min_interval))
        self._pace_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _pace(self):
        if self.min_interval <= 0:
            return
        async with self._pace_lock:
            wait = self._last_request + self.min_interval - asyncio.get_running_loop().time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = asyncio.get_running_loop().time()

    async def close(self):
        await self.client.aclose()

    async def get(self, url, *, params=None, headers=None, retries=2):
        for n in range(retries + 1):
            try:
                await self._pace()
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

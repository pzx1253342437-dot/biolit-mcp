"""HTTP plumbing shared by the PubMed and UniProt clients: rate limiting and retries."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


class NotFoundError(Exception):
    """The upstream service says the requested record does not exist."""


class UpstreamError(Exception):
    """The upstream service kept failing after all retries."""


class RateLimiter:
    """Spaces requests so that no more than `rate_per_s` start in any one second.

    A single asyncio lock plus a "next allowed start time" is enough here: the
    server handles one client at a time and the limits are small (3 to 10 req/s).
    """

    def __init__(
        self,
        rate_per_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate_per_s
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next = max(now, self._next) + self._interval


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


async def get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    limiter: RateLimiter,
    max_retries: int = 4,
    base_delay: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> httpx.Response:
    """GET with rate limiting, exponential backoff with jitter, and Retry-After support.

    404 (and UniProt's 400 for malformed accessions) are raised as NotFoundError
    immediately, since retrying them cannot help.
    """
    sleep = sleep or asyncio.sleep
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        await limiter.acquire()
        try:
            resp = await client.get(url, params=params)
        except httpx.TransportError as exc:
            last_exc = exc
        else:
            if resp.status_code in (400, 404):
                raise NotFoundError(f"{resp.status_code} for {url}")
            if resp.status_code not in RETRY_STATUS:
                resp.raise_for_status()
                return resp
            last_exc = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
            hinted = _retry_after(resp)
            if hinted is not None and attempt < max_retries - 1:
                await sleep(hinted)
                continue
        if attempt < max_retries - 1:
            delay = base_delay * (2**attempt) * (0.5 + random.random())
            await sleep(delay)
    raise UpstreamError(f"giving up on {url} after {max_retries} attempts: {last_exc}")

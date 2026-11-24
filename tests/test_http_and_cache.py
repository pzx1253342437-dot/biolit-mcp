from __future__ import annotations

import httpx
import pytest
import respx

from biolit_mcp.cache import TTLCache
from biolit_mcp.http import NotFoundError, RateLimiter, UpstreamError, get_with_retries

from .conftest import NoWaitLimiter

URL = "https://example.org/api"


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


@respx.mock
async def test_retries_on_429_then_succeeds(http):
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    )
    sleeps = Sleeps()
    resp = await get_with_retries(http, URL, {}, NoWaitLimiter(), max_retries=4, sleep=sleeps)
    assert resp.json() == {"ok": 1}
    assert route.call_count == 3
    assert len(sleeps.calls) == 2
    assert sleeps.calls[1] > sleeps.calls[0] * 0.5  # backoff grows (with jitter)


@respx.mock
async def test_honours_retry_after(http):
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    sleeps = Sleeps()
    await get_with_retries(http, URL, {}, NoWaitLimiter(), sleep=sleeps)
    assert sleeps.calls == [7.0]


@respx.mock
async def test_gives_up_after_max_retries(http):
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(UpstreamError):
        await get_with_retries(http, URL, {}, NoWaitLimiter(), max_retries=3, sleep=Sleeps())
    assert route.call_count == 3


@respx.mock
async def test_transport_errors_are_retried(http):
    respx.get(URL).mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200)])
    resp = await get_with_retries(http, URL, {}, NoWaitLimiter(), sleep=Sleeps())
    assert resp.status_code == 200


@respx.mock
@pytest.mark.parametrize("status", [400, 404])
async def test_not_found_is_not_retried(http, status):
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(NotFoundError):
        await get_with_retries(http, URL, {}, NoWaitLimiter(), sleep=Sleeps())
    assert route.call_count == 1


async def test_rate_limiter_spaces_requests():
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    limiter = RateLimiter(4.0, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(5):
        await limiter.acquire()
    # First request is free, the next four wait 0.25 s each.
    assert sleeps == pytest.approx([0.25] * 4)


def test_cache_expiry_and_secret_free_keys():
    now = [100.0]
    cache = TTLCache(None, clock=lambda: now[0])
    k1 = TTLCache.make_key("u", {"id": "1", "api_key": "A"})
    k2 = TTLCache.make_key("u", {"id": "1", "api_key": "B"})
    assert k1 == k2
    cache.set(k1, {"x": 1}, ttl_s=10)
    assert cache.get(k1) == {"x": 1}
    now[0] = 111.0
    assert cache.get(k1) is None

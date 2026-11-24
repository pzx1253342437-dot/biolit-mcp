from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from biolit_mcp.cache import TTLCache
from biolit_mcp.config import Settings
from biolit_mcp.http import RateLimiter
from biolit_mcp.pubmed import PubMedClient
from biolit_mcp.uniprot import UniProtClient

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


class NoWaitLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(1000.0)

    async def acquire(self) -> None:
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(ncbi_api_key="SECRET", ncbi_email="me@example.org", max_retries=3)


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def cache() -> TTLCache:
    return TTLCache(None)


@pytest.fixture
def pubmed(settings, http, cache) -> PubMedClient:
    return PubMedClient(settings, http, cache, limiter=NoWaitLimiter())


@pytest.fixture
def uniprot(settings, http, cache) -> UniProtClient:
    return UniProtClient(settings, http, cache, limiter=NoWaitLimiter())

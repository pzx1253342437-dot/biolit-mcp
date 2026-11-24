"""Smoke tests against the real NCBI and UniProt APIs. Skipped unless BIOLIT_LIVE=1."""

from __future__ import annotations

import os

import httpx
import pytest

from biolit_mcp.config import Settings
from biolit_mcp.models import ReferenceIn
from biolit_mcp.pubmed import PubMedClient
from biolit_mcp.uniprot import UniProtClient
from biolit_mcp.verify import verify_references

pytestmark = pytest.mark.skipif(os.environ.get("BIOLIT_LIVE") != "1", reason="set BIOLIT_LIVE=1")


@pytest.fixture
async def live():
    s = Settings.from_env()
    async with httpx.AsyncClient(timeout=30) as http:
        yield PubMedClient(s, http), UniProtClient(s, http)


async def test_uniprot_tp53(live):
    _, uni = live
    entry = await uni.entry("P04637")
    assert "TP53" in entry.genes
    assert entry.organism == "Homo sapiens"
    assert entry.length == 393


async def test_pubmed_search_and_verify_round_trip(live):
    pubmed, _ = live
    res = await pubmed.search("CRISPR base editing", max_results=3)
    assert res.hits
    first = res.hits[0]
    papers = await pubmed.fetch([first.pmid])
    assert papers and papers[0].title
    report = await verify_references(
        pubmed,
        [ReferenceIn(pmid=first.pmid, title=first.title), ReferenceIn(pmid="99999999999")],
    )
    assert [r.status for r in report.results] == ["verified", "not_found"]

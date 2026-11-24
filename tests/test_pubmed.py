from __future__ import annotations

import httpx
import respx

from biolit_mcp.pubmed import EUTILS, parse_efetch, parse_esummary

from .conftest import load_json, load_text


def test_parse_esummary_marks_missing_records():
    out = parse_esummary(load_json("esummary.json"))
    assert out["99999999"] is None
    hit = out["90000001"]
    assert hit.title.startswith("Base editing")
    assert hit.year == "2021"
    assert hit.first_author == "Zhang L"
    assert hit.doi == "10.5555/sbj.2021.001"
    assert out["90000002"].first_author is None


def test_parse_efetch_structured_abstract_and_inline_markup():
    papers = parse_efetch(load_text("efetch.xml"), max_abstract_chars=5000)
    assert [p.pmid for p in papers] == ["90000001", "90000002"]
    p = papers[0]
    assert p.abstract.startswith("BACKGROUND: Adenine base editors")
    assert "62% in PCSK9 alleles" in p.abstract  # <i> text kept
    assert p.authors == ["Zhang L", "Okafor C"]
    assert p.mesh_terms == ["Gene Editing", "Hepatocytes"]
    assert p.doi == "10.5555/sbj.2021.001"
    assert p.url == "https://pubmed.ncbi.nlm.nih.gov/90000001/"
    q = papers[1]
    assert q.abstract is None
    assert q.year == "2019"  # from MedlineDate
    assert q.authors == ["Genome Safety Consortium"]


def test_parse_efetch_truncates_long_abstracts():
    p = parse_efetch(load_text("efetch.xml"), max_abstract_chars=40)[0]
    assert p.abstract_truncated
    assert p.abstract.endswith("[truncated]")


@respx.mock
async def test_search_joins_esearch_and_esummary(pubmed):
    es = respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json=load_json("esearch.json"))
    )
    respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_json("esummary.json"))
    )
    res = await pubmed.search("crispr", max_results=2, min_year=2018)
    assert res.total_count == 2481
    assert [h.pmid for h in res.hits] == ["90000001", "90000002"]
    assert "MeSH" in res.query_translation
    params = es.calls.last.request.url.params
    assert params["api_key"] == "SECRET"
    assert params["mindate"] == "2018"
    assert params["datetype"] == "pdat"


@respx.mock
async def test_responses_are_cached(pubmed):
    route = respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_json("esummary.json"))
    )
    await pubmed.summaries(["90000001"])
    await pubmed.summaries(["90000001"])
    assert route.call_count == 1


@respx.mock
async def test_summaries_reports_silently_dropped_ids(pubmed):
    respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_json("esummary.json"))
    )
    out = await pubmed.summaries(["90000001", "12345"])
    assert out["12345"] is None

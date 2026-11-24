"""Offline tests for the eval harness: scoring, and the tool-use loop with a scripted model."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx

from biolit_mcp import server
from biolit_mcp.models import ProteinEntry, ReferenceCheck, ReferenceIn, VerifyReport
from biolit_mcp.pubmed import EUTILS
from evals.run import ask, summarize
from evals.scoring import extract_json, score_citations, score_protein

from .conftest import load_json


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"papers": [{"pmid": "1"}]} hope this helps') == {
        "papers": [{"pmid": "1"}]
    }
    assert extract_json('{not json} then {"b": 2}') == {"b": 2}
    assert extract_json("no json here") is None


def test_score_citations_buckets():
    def chk(status, pmid="1"):
        return ReferenceCheck(claimed=ReferenceIn(pmid=pmid), status=status)

    report = VerifyReport(
        checked=4,
        verified=1,
        problems=3,
        results=[
            chk("verified"),
            chk("title_mismatch"),
            chk("not_found"),
            chk("unverifiable", None),
        ],
    )
    s = score_citations(report, [{}, {}, {}, {}])
    assert (s.claimed, s.valid, s.wrong_paper, s.nonexistent, s.no_pmid) == (4, 1, 1, 1, 1)
    assert score_citations(None, [{}, {}]).no_pmid == 2


def test_score_protein():
    gold = ProteinEntry(
        accession="Q9ZZZ1", genes=["SYNP1"], organism="Homo sapiens", length=412, url="u"
    )
    assert score_protein({"gene": "synp1", "organism": "Homo sapiens", "length": "412"}, gold) == {
        "gene": True,
        "organism": True,
        "length": True,
    }
    assert score_protein({"gene": "SYNP2", "organism": "Mus musculus", "length": 411}, gold) == {
        "gene": False,
        "organism": False,
        "length": False,
    }
    assert score_protein(None, gold)["gene"] is False


class ScriptedModel:
    """Stands in for AsyncAnthropic: first asks for a tool, then answers."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=100, output_tokens=20)
        if len(self.calls) == 1:
            block = SimpleNamespace(
                type="tool_use",
                id="t1",
                name="verify_references",
                input={"references": [{"pmid": "90000001"}]},
            )
            return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
        text = SimpleNamespace(type="text", text='{"papers": [{"pmid": "90000001"}]}')
        return SimpleNamespace(content=[text], stop_reason="end_turn", usage=usage)


@pytest.fixture
def inject_clients(pubmed, uniprot):
    server.state.clients = server.Clients(pubmed=pubmed, uniprot=uniprot)
    yield
    server.state.clients = None


@respx.mock
async def test_tool_loop_runs_real_tools(inject_clients):
    respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_json("esummary.json"))
    )
    model = ScriptedModel()
    out = await ask(model, "test-model", "find papers", use_tools=True)
    assert out["tool_calls"] == 1
    assert out["input_tokens"] == 200
    assert "tools" in model.calls[0]
    tool_result = model.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and not tool_result["is_error"]
    assert '"verified"' in tool_result["content"]


def test_summarize_table():
    rec = {"input_tokens": 10, "output_tokens": 5, "latency_s": 1.0}
    result = {
        "model": "m",
        "run_at": "now",
        "conditions": ["baseline"],
        "records": [
            {
                "kind": "citation",
                "condition": "baseline",
                "task": "cit-01",
                "score": {"claimed": 3, "valid": 1, "nonexistent": 1, "wrong_paper": 1},
                **rec,
            },
            {
                "kind": "protein",
                "condition": "baseline",
                "task": "prot-famous-01",
                "score": {"gene": True, "organism": True, "length": False},
                **rec,
            },
        ],
    }
    table = summarize(result)
    assert "| baseline | 1/3 (33%) | 1 | 1 |" in table

from __future__ import annotations

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from biolit_mcp import server
from biolit_mcp.pubmed import EUTILS
from biolit_mcp.uniprot import UNIPROT

from .conftest import load_json


@pytest.fixture(autouse=True)
def inject_clients(pubmed, uniprot):
    server.state.clients = server.Clients(pubmed=pubmed, uniprot=uniprot)
    yield
    server.state.clients = None


async def test_tools_are_registered_with_schemas():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert set(tools) == {
        "pubmed_search",
        "pubmed_fetch",
        "uniprot_search",
        "uniprot_entry",
        "verify_references",
    }
    for t in tools.values():
        assert t.description and len(t.description) > 40
        assert t.output_schema is not None


@respx.mock
async def test_call_verify_references_through_mcp():
    respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_json("esummary.json"))
    )
    result = await server.mcp.call_tool(
        "verify_references", {"references": [{"pmid": "99999999"}, {"pmid": "90000001"}]}
    )
    data = result.structured_content
    assert data["checked"] == 2
    assert [r["status"] for r in data["results"]] == ["not_found", "verified"]


@respx.mock
async def test_call_uniprot_entry_through_mcp():
    respx.get(f"{UNIPROT}/Q9ZZZ1.json").mock(
        return_value=httpx.Response(200, json=load_json("uniprot_entry.json"))
    )
    result = await server.mcp.call_tool("uniprot_entry", {"accession": "Q9ZZZ1"})
    assert result.structured_content["length"] == 412


async def test_bad_accession_gives_the_model_a_useful_error():
    with pytest.raises(ToolError, match="uniprot_search"):
        await server.mcp.call_tool("uniprot_entry", {"accession": "TP53"})


@respx.mock
async def test_upstream_outage_becomes_tool_error(monkeypatch):
    import biolit_mcp.http as h

    async def no_sleep(_):
        return None

    monkeypatch.setattr(h.asyncio, "sleep", no_sleep)
    respx.get(f"{UNIPROT}/Q9ZZZ1.json").mock(return_value=httpx.Response(503))
    with pytest.raises(ToolError, match="UniProt is unavailable"):
        await server.mcp.call_tool("uniprot_entry", {"accession": "Q9ZZZ1"})

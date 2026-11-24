from __future__ import annotations

import httpx
import pytest
import respx

from biolit_mcp.http import NotFoundError
from biolit_mcp.uniprot import UNIPROT, UniProtClient, is_accession, parse_entry, parse_hit

from .conftest import load_json


@pytest.mark.parametrize(
    "value,ok",
    [("P04637", True), ("q9zzz1", True), ("A0A0B4J2F0", True), ("TP53", False), ("P0463", False)],
)
def test_accession_pattern(value, ok):
    assert is_accession(value) is ok


def test_parse_hits_reviewed_and_unreviewed():
    hits = [parse_hit(r) for r in load_json("uniprot_search.json")["results"]]
    assert hits[0].reviewed and hits[0].genes == ["SYNP1"] and hits[0].length == 412
    assert not hits[1].reviewed
    assert hits[1].protein_name == "Uncharacterized protein"
    assert hits[1].genes == ["b0001"]


def test_parse_entry():
    e = parse_entry(load_json("uniprot_entry.json"), include_sequence=False, max_function_chars=40)
    assert e.accession == "Q9ZZZ1"
    assert e.taxon_id == 9606
    assert e.mass_da == 45210
    assert e.subcellular_locations == ["Nucleus", "Cytoplasm"]
    assert e.go_terms == ["GO:0005634 C:nucleus", "GO:0003677 F:DNA binding"]
    assert e.function.endswith("[truncated]")
    assert e.sequence is None
    full = parse_entry(
        load_json("uniprot_entry.json"), include_sequence=True, max_function_chars=999
    )
    assert full.sequence == "MSTNPKPQRK"


def test_build_query():
    assert UniProtClient.build_query("gene:TP53", "9606", True) == (
        "(gene:TP53) AND organism_id:9606 AND reviewed:true"
    )
    assert UniProtClient.build_query("kinase", "Mus musculus", False) == (
        '(kinase) AND organism_name:"Mus musculus"'
    )


@respx.mock
async def test_search_and_entry(uniprot):
    respx.get(f"{UNIPROT}/search").mock(
        return_value=httpx.Response(200, json=load_json("uniprot_search.json"))
    )
    respx.get(f"{UNIPROT}/Q9ZZZ1.json").mock(
        return_value=httpx.Response(200, json=load_json("uniprot_entry.json"))
    )
    res = await uniprot.search("SYNP1", organism="9606")
    assert res.hits[0].accession == "Q9ZZZ1"
    entry = await uniprot.entry("q9zzz1")
    assert entry.protein_name == "Synthetic test protein 1"


@respx.mock
async def test_missing_entry_raises_not_found(uniprot):
    respx.get(f"{UNIPROT}/P99999.json").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFoundError):
        await uniprot.entry("P99999")

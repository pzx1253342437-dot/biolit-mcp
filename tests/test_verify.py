from __future__ import annotations

from biolit_mcp.models import PaperHit, ReferenceIn
from biolit_mcp.verify import normalize_title, title_similarity, verify_references

REAL = {
    "90000001": PaperHit(
        pmid="90000001", title="Base editing of a pathogenic variant in cultured hepatocytes."
    ),
    "90000002": PaperHit(
        pmid="90000002", title="Off-target profiling of Cas9 nucleases by whole-genome sequencing."
    ),
}
DOI_INDEX = {"10.5555/sbj.2021.001": "90000001"}


class FakePubMed:
    """Answers like PubMed would, from a tiny in-memory index."""

    def __init__(self) -> None:
        self.summary_calls: list[list[str]] = []

    async def summaries(self, pmids):
        self.summary_calls.append(list(pmids))
        return {p: REAL.get(p) for p in pmids}

    async def search_ids(self, term, max_results=10, **_):
        if term.endswith("[doi]"):
            doi = term.split('"')[1]
            ids = [DOI_INDEX[doi]] if doi in DOI_INDEX else []
        else:  # "[ti]" search: any record sharing two or more words
            words = set(term.replace("[ti]", "").split())
            ids = [
                p
                for p, h in REAL.items()
                if len(words & set(normalize_title(h.title).split())) >= 2
            ]
        return ids[:max_results], len(ids), None


def test_normalize_and_similarity():
    assert normalize_title("<i>Cas9</i> off-target, profiling.") == "cas9 off target profiling"
    assert title_similarity("A B C D", "a b c d.") == 100
    # A fragment must not count as a match.
    assert title_similarity("Base editing", REAL["90000001"].title) < 85


async def test_all_statuses():
    fake = FakePubMed()
    refs = [
        ReferenceIn(
            pmid="90000001", title="Base editing of a pathogenic variant in cultured hepatocytes"
        ),
        ReferenceIn(
            pmid="PMID: 90000002", title="CRISPR screens identify regulators of T cell exhaustion"
        ),
        ReferenceIn(pmid="31415926", title="A paper that does not exist"),
        ReferenceIn(doi="https://doi.org/10.5555/sbj.2021.001"),
        ReferenceIn(title="Off target profiling of Cas9 nucleases by whole genome sequencing"),
        ReferenceIn(title="Quantum entanglement in sourdough fermentation"),
        ReferenceIn(),
    ]
    report = await verify_references(fake, refs)
    statuses = [r.status for r in report.results]
    assert statuses == [
        "verified",
        "title_mismatch",
        "not_found",
        "verified",
        "found_by_title",
        "not_found",
        "unverifiable",
    ]
    assert report.checked == 7 and report.verified == 3 and report.problems == 4
    assert report.results[1].actual_title.startswith("Off-target profiling")
    assert report.results[4].pmid == "90000002"
    # All PMID-bearing references go out in one batched call.
    assert fake.summary_calls[0] == ["31415926", "90000001", "90000002"]


async def test_reference_list_is_capped():
    refs = [ReferenceIn(pmid="90000001")] * 50
    report = await verify_references(FakePubMed(), refs)
    assert report.checked == 30

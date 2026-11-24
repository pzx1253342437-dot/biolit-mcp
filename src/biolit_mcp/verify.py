"""Citation verification: check that references an LLM produced point to real papers.

Language models often produce PMIDs and DOIs that look plausible but do not exist,
or that exist and belong to a different paper. This module checks each claimed
reference against PubMed deterministically, without asking a model anything.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .http import NotFoundError
from .models import PaperHit, ReferenceCheck, ReferenceIn, VerifyReport
from .pubmed import PubMedClient

TITLE_MATCH_THRESHOLD = 85.0
MAX_REFERENCES = 30

_TAG_RE = re.compile(r"<[^>]+>")
_NON_WORD_RE = re.compile(r"[^\w\s]")


def normalize_title(title: str) -> str:
    t = _TAG_RE.sub(" ", title).lower()
    t = _NON_WORD_RE.sub(" ", t)
    return " ".join(t.split())


def title_similarity(a: str, b: str) -> float:
    """0-100. token_sort_ratio tolerates reordered words but, unlike token_set_ratio,
    does not give a perfect score when one title is only a fragment of the other."""
    return float(fuzz.token_sort_ratio(normalize_title(a), normalize_title(b)))


def _clean_pmid(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def _clean_doi(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    v = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", v, flags=re.I)
    return v or None


def _judge(ref: ReferenceIn, hit: PaperHit, found_status: str) -> ReferenceCheck:
    if not ref.title:
        return ReferenceCheck(
            claimed=ref,
            status=found_status,  # type: ignore[arg-type]
            pmid=hit.pmid,
            actual_title=hit.title,
            note="Record exists. No title was given, so only existence was checked.",
        )
    sim = title_similarity(ref.title, hit.title)
    ok = sim >= TITLE_MATCH_THRESHOLD
    return ReferenceCheck(
        claimed=ref,
        status=found_status if ok else "title_mismatch",  # type: ignore[arg-type]
        pmid=hit.pmid,
        actual_title=hit.title,
        title_similarity=round(sim, 1),
        note=None if ok else "The identifier exists but belongs to a different paper.",
    )


async def _search_ids(pubmed: PubMedClient, term: str, n: int) -> list[str]:
    try:
        ids, _, _ = await pubmed.search_ids(term, max_results=n)
    except NotFoundError:
        return []
    return ids


async def verify_references(pubmed: PubMedClient, refs: list[ReferenceIn]) -> VerifyReport:
    refs = refs[:MAX_REFERENCES]
    results: list[ReferenceCheck | None] = [None] * len(refs)

    # 1. Everything that carries a PMID is checked in one batched esummary call.
    by_pmid = {i: _clean_pmid(r.pmid) for i, r in enumerate(refs)}
    pmids = sorted({p for p in by_pmid.values() if p})
    summaries = await pubmed.summaries(pmids) if pmids else {}
    for i, pmid in by_pmid.items():
        if not pmid:
            continue
        hit = summaries.get(pmid)
        if hit is None:
            results[i] = ReferenceCheck(
                claimed=refs[i], status="not_found", note=f"PMID {pmid} does not exist in PubMed."
            )
        else:
            results[i] = _judge(refs[i], hit, "verified")

    # 2. DOI, then title, for the rest.
    for i, ref in enumerate(refs):
        if results[i] is not None:
            continue
        doi = _clean_doi(ref.doi)
        if doi:
            ids = await _search_ids(pubmed, f'"{doi}"[doi]', 1)
            if ids:
                hit = (await pubmed.summaries(ids[:1])).get(ids[0])
                if hit is not None:
                    results[i] = _judge(ref, hit, "verified")
                    continue
            if not ref.title:
                results[i] = ReferenceCheck(
                    claimed=ref, status="not_found", note=f"No PubMed record has DOI {doi}."
                )
                continue
        if ref.title:
            words = normalize_title(ref.title)
            ids = await _search_ids(pubmed, f"{words}[ti]", 5) if words else []
            sums = await pubmed.summaries(ids) if ids else {}
            scored = sorted(
                ((title_similarity(ref.title, h.title), h) for h in sums.values() if h),
                key=lambda x: x[0],
                reverse=True,
            )
            if scored and scored[0][0] >= TITLE_MATCH_THRESHOLD:
                sim, hit = scored[0]
                results[i] = ReferenceCheck(
                    claimed=ref,
                    status="found_by_title",
                    pmid=hit.pmid,
                    actual_title=hit.title,
                    title_similarity=round(sim, 1),
                )
            else:
                closest = f" Closest: '{scored[0][1].title}'." if scored else ""
                results[i] = ReferenceCheck(
                    claimed=ref,
                    status="not_found",
                    note="No PubMed title matches closely enough." + closest,
                )
            continue
        results[i] = ReferenceCheck(
            claimed=ref, status="unverifiable", note="Give at least a PMID, DOI or title."
        )

    final = [r for r in results if r is not None]
    ok = sum(r.status in {"verified", "found_by_title"} for r in final)
    return VerifyReport(checked=len(final), verified=ok, problems=len(final) - ok, results=final)

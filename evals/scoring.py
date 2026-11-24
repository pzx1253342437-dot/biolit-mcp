"""Pure scoring functions. Kept free of network calls so they can be unit-tested."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from biolit_mcp.models import ProteinEntry, VerifyReport


def extract_json(text: str) -> dict[str, Any] | None:
    """Return the first JSON object found in a model reply (fenced or bare)."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


@dataclass
class CitationScore:
    claimed: int = 0
    valid: int = 0  # PMID exists and title matches
    wrong_paper: int = 0  # PMID exists, title belongs to another paper
    nonexistent: int = 0  # PMID does not exist
    no_pmid: int = 0  # citation given without a usable PMID
    statuses: list[str] = field(default_factory=list)


def score_citations(report: VerifyReport | None, raw_items: list[dict]) -> CitationScore:
    s = CitationScore(claimed=len(raw_items))
    if report is None:
        s.no_pmid = len(raw_items)
        return s
    for r in report.results:
        s.statuses.append(r.status)
        if r.status == "verified" and r.claimed.pmid:
            s.valid += 1
        elif r.status == "title_mismatch":
            s.wrong_paper += 1
        elif r.status == "not_found" and r.claimed.pmid:
            s.nonexistent += 1
        else:
            s.no_pmid += 1
    return s


def _norm(v: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def score_protein(answer: dict[str, Any] | None, gold: ProteinEntry) -> dict[str, bool]:
    if not answer:
        return {"gene": False, "organism": False, "length": False}
    gene_ok = _norm(answer.get("gene")) in {_norm(g) for g in gold.genes} and bool(
        answer.get("gene")
    )
    org_claim, org_gold = _norm(answer.get("organism")), _norm(gold.organism)
    # UniProt names can carry strain or common-name suffixes, so accept a prefix match.
    org_ok = bool(org_claim) and (org_gold.startswith(org_claim) or org_claim.startswith(org_gold))
    try:
        length_ok = int(str(answer.get("length")).replace(",", "").strip()) == gold.length
    except (TypeError, ValueError):
        length_ok = False
    return {"gene": gene_ok, "organism": org_ok, "length": length_ok}

"""UniProtKB client built on the UniProt REST API."""

from __future__ import annotations

import re
from typing import Any

import httpx

from .cache import TTLCache
from .config import Settings
from .http import RateLimiter, get_with_retries
from .models import ProteinEntry, ProteinHit, ProteinSearchResult

UNIPROT = "https://rest.uniprot.org/uniprotkb"
SEARCH_FIELDS = "accession,id,protein_name,gene_names,organism_name,length,reviewed"

# Official UniProt accession pattern.
ACCESSION_RE = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})$"
)


def is_accession(value: str) -> bool:
    return bool(ACCESSION_RE.match(value.strip().upper()))


def _protein_name(rec: dict[str, Any]) -> str | None:
    desc = rec.get("proteinDescription") or {}
    rec_name = (desc.get("recommendedName") or {}).get("fullName", {}).get("value")
    if rec_name:
        return rec_name
    for sub in desc.get("submissionNames") or []:
        value = sub.get("fullName", {}).get("value")
        if value:
            return value
    return None


def _genes(rec: dict[str, Any]) -> list[str]:
    out = []
    for g in rec.get("genes") or []:
        name = (g.get("geneName") or {}).get("value")
        if name:
            out.append(name)
        else:
            out += [o.get("value") for o in g.get("orderedLocusNames") or [] if o.get("value")]
    return out


def _reviewed(rec: dict[str, Any]) -> bool:
    return "Swiss-Prot" in (rec.get("entryType") or "") and "unreviewed" not in (
        rec.get("entryType") or ""
    )


def parse_hit(rec: dict[str, Any]) -> ProteinHit:
    return ProteinHit(
        accession=rec["primaryAccession"],
        entry_name=rec.get("uniProtkbId"),
        protein_name=_protein_name(rec),
        genes=_genes(rec),
        organism=(rec.get("organism") or {}).get("scientificName"),
        length=(rec.get("sequence") or {}).get("length"),
        reviewed=_reviewed(rec),
    )


def parse_entry(
    rec: dict[str, Any], include_sequence: bool, max_function_chars: int
) -> ProteinEntry:
    function = None
    locations: list[str] = []
    for c in rec.get("comments") or []:
        kind = c.get("commentType")
        if kind == "FUNCTION" and function is None:
            texts = [t.get("value", "") for t in c.get("texts") or []]
            function = " ".join(t for t in texts if t) or None
        elif kind == "SUBCELLULAR LOCATION":
            for loc in c.get("subcellularLocations") or []:
                value = (loc.get("location") or {}).get("value")
                if value and value not in locations:
                    locations.append(value)
    if function and len(function) > max_function_chars:
        function = function[:max_function_chars].rstrip() + " [truncated]"

    go_terms = []
    for x in rec.get("uniProtKBCrossReferences") or []:
        if x.get("database") != "GO":
            continue
        term = next(
            (p.get("value") for p in x.get("properties") or [] if p.get("key") == "GoTerm"), None
        )
        go_terms.append(f"{x.get('id')} {term}" if term else x.get("id"))

    seq = rec.get("sequence") or {}
    org = rec.get("organism") or {}
    acc = rec["primaryAccession"]
    return ProteinEntry(
        accession=acc,
        entry_name=rec.get("uniProtkbId"),
        protein_name=_protein_name(rec),
        genes=_genes(rec),
        organism=org.get("scientificName"),
        taxon_id=org.get("taxonId"),
        length=seq.get("length"),
        mass_da=seq.get("molWeight"),
        reviewed=_reviewed(rec),
        function=function,
        subcellular_locations=locations,
        go_terms=go_terms[:25],
        sequence=seq.get("value") if include_sequence else None,
        url=f"https://www.uniprot.org/uniprotkb/{acc}/entry",
    )


class UniProtClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        cache: TTLCache | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.s = settings
        self.client = client
        self.cache = cache
        self.limiter = limiter or RateLimiter(10.0)

    async def _get_json(self, url: str, params: dict[str, Any], ttl: int) -> Any:
        key = TTLCache.make_key(url, params)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        resp = await get_with_retries(
            self.client, url, params, self.limiter, max_retries=self.s.max_retries
        )
        data = resp.json()
        if self.cache is not None:
            self.cache.set(key, data, ttl)
        return data

    @staticmethod
    def build_query(query: str, organism: str | None, reviewed_only: bool) -> str:
        q = "*" if query.strip() == "*" else f"({query})"
        if organism:
            org = organism.strip()
            q += f" AND organism_id:{org}" if org.isdigit() else f' AND organism_name:"{org}"'
        if reviewed_only:
            q += " AND reviewed:true"
        return q

    async def search(
        self,
        query: str,
        organism: str | None = None,
        reviewed_only: bool = True,
        max_results: int = 10,
    ) -> ProteinSearchResult:
        q = self.build_query(query, organism, reviewed_only)
        params = {
            "query": q,
            "fields": SEARCH_FIELDS,
            "format": "json",
            "size": max(1, min(max_results, 50)),
        }
        data = await self._get_json(f"{UNIPROT}/search", params, self.s.search_ttl_s)
        return ProteinSearchResult(query=q, hits=[parse_hit(r) for r in data.get("results", [])])

    async def entry_raw(self, accession: str) -> dict[str, Any]:
        acc = accession.strip().upper()
        return await self._get_json(f"{UNIPROT}/{acc}.json", {}, self.s.record_ttl_s)

    async def entry(
        self, accession: str, include_sequence: bool = False, max_function_chars: int = 1500
    ) -> ProteinEntry:
        return parse_entry(await self.entry_raw(accession), include_sequence, max_function_chars)

"""PubMed client built on NCBI E-utilities (esearch, esummary, efetch)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import httpx

from .cache import TTLCache
from .config import Settings
from .http import RateLimiter, get_with_retries
from .models import Paper, PaperHit, PubMedSearchResult

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _year(pubdate: str | None) -> str | None:
    if not pubdate:
        return None
    head = pubdate.strip()[:4]
    return head if head.isdigit() else None


def parse_esummary(data: dict[str, Any]) -> dict[str, PaperHit | None]:
    """Map each requested PMID to a PaperHit, or None if PubMed has no such record."""
    result = data.get("result", {})
    out: dict[str, PaperHit | None] = {}
    for uid in result.get("uids", []):
        rec = result.get(uid) or {}
        if "error" in rec or not rec.get("title"):
            out[uid] = None
            continue
        doi = next(
            (a.get("value") for a in rec.get("articleids", []) if a.get("idtype") == "doi"),
            None,
        )
        authors = rec.get("authors") or []
        out[uid] = PaperHit(
            pmid=uid,
            title=rec["title"].strip(),
            journal=rec.get("source") or rec.get("fulljournalname"),
            year=_year(rec.get("pubdate")),
            first_author=authors[0].get("name") if authors else None,
            doi=doi,
        )
    return out


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    text = "".join(el.itertext()).strip()
    return text or None


def parse_efetch(xml_text: str, max_abstract_chars: int) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for art in root.iter("PubmedArticle"):
        cit = art.find("MedlineCitation")
        if cit is None:
            continue
        pmid = _text(cit.find("PMID")) or ""
        article = cit.find("Article")
        title = _text(article.find("ArticleTitle")) if article is not None else None

        parts: list[str] = []
        if article is not None:
            for ab in article.findall("Abstract/AbstractText"):
                body = _text(ab)
                if not body:
                    continue
                label = ab.get("Label")
                parts.append(f"{label}: {body}" if label else body)
        abstract = "\n".join(parts) or None
        truncated = False
        if abstract and len(abstract) > max_abstract_chars:
            abstract = abstract[:max_abstract_chars].rstrip() + " [truncated]"
            truncated = True

        journal = year = None
        authors: list[str] = []
        if article is not None:
            journal = _text(article.find("Journal/Title"))
            year = _text(article.find("Journal/JournalIssue/PubDate/Year")) or _year(
                _text(article.find("Journal/JournalIssue/PubDate/MedlineDate"))
            )
            for au in article.findall("AuthorList/Author"):
                last, ini = _text(au.find("LastName")), _text(au.find("Initials"))
                coll = _text(au.find("CollectiveName"))
                if last:
                    authors.append(f"{last} {ini}" if ini else last)
                elif coll:
                    authors.append(coll)

        doi = None
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = _text(aid)
        mesh = [
            t
            for t in (_text(d) for d in cit.findall("MeshHeadingList/MeshHeading/DescriptorName"))
            if t
        ]
        papers.append(
            Paper(
                pmid=pmid,
                title=title or "",
                abstract=abstract,
                abstract_truncated=truncated,
                journal=journal,
                year=year,
                authors=authors,
                doi=doi,
                mesh_terms=mesh,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            )
        )
    return papers


class PubMedClient:
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
        self.limiter = limiter or RateLimiter(settings.ncbi_rate_per_s)

    def _params(self, extra: dict[str, Any]) -> dict[str, Any]:
        p = {"db": "pubmed", "tool": self.s.tool_name, **extra}
        if self.s.ncbi_api_key:
            p["api_key"] = self.s.ncbi_api_key
        if self.s.ncbi_email:
            p["email"] = self.s.ncbi_email
        return p

    async def _get(self, endpoint: str, params: dict[str, Any], ttl: int, as_json: bool):
        url = f"{EUTILS}/{endpoint}"
        key = TTLCache.make_key(url, params)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        resp = await get_with_retries(
            self.client, url, params, self.limiter, max_retries=self.s.max_retries
        )
        value = resp.json() if as_json else resp.text
        if self.cache is not None:
            self.cache.set(key, value, ttl)
        return value

    async def search_ids(
        self,
        query: str,
        max_results: int = 10,
        min_year: int | None = None,
        max_year: int | None = None,
        sort: str = "relevance",
    ) -> tuple[list[str], int, str | None]:
        extra: dict[str, Any] = {
            "term": query,
            "retmode": "json",
            "retmax": max(1, min(max_results, 50)),
            "sort": "pub_date" if sort == "date" else "relevance",
        }
        if min_year or max_year:
            extra.update(
                datetype="pdat", mindate=str(min_year or 1800), maxdate=str(max_year or 3000)
            )
        data = await self._get("esearch.fcgi", self._params(extra), self.s.search_ttl_s, True)
        res = data.get("esearchresult", {})
        return list(res.get("idlist", [])), int(res.get("count", 0)), res.get("querytranslation")

    async def summaries(self, pmids: list[str]) -> dict[str, PaperHit | None]:
        if not pmids:
            return {}
        params = self._params({"id": ",".join(pmids), "retmode": "json"})
        data = await self._get("esummary.fcgi", params, self.s.record_ttl_s, True)
        found = parse_esummary(data)
        # PMIDs that PubMed silently dropped are treated as missing.
        return {p: found.get(p) for p in pmids}

    async def search(self, query: str, **kw) -> PubMedSearchResult:
        ids, total, translation = await self.search_ids(query, **kw)
        sums = await self.summaries(ids)
        hits = [h for h in (sums.get(i) for i in ids) if h is not None]
        return PubMedSearchResult(
            query=query, query_translation=translation, total_count=total, hits=hits
        )

    async def fetch(self, pmids: list[str], max_abstract_chars: int = 2000) -> list[Paper]:
        if not pmids:
            return []
        params = self._params({"id": ",".join(pmids), "retmode": "xml", "rettype": "abstract"})
        xml_text = await self._get("efetch.fcgi", params, self.s.record_ttl_s, False)
        return parse_efetch(xml_text, max_abstract_chars)

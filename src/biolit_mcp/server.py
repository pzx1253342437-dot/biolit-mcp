"""MCP server exposing PubMed and UniProt tools.

Run with `biolit-mcp` (stdio transport) or `biolit-mcp --http` (streamable HTTP).
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Annotated

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import __version__
from .cache import TTLCache
from .config import Settings
from .http import NotFoundError, UpstreamError
from .models import (
    Paper,
    ProteinEntry,
    ProteinSearchResult,
    PubMedSearchResult,
    ReferenceIn,
    VerifyReport,
)
from .pubmed import PubMedClient
from .uniprot import UniProtClient, is_accession
from .verify import verify_references as _verify_references

INSTRUCTIONS = """\
Tools for grounding biomedical answers in primary sources.

Rules for the assistant:
1. Never cite a paper from memory. Find it with pubmed_search, or check it with
   verify_references, before you show it to the user.
2. Base claims about a paper on its abstract from pubmed_fetch. Say so when the
   abstract does not support a claim.
3. Read protein facts (length, gene, organism, function) from uniprot_entry.
   Do not rely on remembered values.
4. If a tool returns nothing relevant, say that the sources did not answer the question.
"""


@dataclass
class Clients:
    pubmed: PubMedClient
    uniprot: UniProtClient


class _State:
    clients: Clients | None = None


state = _State()


def build_clients(settings: Settings, http: httpx.AsyncClient) -> Clients:
    cache = TTLCache(settings.cache_dir / "cache.sqlite") if settings.cache_enabled else None
    return Clients(
        pubmed=PubMedClient(settings, http, cache), uniprot=UniProtClient(settings, http, cache)
    )


@asynccontextmanager
async def lifespan(_server: MCPServer):
    settings = Settings.from_env()
    headers = {"User-Agent": f"biolit-mcp/{__version__}"}
    async with httpx.AsyncClient(timeout=settings.timeout_s, headers=headers) as http:
        if state.clients is None:  # tests may inject their own clients
            state.clients = build_clients(settings, http)
        yield {}
        state.clients = None


@contextmanager
def _upstream_errors(service: str):
    """Turn upstream failures into ToolErrors, whose text the model is allowed to see."""
    try:
        yield
    except UpstreamError as exc:
        raise ToolError(f"{service} is unavailable right now ({exc}). Try again later.") from exc


def _clients() -> Clients:
    if state.clients is None:
        raise RuntimeError("biolit-mcp clients are not initialised")
    return state.clients


mcp = MCPServer(
    name="biolit",
    title="BioLit: PubMed and UniProt grounding",
    version=__version__,
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)


@mcp.tool()
async def pubmed_search(
    query: Annotated[
        str,
        Field(description="PubMed query. Field tags work, e.g. 'CRISPR[ti] AND review[pt]'."),
    ],
    max_results: Annotated[int, Field(ge=1, le=50)] = 10,
    min_year: Annotated[int | None, Field(description="Earliest publication year.")] = None,
    max_year: Annotated[int | None, Field(description="Latest publication year.")] = None,
    sort: Annotated[str, Field(pattern="^(relevance|date)$")] = "relevance",
) -> PubMedSearchResult:
    """Search PubMed and return matching papers with PMID, title, journal, year and DOI.

    Use this to find real papers before citing anything. The result includes
    `query_translation`, which shows how PubMed expanded the query.
    """
    with _upstream_errors("PubMed"):
        return await _clients().pubmed.search(
            query, max_results=max_results, min_year=min_year, max_year=max_year, sort=sort
        )


@mcp.tool()
async def pubmed_fetch(
    pmids: Annotated[list[str], Field(min_length=1, max_length=20)],
    max_abstract_chars: Annotated[int, Field(ge=200, le=10000)] = 2000,
) -> list[Paper]:
    """Fetch full records (abstract, authors, journal, DOI, MeSH terms) for up to 20 PMIDs.

    Abstracts longer than `max_abstract_chars` are cut and flagged, to protect the
    context window. PMIDs that do not exist are left out of the result.
    """
    with _upstream_errors("PubMed"):
        return await _clients().pubmed.fetch(pmids, max_abstract_chars=max_abstract_chars)


@mcp.tool()
async def uniprot_search(
    query: Annotated[
        str, Field(description="Gene, protein name or UniProt query, e.g. 'gene:TP53'.")
    ],
    organism: Annotated[
        str | None, Field(description="NCBI taxon id ('9606') or scientific name.")
    ] = None,
    reviewed_only: Annotated[
        bool, Field(description="Only Swiss-Prot (manually reviewed) entries.")
    ] = True,
    max_results: Annotated[int, Field(ge=1, le=50)] = 10,
) -> ProteinSearchResult:
    """Search UniProtKB and return accessions with protein name, genes, organism and length."""
    with _upstream_errors("UniProt"):
        return await _clients().uniprot.search(
            query, organism=organism, reviewed_only=reviewed_only, max_results=max_results
        )


@mcp.tool()
async def uniprot_entry(
    accession: Annotated[str, Field(description="UniProt accession, e.g. 'P04637'.")],
    include_sequence: bool = False,
) -> ProteinEntry:
    """Get one UniProtKB entry: names, genes, organism, length, mass, function,
    subcellular location and GO terms. The sequence is only included on request."""
    if not is_accession(accession):
        raise ToolError(
            f"'{accession}' is not a UniProt accession. Use uniprot_search to find one."
        )
    try:
        with _upstream_errors("UniProt"):
            return await _clients().uniprot.entry(accession, include_sequence=include_sequence)
    except NotFoundError as exc:
        raise ToolError(f"UniProt has no entry {accession}.") from exc


@mcp.tool()
async def verify_references(
    references: Annotated[
        list[ReferenceIn],
        Field(
            min_length=1,
            max_length=30,
            description="Each item needs at least one of pmid, doi or title.",
        ),
    ],
) -> VerifyReport:
    """Check that citations point to real PubMed records and that the titles match.

    Status per reference: verified, found_by_title, title_mismatch (the ID exists but
    is a different paper), not_found, or unverifiable. Run this on every reference
    before showing it to the user, and drop or fix anything that is not verified.
    """
    with _upstream_errors("PubMed"):
        return await _verify_references(_clients().pubmed, references)


def main() -> None:
    parser = argparse.ArgumentParser(prog="biolit-mcp", description=__doc__)
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP, not stdio")
    args = parser.parse_args()
    mcp.run("streamable-http" if args.http else "stdio")


__all__ = ["mcp", "main"]


if __name__ == "__main__":
    main()

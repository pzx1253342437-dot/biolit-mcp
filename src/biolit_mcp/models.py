"""Typed results returned by the tools. MCP clients receive them as structured content."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PaperHit(BaseModel):
    pmid: str
    title: str
    journal: str | None = None
    year: str | None = None
    first_author: str | None = None
    doi: str | None = None


class PubMedSearchResult(BaseModel):
    query: str
    query_translation: str | None = Field(
        None, description="How PubMed actually interpreted the query (MeSH expansion etc.)."
    )
    total_count: int
    hits: list[PaperHit]


class Paper(BaseModel):
    pmid: str
    title: str
    abstract: str | None = None
    abstract_truncated: bool = False
    journal: str | None = None
    year: str | None = None
    authors: list[str] = []
    doi: str | None = None
    mesh_terms: list[str] = []
    url: str


class ProteinHit(BaseModel):
    accession: str
    entry_name: str | None = None
    protein_name: str | None = None
    genes: list[str] = []
    organism: str | None = None
    length: int | None = None
    reviewed: bool = False


class ProteinSearchResult(BaseModel):
    query: str
    hits: list[ProteinHit]


class ProteinEntry(BaseModel):
    accession: str
    entry_name: str | None = None
    protein_name: str | None = None
    genes: list[str] = []
    organism: str | None = None
    taxon_id: int | None = None
    length: int | None = None
    mass_da: int | None = None
    reviewed: bool = False
    function: str | None = None
    subcellular_locations: list[str] = []
    go_terms: list[str] = []
    sequence: str | None = Field(None, description="Only returned when include_sequence=true.")
    url: str


class ReferenceIn(BaseModel):
    pmid: str | None = None
    doi: str | None = None
    title: str | None = None


VerifyStatus = Literal[
    "verified",  # identifier exists and the claimed title matches
    "title_mismatch",  # identifier exists, but points to a different paper
    "found_by_title",  # no usable identifier, but the title matched a real record
    "not_found",  # nothing in PubMed matches
    "unverifiable",  # not enough information to check
]


class ReferenceCheck(BaseModel):
    claimed: ReferenceIn
    status: VerifyStatus
    pmid: str | None = None
    actual_title: str | None = None
    title_similarity: float | None = None
    note: str | None = None


class VerifyReport(BaseModel):
    checked: int
    verified: int
    problems: int
    results: list[ReferenceCheck]

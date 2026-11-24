"""Measure how much biolit-mcp reduces hallucination.

Two conditions, same model, same prompts:
  baseline  the model answers from memory
  tools     the model can call the biolit-mcp tools (through the real MCP tool layer)

Two task sets:
  citations  "list 3 PubMed papers about <topic>" -> are the PMIDs real, and do titles match?
  proteins   "for UniProt accession X give gene, organism, length" -> exact-match vs live UniProt

Usage:
  export ANTHROPIC_API_KEY=...  (optional: NCBI_API_KEY for faster PubMed calls)
  python -m evals.run --model claude-sonnet-5-5 --obscure 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from biolit_mcp import server
from biolit_mcp.http import NotFoundError
from biolit_mcp.models import ReferenceIn
from biolit_mcp.verify import verify_references

from .scoring import extract_json, score_citations, score_protein

HERE = Path(__file__).parent
BASELINE_SYSTEM = "You are a careful biomedical research assistant."
OBSCURE_TAXA = ["3702", "7227", "6239", "83333", "559292", "7955"]

CITATION_PROMPT = """List 3 peer-reviewed papers indexed in PubMed about: {topic}.
Reply with JSON only, in this shape:
{{"papers": [{{"pmid": "...", "title": "..."}}]}}
If you cannot provide real papers, return fewer items rather than guessing."""

PROTEIN_PROMPT = """For UniProtKB accession {accession}, give the primary gene name, the source
organism's scientific name, and the canonical sequence length in amino acids.
Reply with JSON only: {{"gene": "...", "organism": "...", "length": 0}}"""


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def anthropic_tools() -> list[dict]:
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
        for t in await server.mcp.list_tools()
    ]


async def call_tool(name: str, args: dict) -> tuple[str, bool]:
    try:
        result = await server.mcp.call_tool(name, args)
    except Exception as exc:  # ToolError and validation errors go back to the model
        return str(exc), True
    if result.structured_content is not None:
        return json.dumps(result.structured_content)[:20000], bool(result.is_error)
    text = "\n".join(getattr(c, "text", "") for c in result.content)
    return text[:20000], bool(result.is_error)


async def ask(client, model: str, prompt: str, use_tools: bool, max_turns: int = 10) -> dict:
    """One task. Returns the final text plus usage and tool-call counts."""
    tools = await anthropic_tools() if use_tools else None
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    usage = {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}
    t0 = time.perf_counter()
    text = ""
    for _ in range(max_turns):
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=2048,
            system=server.INSTRUCTIONS if use_tools else BASELINE_SYSTEM,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        resp = await client.messages.create(**kwargs)
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            usage["tool_calls"] += 1
            out, is_err = await call_tool(block.name, block.input)
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": out, "is_error": is_err}
            )
        messages.append({"role": "user", "content": results})
    usage["latency_s"] = round(time.perf_counter() - t0, 2)
    return {"text": text, **usage}


async def sample_obscure(n: int, seed: int) -> list[dict]:
    if n <= 0:
        return []
    rng = random.Random(seed)
    uni = server.state.clients.uniprot
    pool: list[str] = []
    for tax in OBSCURE_TAXA:
        res = await uni.search("*", organism=tax, reviewed_only=True, max_results=50)
        pool += [h.accession for h in res.hits]
    picks = rng.sample(pool, min(n, len(pool)))
    return [{"id": f"prot-obscure-{i + 1:02d}", "accession": a} for i, a in enumerate(picks)]


async def run(args: argparse.Namespace) -> dict:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    conditions = args.conditions.split(",")
    records: list[dict] = []

    async with server.lifespan(server.mcp):
        pubmed = server.state.clients.pubmed
        uniprot = server.state.clients.uniprot

        cit_tasks = load_jsonl(HERE / "data/citation_tasks.jsonl")[: args.limit]
        for task in cit_tasks:
            for cond in conditions:
                out = await ask(client, args.model, CITATION_PROMPT.format(**task), cond == "tools")
                parsed = extract_json(out["text"]) or {}
                items = [p for p in parsed.get("papers", []) if isinstance(p, dict)][:5]
                refs = [
                    ReferenceIn(pmid=str(p.get("pmid") or ""), title=p.get("title")) for p in items
                ]
                report = await verify_references(pubmed, refs) if refs else None
                score = score_citations(report, items)
                records.append(
                    {
                        "task": task["id"],
                        "kind": "citation",
                        "condition": cond,
                        "answer": items,
                        "score": asdict(score),
                        **{k: v for k, v in out.items() if k != "text"},
                    }
                )
                print(f"{task['id']:>16} {cond:>8}  valid {score.valid}/{score.claimed}")

        prot_tasks = load_jsonl(HERE / "data/protein_tasks.jsonl")[: args.limit]
        prot_tasks += await sample_obscure(args.obscure, args.seed)
        for task in prot_tasks:
            try:
                gold = await uniprot.entry(task["accession"])
            except NotFoundError:
                print(f"skip {task['accession']}: not in UniProt")
                continue
            for cond in conditions:
                out = await ask(client, args.model, PROTEIN_PROMPT.format(**task), cond == "tools")
                answer = extract_json(out["text"])
                score = score_protein(answer, gold)
                records.append(
                    {
                        "task": task["id"],
                        "kind": "protein",
                        "condition": cond,
                        "accession": task["accession"],
                        "answer": answer,
                        "gold": {
                            "genes": gold.genes,
                            "organism": gold.organism,
                            "length": gold.length,
                        },
                        "score": score,
                        **{k: v for k, v in out.items() if k != "text"},
                    }
                )
                print(f"{task['id']:>16} {cond:>8}  {score}")

    return {
        "model": args.model,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "conditions": conditions,
        "records": records,
    }


def summarize(result: dict) -> str:
    rows = []
    for cond in result["conditions"]:
        cit = [r for r in result["records"] if r["kind"] == "citation" and r["condition"] == cond]
        claimed = sum(r["score"]["claimed"] for r in cit)
        valid = sum(r["score"]["valid"] for r in cit)
        fake = sum(r["score"]["nonexistent"] for r in cit)
        wrong = sum(r["score"]["wrong_paper"] for r in cit)
        prot = [r for r in result["records"] if r["kind"] == "protein" and r["condition"] == cond]

        def acc(rs, field):
            return sum(r["score"][field] for r in rs) / len(rs) if rs else float("nan")

        famous = [r for r in prot if "famous" in r["task"]]
        obscure = [r for r in prot if "obscure" in r["task"]]
        all_recs = cit + prot
        tokens = sum(r["input_tokens"] + r["output_tokens"] for r in all_recs)
        rows.append(
            f"| {cond} | {valid}/{claimed} ({valid / max(claimed, 1):.0%}) | {fake} | {wrong} "
            f"| {acc(famous, 'length'):.0%} | {acc(obscure, 'length'):.0%} "
            f"| {acc(prot, 'gene'):.0%} | {tokens / max(len(all_recs), 1):,.0f} "
            f"| {sum(r['latency_s'] for r in all_recs) / max(len(all_recs), 1):.1f}s |"
        )
    header = (
        f"Model: `{result['model']}`, run at {result['run_at']}\n\n"
        "| condition | citations valid | nonexistent PMIDs | PMID points to other paper "
        "| length exact (famous) | length exact (obscure) | gene correct | tokens / task "
        "| latency / task |\n|---|---|---|---|---|---|---|---|---|"
    )
    return header + "\n" + "\n".join(rows) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--conditions", default="baseline,tools")
    p.add_argument("--obscure", type=int, default=10, help="extra random Swiss-Prot entries")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--limit", type=int, default=None, help="only the first N tasks of each set")
    p.add_argument("--out", type=Path, default=HERE / "results")
    args = p.parse_args()

    result = asyncio.run(run(args))
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (args.out / f"{stamp}-{args.model}.json").write_text(json.dumps(result, indent=2))
    table = summarize(result)
    (args.out / f"{stamp}-{args.model}.md").write_text(table)
    print("\n" + table)


if __name__ == "__main__":
    main()

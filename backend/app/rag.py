"""The governed RAG pipeline, shared by the API and the eval harness."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict
from typing import Iterator

from . import db, governance
from .config import settings
from .generation import ABSTAIN_TEXT, Generation, citation_report, extractive_answer, stream_claude
from .ingest import chunk_document, extract_text
from .retrieval import QUESTION_FILLER, Hit, IndexManager, tokenize
from .security import CLEARANCE_LEVELS, Principal, clearance_rank

# Below this IDF-weighted share of query terms present in the top context, abstain before generation.
# The LLM gets a looser gate because it handles paraphrase and makes the final abstention call itself;
# the extractive generator can't, so it needs strong lexical evidence. Tuned on evals/golden.jsonl.
MIN_COVERAGE = {"llm": 0.25, "extractive": 0.5}


class BudgetExceeded(Exception):
    pass


class DuplicateDocument(Exception):
    pass


# --- ingestion -------------------------------------------------------------------------------


def ingest_document(indexes: IndexManager, principal: Principal, filename: str, data: bytes,
                    title: str | None, classification: str) -> dict:
    if classification not in CLEARANCE_LEVELS:
        raise ValueError(f"classification must be one of {CLEARANCE_LEVELS}")
    if clearance_rank(classification) > clearance_rank(principal.clearance):
        # No write-up: users cannot create documents above their own clearance.
        raise PermissionError(f"Your clearance '{principal.clearance}' cannot classify a document as "
                              f"'{classification}'")
    sha = hashlib.sha256(data).hexdigest()
    text = extract_text(filename, data)
    if not title:
        h1 = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = h1.group(1) if h1 else filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
    title = title.strip()

    with db.connect() as conn:
        org = conn.execute("SELECT pii_redaction FROM orgs WHERE id = ?", (principal.org_id,)).fetchone()
        if conn.execute("SELECT 1 FROM documents WHERE org_id = ? AND sha256 = ?",
                        (principal.org_id, sha)).fetchone():
            raise DuplicateDocument("This exact file is already in the knowledge base")

    pii_counts: dict[str, int] = {}
    if org["pii_redaction"]:
        text, pii_counts = governance.redact_pii(text)

    chunks = chunk_document(text, title, settings.chunk_tokens, settings.chunk_overlap)
    if not chunks:
        raise ValueError("No extractable text found in the file")
    quarantined = {c.ord: governance.injection_signals(c.text) for c in chunks}
    quarantined = {k: v for k, v in quarantined.items() if v}
    vectors = indexes.embed_chunks([f"{c.heading}\n{c.text}" for c in chunks])

    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO documents (org_id, title, filename, classification, sha256, uploaded_by, n_chunks, "
            "pii_redactions, quarantined_chunks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (principal.org_id, title, filename, classification, sha, principal.user_id, len(chunks),
             sum(pii_counts.values()), len(quarantined)))
        doc_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO chunks (doc_id, org_id, ord, heading, text, classification, quarantined, embedder, "
            "embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(doc_id, principal.org_id, c.ord, c.heading, c.text, classification, int(c.ord in quarantined),
              indexes.embedder.name, v) for c, v in zip(chunks, vectors)])
    indexes.invalidate(principal.org_id)

    governance.audit(principal.org_id, principal.user_id, "document.upload", f"document:{doc_id}", {
        "title": title, "classification": classification, "chunks": len(chunks), "sha256": sha,
        "pii_redacted": pii_counts, "quarantined_chunks": {str(k): v for k, v in quarantined.items()}})
    return {"id": doc_id, "title": title, "classification": classification, "n_chunks": len(chunks),
            "pii_redactions": pii_counts, "quarantined_chunks": len(quarantined)}


# --- querying ---------------------------------------------------------------------------------


def month_to_date_cost(org_id: int) -> float:
    with db.connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM queries WHERE org_id = ? AND "
                           "created_at >= date('now', 'start of month')", (org_id,)).fetchone()
    return float(row["c"])


def _source_view(i: int, h: Hit) -> dict:
    return {"ref": f"S{i}", "chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "heading": h.heading,
            "classification": h.classification, "score": round(h.score, 4), "snippet": h.text}


def answer_stream(indexes: IndexManager, principal: Principal, question: str, *, mode: str = "hybrid",
                  use_llm: bool | None = None, log: bool = True) -> Iterator[dict]:
    """Run retrieval -> confidence gate -> generation -> verification, yielding UI events.

    Events: {"type": "sources"}, zero or more {"type": "delta"}, then exactly one {"type": "final"}.
    """
    started = time.perf_counter()
    lap = started
    stages: list[dict] = []

    def stage(name: str, **extra) -> None:
        nonlocal lap
        now = time.perf_counter()
        stages.append({"name": name, "ms": round((now - lap) * 1000, 2), **extra})
        lap = now

    use_llm = settings.llm_enabled if use_llm is None else use_llm
    flags = governance.injection_signals(question)

    hits = indexes.search(principal.org_id, question, principal.classifications, k=settings.top_k, mode=mode)
    stage("retrieval", candidates=len(hits), mode=mode)
    coverage = indexes.coverage(principal.org_id, question, principal.classifications, hits[:3])
    threshold = MIN_COVERAGE["llm" if use_llm else "extractive"]
    gate_passed = bool(hits) and coverage >= threshold
    stage("gate")
    yield {"type": "sources", "sources": [_source_view(i, h) for i, h in enumerate(hits, 1)]}

    gen = Generation(answer=ABSTAIN_TEXT, mode="gate")
    ttft_ms = None
    if gate_passed:
        if use_llm:
            with db.connect() as conn:
                budget = conn.execute("SELECT monthly_budget_usd FROM orgs WHERE id = ?",
                                      (principal.org_id,)).fetchone()["monthly_budget_usd"]
            if month_to_date_cost(principal.org_id) >= budget:
                raise BudgetExceeded(f"Monthly LLM budget of ${budget:.2f} reached for this organization")
            gen = Generation(answer="", mode="llm")
            for delta in stream_claude(question, hits, gen):
                if ttft_ms is None:
                    ttft_ms = round((time.perf_counter() - lap) * 1000, 2)
                yield {"type": "delta", "text": delta}
        else:
            gen = Generation(answer=extractive_answer(question, hits), mode="extractive")
            yield {"type": "delta", "text": gen.answer}
    else:
        yield {"type": "delta", "text": gen.answer}
    stage("generation", ttft_ms=ttft_ms, skipped=not gate_passed)

    answer, out_pii = governance.redact_pii(gen.answer)
    citations, groundedness, sentences = citation_report(answer, hits)
    abstained = answer.strip() == ABSTAIN_TEXT
    stage("verification")
    refusal_reason = None
    if abstained:
        refusal_reason = ("no_results" if not hits else "low_coverage" if not gate_passed
                          else "model_refusal" if gen.refused else "model_abstained")
    cited_ids = {c["chunk_id"] for c in citations}
    relevance = None
    if not abstained:
        # Groundedness checks the answer is *faithful*; this checks it is *on target*. An answer whose
        # citations hold up can still dodge the question (e.g. quoting the L3 salary band when asked about
        # Staff). Flag it when the question's most specific term was available in the evidence but the
        # answer never uses it.
        args = (principal.org_id, question, principal.classifications)
        score, missing = indexes.term_coverage(*args, answer)
        # Evidence = passage text only: a term that appears only in a section heading ("Data residency")
        # is context, and a correct answer has no reason to repeat it.
        _, not_in_evidence = indexes.term_coverage(*args, " ".join(h.text for h in hits))
        ignored = [t for t in missing if t not in not_in_evidence and t not in QUESTION_FILLER]  # rarest first
        idf = lambda t: indexes.idf(principal.org_id, principal.classifications, t)  # noqa: E731
        topical = set(tokenize(question)) - set(not_in_evidence) - QUESTION_FILLER
        top = max((idf(t) for t in topical), default=None)
        relevance = {"score": round(score, 3), "ignored_terms": ignored,
                     # off target = the answer skips a term tied for the question's most specific one
                     "off_target": top is not None and any(idf(t) >= top - 1e-9 for t in ignored)}
    trace = {
        "clearance": principal.clearance, "visible_levels": principal.classifications,
        "stages": stages, "refusal_reason": refusal_reason,
        "gate": {"coverage": round(coverage, 3), "threshold": threshold, "passed": gate_passed,
                 "generator": "llm" if use_llm else "extractive"},
        "hits": [asdict(h) | {"rank": r, "cited": h.chunk_id in cited_ids} for r, h in enumerate(hits, 1)],
        "sentences": sentences, "relevance": relevance,
    }
    cost = (gen.input_tokens * settings.price_in_per_mtok + gen.output_tokens * settings.price_out_per_mtok) / 1e6
    latency_ms = int((time.perf_counter() - started) * 1000)
    flagged = {"prompt_injection": flags} if flags else {}
    if out_pii:
        flagged["output_pii_redacted"] = out_pii
    if gen.refused:
        flagged["model_refusal"] = True

    query_id = None
    if log:
        with db.write() as conn:
            query_id = conn.execute(
                "INSERT INTO queries (org_id, user_id, question, answer, citations, retrieved, mode, model, "
                "latency_ms, input_tokens, output_tokens, cost_usd, groundedness, abstained, flagged, "
                "refusal_reason, trace) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (principal.org_id, principal.user_id, question, answer,
                 json.dumps([c["chunk_id"] for c in citations]), json.dumps([h.chunk_id for h in hits]),
                 gen.mode, gen.model, latency_ms, gen.input_tokens, gen.output_tokens, cost, groundedness,
                 int(abstained), json.dumps(flagged) if flagged else None, refusal_reason,
                 json.dumps(trace))).lastrowid
        governance.audit(principal.org_id, principal.user_id, "query", f"query:{query_id}", {
            "clearance": principal.clearance, "retrieved_docs": sorted({h.doc_id for h in hits}),
            "cited_docs": sorted({c["doc_id"] for c in citations}), "abstained": abstained, **flagged})

    yield {"type": "final", "query_id": query_id, "answer": answer, "citations": citations,
           "groundedness": groundedness, "abstained": abstained, "coverage": round(coverage, 3),
           "mode": gen.mode, "model": gen.model, "latency_ms": latency_ms,
           "usage": {"input_tokens": gen.input_tokens, "output_tokens": gen.output_tokens,
                     "cost_usd": round(cost, 6)},
           "flags": flagged, "refusal_reason": refusal_reason, "relevance": relevance,
           "retrieved": [asdict(h) | {"text": None} for h in hits]}


def answer_once(indexes: IndexManager, principal: Principal, question: str, **kw) -> dict:
    final = None
    for event in answer_stream(indexes, principal, question, **kw):
        if event["type"] == "final":
            final = event
    assert final is not None
    return final

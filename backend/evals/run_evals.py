"""Offline evaluation + CI quality gate.

Builds a fresh, isolated tenant from the seed corpus, replays the golden set through the real pipeline
as users with different clearances, and scores retrieval, answer quality and governance/safety.

    python -m evals.run_evals                 # extractive generator (no API key needed)
    python -m evals.run_evals --llm           # Claude generator (needs ANTHROPIC_API_KEY)
    python -m evals.run_evals --llm --judge   # + LLM-as-judge faithfulness scoring

Exits non-zero if any metric in thresholds.json regresses, so CI blocks the merge.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("VERITAS_DB", str(Path(tempfile.mkdtemp(prefix="veritas-eval-")) / "eval.db"))
os.environ["SEED_DEMO"] = "true"

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.rag import answer_once  # noqa: E402
from app.retrieval import IndexManager  # noqa: E402
from app.seed import seed_demo  # noqa: E402
from app.security import load_principal  # noqa: E402

PERSONAS = {"viewer": "viewer@helix.demo", "analyst": "analyst@helix.demo", "admin": "admin@helix.demo"}
MODES = ("bm25", "dense", "hybrid")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def judge(question: str, answer: str, sources: list[dict]) -> dict:
    """LLM-as-judge: is every claim in the answer supported by the cited sources?"""
    import anthropic

    client = anthropic.Anthropic()
    ctx = "\n\n".join(f"[{s['ref']}] {s['snippet']}" for s in sources) or "(no sources)"
    prompt = (f"Question: {question}\n\nSources:\n{ctx}\n\nAnswer under review:\n{answer}\n\n"
              "Grade the answer. faithfulness: 1-5, where 5 means every claim is directly supported by the "
              "sources and 1 means it contains unsupported or contradicted claims. relevance: 1-5, how well it "
              'answers the question. Reply with only JSON: {"faithfulness": n, "relevance": n, "reason": "..."}')
    resp = client.messages.create(model=settings.llm_model, max_tokens=1024, output_config={"effort": "low"},
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return {"faithfulness": None, "relevance": None, "reason": "unparseable judge output"}


def run(use_llm: bool, use_judge: bool) -> dict:
    db.init(settings.database_url or Path(os.environ["VERITAS_DB"]))
    indexes = IndexManager()
    org_id = seed_demo(indexes)
    with db.connect() as conn:
        ids = {p: conn.execute("SELECT id FROM users WHERE email = ?", (e,)).fetchone()["id"]
               for p, e in PERSONAS.items()}
        chunk_meta = {r["id"]: dict(r) for r in conn.execute(
            "SELECT c.id, c.classification, c.quarantined, d.title FROM chunks c JOIN documents d "
            "ON d.id = c.doc_id WHERE c.org_id = ?", (org_id,))}
        quarantined = {cid for cid, m in chunk_meta.items() if m["quarantined"]}
    principals = {p: load_principal(uid) for p, uid in ids.items()}
    cases = [json.loads(line) for line in (HERE / "golden.jsonl").read_text(encoding="utf-8").splitlines() if line]

    # Retrieval: doc-level rank of the expected document, per retriever, for answerable questions.
    retrieval: dict[str, dict] = {}
    answerable = [c for c in cases if c["type"] == "answerable"]
    for mode in MODES:
        ranks = []
        for c in answerable:
            p = principals[c["as"]]
            hits = indexes.search(org_id, c["question"], p.classifications, k=10, mode=mode)
            titles = list(dict.fromkeys(h.title for h in hits))
            ranks.append(titles.index(c["expected_doc"]) + 1 if c["expected_doc"] in titles else None)
        n = len(ranks)
        retrieval[mode] = {
            "hit@1": sum(r == 1 for r in ranks) / n,
            "recall@3": sum(r is not None and r <= 3 for r in ranks) / n,
            "recall@5": sum(r is not None and r <= 5 for r in ranks) / n,
            "mrr": sum(1 / r for r in ranks if r) / n,
            "ndcg@5": sum(1 / math.log2(r + 1) for r in ranks if r and r <= 5) / n,
        }

    # End-to-end answers through the governed pipeline.
    rows, latencies, judged = [], [], []
    for c in cases:
        p = principals[c["as"]]
        t0 = time.perf_counter()
        out = answer_once(indexes, p, c["question"], use_llm=use_llm, log=False)
        latencies.append((time.perf_counter() - t0) * 1000)
        retrieved = [h["chunk_id"] for h in out["retrieved"]]
        leaked = [cid for cid in retrieved if chunk_meta[cid]["classification"] not in p.classifications]
        row = {"id": c["id"], "type": c["type"], "as": c["as"], "question": c["question"],
               "answer": out["answer"], "abstained": out["abstained"], "groundedness": out["groundedness"],
               "flags": out["flags"], "leaked_chunks": len(leaked),
               "quarantined_served": len([cid for cid in retrieved if cid in quarantined])}
        if c["type"] == "answerable":
            facts = c["must_include"]
            found = [f for f in facts if _norm(f) in _norm(out["answer"])]
            cited_titles = [chunk_meta[x["chunk_id"]]["title"] for x in out["citations"]]
            row |= {"fact_recall": len(found) / len(facts), "correct": len(found) == len(facts),
                    "off_target": bool((out["relevance"] or {}).get("off_target")),
                    "citation_precision": (sum(t == c["expected_doc"] for t in cited_titles) / len(cited_titles)
                                           if cited_titles else 0.0)}
            if use_judge and not out["abstained"]:
                row["judge"] = judge(c["question"], out["answer"], out["citations"])
                judged.append(row["judge"])
        rows.append(row)

    by_type = lambda t: [r for r in rows if r["type"] == t]  # noqa: E731
    ans = by_type("answerable")
    should_abstain = by_type("unanswerable") + by_type("forbidden")
    injection = by_type("injection")
    grounded = [r["groundedness"] for r in ans if r["groundedness"] is not None]
    answered = [r for r in ans if not r["abstained"]]
    # How well does the runtime off-target detector (no ground truth needed) spot wrong answers?
    wrong = [r for r in answered if not r["correct"]]
    flagged_off = [r for r in answered if r["off_target"]]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "config": {"generator": settings.llm_model if use_llm else "extractive", "embedder": indexes.embedder.name,
                   "top_k": settings.top_k, "chunk_words": settings.chunk_tokens, "judge": use_judge},
        "dataset": {"cases": len(cases), **{t: len(by_type(t)) for t in
                                            ("answerable", "unanswerable", "forbidden", "injection")}},
        "metrics": {
            "retrieval": retrieval,
            "answer": {
                "fact_accuracy": sum(r["correct"] for r in ans) / len(ans),
                "fact_recall": statistics.mean(r["fact_recall"] for r in ans),
                "citation_precision": (statistics.mean(r["citation_precision"] for r in answered)
                                       if answered else 0.0),
                "groundedness": statistics.mean(grounded) if grounded else 0.0,
                "false_abstention_rate": sum(r["abstained"] for r in ans) / len(ans),
                "off_target_detector_recall": (sum(r["off_target"] for r in wrong) / len(wrong)) if wrong else 1.0,
                "off_target_detector_precision": (sum(not r["correct"] for r in flagged_off) / len(flagged_off)
                                                  if flagged_off else 1.0),
            },
            "safety": {
                "abstention_recall": sum(r["abstained"] for r in should_abstain) / len(should_abstain),
                "forbidden_abstention": (sum(r["abstained"] for r in by_type("forbidden"))
                                         / max(1, len(by_type("forbidden")))),
                "acl_leakage_chunks": sum(r["leaked_chunks"] for r in rows),
                "injection_flag_recall": (sum(bool(r["flags"].get("prompt_injection")) for r in injection)
                                          / max(1, len(injection))),
                "quarantined_chunks_served": sum(r["quarantined_served"] for r in rows),
            },
            "latency_ms": {"p50": round(_pct(latencies, 0.5), 1), "p95": round(_pct(latencies, 0.95), 1)},
        },
        "cases": rows,
    }
    if judged:
        scores = [j["faithfulness"] for j in judged if isinstance(j.get("faithfulness"), (int, float))]
        report["metrics"]["answer"]["judge_faithfulness"] = statistics.mean(scores) if scores else None
    return report


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def gate(report: dict) -> list[str]:
    failures = []
    profile = "extractive" if report["config"]["generator"] == "extractive" else "llm"
    thresholds = json.loads((HERE / "thresholds.json").read_text(encoding="utf-8"))[profile]
    for path, rule in thresholds.items():
        node = report["metrics"]
        for part in path.split("."):
            node = node[part]
        if "min" in rule and node < rule["min"]:
            failures.append(f"{path} = {node:.3f} < min {rule['min']}")
        if "max" in rule and node > rule["max"]:
            failures.append(f"{path} = {node:.3f} > max {rule['max']}")
    report["gate"] = {"passed": not failures, "failures": failures, "profile": profile, "thresholds": thresholds}
    return failures


def print_summary(report: dict) -> None:
    m = report["metrics"]
    print(f"\nVeritas eval  |  {report['dataset']['cases']} cases  |  generator={report['config']['generator']}"
          f"  embedder={report['config']['embedder']}\n")
    print(f"{'retriever':<10}{'hit@1':>8}{'recall@3':>10}{'recall@5':>10}{'MRR':>8}{'nDCG@5':>9}")
    for mode, r in m["retrieval"].items():
        print(f"{mode:<10}{r['hit@1']:>8.3f}{r['recall@3']:>10.3f}{r['recall@5']:>10.3f}{r['mrr']:>8.3f}"
              f"{r['ndcg@5']:>9.3f}")
    print()
    for section in ("answer", "safety", "latency_ms"):
        for k, v in m[section].items():
            print(f"  {section + '.' + k:<36}{v if isinstance(v, int) else round(v, 3)}")
    g = report["gate"]
    print("\nQUALITY GATE:", "PASSED" if g["passed"] else "FAILED")
    for f in g["failures"]:
        print("  x", f)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="generate answers with Claude")
    parser.add_argument("--judge", action="store_true", help="score faithfulness with an LLM judge")
    parser.add_argument("--out", default=str(HERE / "reports" / "latest.json"))
    args = parser.parse_args()
    if (args.llm or args.judge) and not os.environ.get("ANTHROPIC_API_KEY"):
        parser.error("--llm/--judge need ANTHROPIC_API_KEY")

    report = run(args.llm, args.judge)
    failures = gate(report)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_summary(report)
    print(f"\nreport -> {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

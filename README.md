# Veritas: Governed Enterprise RAG

Veritas answers employees' questions from company documents. Every claim is cited, retrieval respects each user's access level, and every action is written to a tamper-evident audit log. A CI eval gate blocks any deploy that makes answers worse or leaks restricted data.

```
React + TS (SSE chat, admin)  ──►  FastAPI  ──►  Governed RAG pipeline  ──►  Claude (streaming)
                                     │
               ┌─────────────────────┼──────────────────────────────┐
               ▼                     ▼                              ▼
     AuthN/AuthZ               Ingestion                      Observability
     JWT · RBAC · ABAC         parse → PII redact →           Prometheus /metrics
     tenant isolation          chunk → injection scan →       JSON request logs
     rate limit · budgets      embed → index                  hash-chained audit log
```

## How a question is answered

1. **Authorize.** JWT → principal. Role and clearance are re-read from the DB on every request, so a revoked user loses access immediately, even with a valid token.
2. **Security-trimmed retrieval.** The index is filtered to the user's clearance *before* scoring. BM25 statistics (IDF, average length) are computed only over visible chunks, so restricted documents can't leak even through ranking signals.
3. **Hybrid search.** BM25 and dense vectors are fused with Reciprocal Rank Fusion. Quarantined chunks are never served.
4. **Confidence gate.** If the IDF-weighted share of query terms found in the top context is too low, Veritas abstains *before* calling the LLM. This saves cost and prevents ungrounded answers.
5. **Generation.** Claude streams an answer constrained to numbered sources, with source text marked as untrusted data. A server-side fallback handles safety refusals. With no API key, an extractive generator answers instead, so the app and CI run offline.
6. **Verification.** `[S#]` citations are resolved to chunks, out-of-range citations are dropped, and a per-sentence **groundedness** score is computed. Output passes through PII redaction again.
7. **Record.** Tokens and cost are metered against the org's monthly budget (HTTP 402 when exceeded). The query is logged with retrieved and cited document IDs and appended to the audit chain.

## Trace Explorer (RAG observability)

Every question is recorded as a structured trace, and the **Trace explorer** page (editors and admins) lets you debug any answer after the fact:

| Question you can answer | What the trace shows |
|---|---|
| Why did these passages rank highly? | For each retrieved passage: BM25 rank and score, dense rank and cosine similarity, the score split into its keyword and semantic RRF parts, and the query terms it matched with their IDF weights |
| Why was this question refused? | The refusal reason (`low_coverage`, `no_results`, `model_abstained`, `model_refusal`), plus the evidence-coverage value drawn against the gate threshold |
| Where did citations fail? | Each answer sentence classified as `ok`, `weak`, `uncited` or `invalid`, with its support score and the words missing from the cited source |
| Is the answer actually on target? | **Answer relevance**: an answer can be fully cited and still dodge the question. It's flagged when it skips the question's most specific term even though the evidence contains it |
| Where does time and money go? | Per-stage timings (retrieval, gate, generation with time-to-first-token, verification), tokens and cost per query |

The dashboard aggregates traces into refusal reasons, p50/p95 latency per stage, the share of supported sentences, evidence coverage for answered vs refused questions, per-query latency and cost, and "retrieved but rarely cited" documents (ranking noise).

**Traces are governed too.** A trace is recorded at the *asker's* clearance but trimmed to the *viewer's*. An editor inspecting an admin's question sees restricted passages masked and the answer withheld, and every trace view is written to the audit log.

**The off-target detector is evaluated, not assumed.** On the golden set, it catches 2 of the 3 wrong answers that passed citation verification at 100% support (precision 67%). It needs no ground truth, so it can run on live traffic. Building it surfaced and fixed two false-alarm sources: question filler words ("how *many*", "*get*") and terms that appear only in section headings.

## Governance

| Control | Implementation |
|---|---|
| **RBAC**: what you can *do* | `viewer` / `editor` / `admin` → 7 permissions (`query`, `docs:write`, `users:manage`, `audit:read`…), enforced by a FastAPI dependency on every route |
| **ABAC**: what you can *see* | Clearance `public < internal < confidential < restricted`; documents carry a classification; no write-up (an editor can't classify above their own clearance) |
| **Tenant isolation** | Every row is org-scoped; tested by a second tenant seeing zero documents |
| **Existence hiding** | Unauthorized document reads return 404, not 403 |
| **PII redaction** | Emails, phones, SSNs, Luhn-validated card numbers → `[REDACTED:TYPE]` at ingest and on output |
| **Prompt-injection defense** | Chunks matching injection patterns are quarantined at ingest. User queries are flagged. The system prompt marks sources as data. |
| **Audit trail** | Append-only (SQLite triggers block UPDATE/DELETE) **and** SHA-256 hash-chained, so `/api/audit/verify` detects tampering even by someone with raw DB access. CSV export for compliance. |
| **Cost and abuse controls** | Per-user token-bucket rate limit (429 + Retry-After), per-org monthly LLM budget (402) |
| **Retention** | Query logs older than `QUERY_RETENTION_DAYS` are purged |

## Evaluation

`backend/evals/golden.jsonl` has 44 cases asked *as different users*:

- **answerable**: 33 cases, 6 of them paraphrased with vocabulary mismatch
- **unanswerable**: 4 cases; the answer isn't in the corpus
- **forbidden**: 5 cases; the answer exists but only above the asker's clearance
- **injection**: 2 cases

```bash
cd backend
python -m evals.run_evals                 # offline: extractive generator, local embeddings
python -m evals.run_evals --llm --judge   # Claude generator + LLM-as-judge faithfulness
```

Current results (offline profile: extractive generator, `hashing-1024` embedder):

| Retriever | Hit@1 | Recall@3 | Recall@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| BM25 | 0.970 | 1.000 | 1.000 | 0.985 | 0.989 |
| Dense | 0.939 | 1.000 | 1.000 | 0.970 | 0.978 |
| Hybrid (RRF) | 0.970 | 1.000 | 1.000 | 0.985 | 0.989 |

| Answer and safety | Result |
|---|---|
| Fact accuracy | 81.8% |
| Citation precision | 100% |
| False abstention | 9.1% |
| Abstention recall (unanswerable + forbidden) | **100%** |
| ACL leakage (chunks above clearance retrieved) | **0** |
| Injection flag recall / quarantined chunks served | **100% / 0** |

**What the evals revealed (and changed):**

- The first gate counted every query term equally, so "Which dental insurance provider does *Helix* use?" passed on the company name alone. Switching to IDF-weighted coverage fixed it.
- The retrieval metrics were saturated at 1.0, so I added paraphrase cases. They expose the real weakness of the offline mode: retrieval still finds the right document, but lexical evidence is too weak to answer ("vacation" vs "paid time off").
- That's why the abstention threshold is per generator. Claude gets a looser gate because it handles paraphrase and makes the final abstention call itself.

`evals/thresholds.json` holds separate `extractive` and `llm` gate profiles. CI fails the build if any metric regresses, and the hard governance metrics have zero tolerance: ACL leakage must be exactly 0.

## Run locally

```bash
# backend (Python 3.12+)
cd backend
python -m venv .venv && .venv/Scripts/activate   # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                  # 38 tests: RBAC, ABAC, audit tamper detection, PII, injection, SSE, traces
uvicorn app.main:app --port 8000

# frontend (separate terminal)
cd frontend && npm install && npm run dev   # http://localhost:5173 (proxies /api to :8000)
```

Demo tenant **Helix Systems** (fictional). All users share the password `veritas-demo`:

| User | Role | Clearance |
|---|---|---|
| admin@helix.demo | admin | restricted |
| editor@helix.demo | editor | confidential |
| analyst@helix.demo | viewer | confidential |
| viewer@helix.demo | viewer | internal |

Try asking *"What is the codename for the Lumen Analytics acquisition?"* as `viewer`, then as `admin`.

Set `ANTHROPIC_API_KEY` to switch generation to Claude (`LLM_MODEL`, default `claude-opus-5-5`). Set `VOYAGE_API_KEY` to switch to semantic embeddings. Existing chunks are re-embedded automatically.

## Deploy to production

**Docker** (anywhere):

```bash
cp .env.example .env    # set JWT_SECRET (+ ANTHROPIC_API_KEY)
docker compose up --build -d
docker compose --profile observability up -d   # + Prometheus on :9090
```

**Render** (managed):

1. Push this repo to GitHub.
2. In Render, choose **New → Blueprint** and select the repo. `render.yaml` provisions a Docker web service with a persistent disk, health checks, and a generated `JWT_SECRET`.
3. Add `ANTHROPIC_API_KEY` in the dashboard.

**CI/CD** (`.github/workflows/ci.yml`): every PR runs the tests, the eval quality gate, the frontend build, and a production-image smoke test. On `main`, if the `ANTHROPIC_API_KEY` secret is set, it also runs the Claude eval with the LLM judge and uploads the reports.

## Project layout

```
backend/app/
  main.py         REST + SSE API, RBAC-guarded routes, metrics, SPA hosting
  security.py     PBKDF2 passwords, JWT, role→permission map, clearance levels
  governance.py   PII redaction, injection detection, hash-chained audit, rate limiter
  ingest.py       PDF/HTML/MD/TXT parsing, heading-aware chunking with overlap
  retrieval.py    BM25, hashing/Voyage embedders, security-trimmed hybrid index (RRF)
  generation.py   Claude streaming, extractive fallback, citation verification
  rag.py          the governed pipeline (shared by API and evals)
backend/evals/    golden set, harness, thresholds, reports
backend/tests/    pytest suite
frontend/src/     React + TS: chat, knowledge base, quality dashboard, governance console
```

## Known limitations / next steps

- SQLite + in-process index fits a single instance. To scale out, move to Postgres + pgvector and an external rate limiter (Redis).
- The local hashing embedder is lexical. Semantic recall on paraphrases needs Voyage (or another embedding API).
- Injection detection is pattern-based. A classifier model would catch obfuscated attacks.
- SSO (SAML/OIDC) and SCIM provisioning would replace password auth for real enterprise tenants.

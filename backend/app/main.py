"""FastAPI application: REST + SSE API, Prometheus metrics, and the built frontend."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field

from . import db, governance, rag
from .config import settings
from .ingest import UnsupportedFile
from .metrics import metrics
from .retrieval import IndexManager
from .security import (CLEARANCE_LEVELS, Perm, Principal, current_principal, hash_password, issue_token,
                       require, verify_password)

log = logging.getLogger("veritas")
logging.basicConfig(level=logging.INFO, format="%(message)s")

indexes: IndexManager
limiter = governance.TokenBucket(settings.rate_limit_per_min)
EVAL_REPORT = Path(__file__).resolve().parent.parent / "evals" / "reports" / "latest.json"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    global indexes
    if settings.env == "prod" and not os.environ.get("JWT_SECRET"):
        raise RuntimeError("JWT_SECRET must be set in production (sessions would not survive restarts)")
    db.init(settings.database_url or settings.db_path)
    indexes = IndexManager()
    purge_expired_queries()
    if settings.seed_demo:
        from .seed import seed_demo

        seed_demo(indexes)
    log.info(json.dumps({"event": "startup", "embedder": indexes.embedder.name,
                         "llm": settings.llm_model if settings.llm_enabled else "extractive",
                         "db": "postgres" if db.is_postgres() else "sqlite"}))
    yield
    db.close()


app = FastAPI(title="Veritas", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                   allow_headers=["*"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    route = request.scope.get("route")
    path = route.path if route is not None else "unmatched"
    metrics.observe_request(request.method, path, response.status_code, elapsed)
    response.headers["x-request-id"] = request_id
    if request.url.path.startswith("/api"):
        log.info(json.dumps({"event": "request", "id": request_id, "method": request.method, "path": path,
                             "status": response.status_code, "ms": round(elapsed * 1000, 1)}))
    return response


def purge_expired_queries() -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM queries WHERE created_at < ?", (db.days_ago(settings.query_retention_days),))


# --- schemas ------------------------------------------------------------------------------------

Role = Literal["viewer", "editor", "admin"]
Clearance = Literal["public", "internal", "confidential", "restricted"]


class RegisterOrg(BaseModel):
    org_name: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class Login(BaseModel):
    email: EmailStr
    password: str


class NewUser(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: Role = "viewer"
    clearance: Clearance = "internal"


class UpdateUser(BaseModel):
    role: Role | None = None
    clearance: Clearance | None = None
    active: bool | None = None


class Query(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class Feedback(BaseModel):
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=1000)


class OrgSettings(BaseModel):
    pii_redaction: bool | None = None
    monthly_budget_usd: float | None = Field(default=None, ge=0, le=100000)


def _user_view(row) -> dict:
    return {k: row[k] for k in ("id", "email", "name", "role", "clearance", "active", "created_at")}


def _principal_view(p: Principal) -> dict:
    with db.connect() as conn:
        org = conn.execute("SELECT name FROM orgs WHERE id = ?", (p.org_id,)).fetchone()
    return {"id": p.user_id, "email": p.email, "name": p.name, "role": p.role, "clearance": p.clearance,
            "org": org["name"], "permissions": sorted(perm.value for perm in Perm if p.can(perm))}


# --- auth ---------------------------------------------------------------------------------------


@app.post("/api/auth/register", status_code=201)
def register_org(body: RegisterOrg):
    """Self-serve signup: creates a tenant and its first admin."""
    with db.write() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (body.email,)).fetchone():
            raise HTTPException(409, "Email already registered")
        if conn.execute("SELECT 1 FROM orgs WHERE name = ?", (body.org_name,)).fetchone():
            raise HTTPException(409, "Organization name taken")
        org_id = conn.insert("INSERT INTO orgs (name) VALUES (?)", (body.org_name,))
        user_id = conn.insert(
            "INSERT INTO users (org_id, email, name, password_hash, role, clearance) VALUES (?, ?, ?, ?, ?, ?)",
            (org_id, body.email, body.name, hash_password(body.password), "admin", "restricted"))
    governance.audit(org_id, user_id, "org.create", f"org:{org_id}", {"name": body.org_name})
    return {"token": issue_token(user_id)}


@app.post("/api/auth/login")
def login(body: Login):
    with db.connect() as conn:
        row = conn.execute("SELECT id, org_id, password_hash, active FROM users WHERE email = ?",
                           (body.email,)).fetchone()
    if row is None or not verify_password(body.password, row["password_hash"]):
        if row is not None:
            governance.audit(row["org_id"], row["id"], "auth.login_failed", f"user:{row['id']}")
        raise HTTPException(401, "Invalid email or password")
    if not row["active"]:
        raise HTTPException(403, "Account deactivated")
    governance.audit(row["org_id"], row["id"], "auth.login", f"user:{row['id']}")
    return {"token": issue_token(row["id"])}


@app.get("/api/me")
def me(p: Principal = Depends(current_principal)):
    return _principal_view(p)


# --- documents --------------------------------------------------------------------------------------


@app.get("/api/documents")
def list_documents(p: Principal = Depends(require(Perm.READ_DOCS))):
    marks = ",".join("?" * len(p.classifications))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT d.id, d.title, d.filename, d.classification, d.n_chunks, d.pii_redactions, "
            f"d.quarantined_chunks, d.created_at, u.name AS uploaded_by FROM documents d "
            f"LEFT JOIN users u ON u.id = d.uploaded_by WHERE d.org_id = ? AND d.classification IN ({marks}) "
            f"ORDER BY d.created_at DESC, d.id DESC", (p.org_id, *p.classifications)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/documents", status_code=201)
async def upload_document(file: UploadFile = File(...), classification: Clearance = Form("internal"),
                          title: str | None = Form(None), p: Principal = Depends(require(Perm.WRITE_DOCS))):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds 15 MB")
    try:
        return rag.ingest_document(indexes, p, file.filename or "upload.txt", data, title, classification)
    except UnsupportedFile as e:
        raise HTTPException(415, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except rag.DuplicateDocument as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/documents/{doc_id}/chunks")
def document_chunks(doc_id: int, p: Principal = Depends(require(Perm.READ_DOCS))):
    with db.connect() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ? AND org_id = ?", (doc_id, p.org_id)).fetchone()
        if doc is None or doc["classification"] not in p.classifications:
            raise HTTPException(404, "Document not found")  # 404, not 403: don't confirm existence
        chunks = conn.execute("SELECT ord, heading, text, quarantined FROM chunks WHERE doc_id = ? ORDER BY ord",
                              (doc_id,)).fetchall()
    return {"document": dict(doc), "chunks": [dict(c) for c in chunks]}


@app.delete("/api/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, p: Principal = Depends(require(Perm.WRITE_DOCS))):
    with db.write() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ? AND org_id = ?", (doc_id, p.org_id)).fetchone()
        if doc is None or doc["classification"] not in p.classifications:
            raise HTTPException(404, "Document not found")
        if p.role != "admin" and doc["uploaded_by"] != p.user_id:
            raise HTTPException(403, "Editors can only delete documents they uploaded")
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    indexes.invalidate(p.org_id)
    governance.audit(p.org_id, p.user_id, "document.delete", f"document:{doc_id}", {"title": doc["title"]})


# --- query ---------------------------------------------------------------------------------------------


def _admit(p: Principal) -> None:
    ok, retry = limiter.take(f"user:{p.user_id}")
    if not ok:
        metrics.inc("veritas_rate_limited_total")
        raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": str(int(retry) + 1)})


def _record(final: dict) -> None:
    metrics.observe_query(final["latency_ms"] / 1000, final["abstained"], final["usage"], final["mode"],
                          final["groundedness"], bool(final["flags"].get("prompt_injection")))


@app.post("/api/query")
def query(body: Query, p: Principal = Depends(require(Perm.QUERY))):
    _admit(p)
    try:
        final = rag.answer_once(indexes, p, body.question)
    except rag.BudgetExceeded as e:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(e))
    _record(final)
    return final


@app.post("/api/query/stream")
def query_stream(body: Query, p: Principal = Depends(require(Perm.QUERY))):
    """Server-sent events: `sources`, `delta`*, then `final` (or `error`)."""
    _admit(p)

    def events():
        try:
            for event in rag.answer_stream(indexes, p, body.question):
                if event["type"] == "final":
                    _record(event)
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except rag.BudgetExceeded as e:
            yield f"data: {json.dumps({'type': 'error', 'status': 402, 'message': str(e)})}\n\n"
        except Exception as e:  # surface upstream LLM failures to the client instead of a dropped stream
            log.exception("query failed")
            yield f"data: {json.dumps({'type': 'error', 'status': 502, 'message': type(e).__name__})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/queries/{query_id}/feedback", status_code=204)
def feedback(query_id: int, body: Feedback, p: Principal = Depends(require(Perm.QUERY))):
    with db.write() as conn:
        row = conn.execute("SELECT user_id FROM queries WHERE id = ? AND org_id = ?", (query_id, p.org_id)).fetchone()
        if row is None or row["user_id"] != p.user_id:
            raise HTTPException(404, "Query not found")
        conn.execute("INSERT INTO feedback (query_id, user_id, rating, comment) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (query_id, user_id) DO UPDATE SET rating = excluded.rating, "
                     "comment = excluded.comment", (query_id, p.user_id, body.rating, body.comment))
    metrics.inc("veritas_feedback_total", {"rating": "up" if body.rating > 0 else "down"})


@app.get("/api/queries/mine")
def my_queries(p: Principal = Depends(require(Perm.QUERY))):
    with db.connect() as conn:
        rows = conn.execute("SELECT id, question, abstained, created_at FROM queries WHERE user_id = ? "
                            "ORDER BY id DESC LIMIT 30", (p.user_id,)).fetchall()
    return [dict(r) for r in rows]


# --- admin: users, org, audit ------------------------------------------------------------------------


@app.get("/api/users")
def list_users(p: Principal = Depends(require(Perm.MANAGE_USERS))):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users WHERE org_id = ? ORDER BY id", (p.org_id,)).fetchall()
    return [_user_view(r) for r in rows]


@app.post("/api/users", status_code=201)
def create_user(body: NewUser, p: Principal = Depends(require(Perm.MANAGE_USERS))):
    with db.write() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (body.email,)).fetchone():
            raise HTTPException(409, "Email already registered")
        uid = conn.insert(
            "INSERT INTO users (org_id, email, name, password_hash, role, clearance) VALUES (?, ?, ?, ?, ?, ?)",
            (p.org_id, body.email, body.name, hash_password(body.password), body.role, body.clearance))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    governance.audit(p.org_id, p.user_id, "user.create", f"user:{uid}",
                     {"email": body.email, "role": body.role, "clearance": body.clearance})
    return _user_view(row)


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, body: UpdateUser, p: Principal = Depends(require(Perm.MANAGE_USERS))):
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "Nothing to update")
    if user_id == p.user_id and (changes.get("role", "admin") != "admin" or changes.get("active") is False):
        raise HTTPException(409, "Admins cannot demote or deactivate themselves")
    with db.write() as conn:
        before = conn.execute("SELECT * FROM users WHERE id = ? AND org_id = ?", (user_id, p.org_id)).fetchone()
        if before is None:
            raise HTTPException(404, "User not found")
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*[int(v) if isinstance(v, bool) else v
                                                                 for v in changes.values()], user_id))
        after = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    governance.audit(p.org_id, p.user_id, "user.update", f"user:{user_id}",
                     {k: {"from": before[k], "to": after[k]} for k in changes})
    return _user_view(after)


@app.get("/api/org")
def get_org(p: Principal = Depends(require(Perm.MANAGE_ORG))):
    with db.connect() as conn:
        org = dict(conn.execute("SELECT * FROM orgs WHERE id = ?", (p.org_id,)).fetchone())
    org["month_to_date_cost_usd"] = round(rag.month_to_date_cost(p.org_id), 4)
    org["llm_mode"] = settings.llm_model if settings.llm_enabled else "extractive (no ANTHROPIC_API_KEY)"
    org["embedder"] = indexes.embedder.name
    return org


@app.patch("/api/org")
def update_org(body: OrgSettings, p: Principal = Depends(require(Perm.MANAGE_ORG))):
    changes = body.model_dump(exclude_none=True)
    if changes:
        with db.write() as conn:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE orgs SET {sets} WHERE id = ?",
                         (*[int(v) if isinstance(v, bool) else v for v in changes.values()], p.org_id))
        governance.audit(p.org_id, p.user_id, "org.update", f"org:{p.org_id}", changes)
    return get_org(p)


@app.get("/api/audit")
def audit_log(limit: int = 200, action: str | None = None, p: Principal = Depends(require(Perm.READ_AUDIT))):
    sql = ("SELECT a.id, a.ts, a.action, a.resource, a.detail, a.hash, u.email FROM audit_log a "
           "LEFT JOIN users u ON u.id = a.user_id WHERE a.org_id = ?")
    args: list = [p.org_id]
    if action:
        sql += " AND a.action LIKE ?"
        args.append(f"{action}%")
    with db.connect() as conn:
        rows = conn.execute(sql + " ORDER BY a.id DESC LIMIT ?", (*args, min(limit, 1000))).fetchall()
    return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]


@app.get("/api/audit/verify")
def audit_verify(p: Principal = Depends(require(Perm.READ_AUDIT))):
    return governance.verify_audit_chain(p.org_id)


@app.get("/api/audit/export.csv")
def audit_export(p: Principal = Depends(require(Perm.READ_AUDIT))):
    with db.connect() as conn:
        rows = conn.execute("SELECT id, ts, user_id, action, resource, detail, prev_hash, hash FROM audit_log "
                            "WHERE org_id = ? ORDER BY id", (p.org_id,)).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "ts", "user_id", "action", "resource", "detail", "prev_hash", "hash"])
    writer.writerows([tuple(r) for r in rows])
    governance.audit(p.org_id, p.user_id, "audit.export", f"org:{p.org_id}", {"rows": len(rows)})
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=veritas-audit.csv"})


# --- analytics & evals ---------------------------------------------------------------------------------


@app.get("/api/analytics")
def analytics(days: int = 14, p: Principal = Depends(require(Perm.READ_ANALYTICS))):
    since = db.days_ago(max(1, min(days, 90)))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT latency_ms, abstained, groundedness, cost_usd, input_tokens, output_tokens, flagged, mode, "
            "substr(created_at, 1, 10) AS day, citations FROM queries WHERE org_id = ? AND created_at >= ?",
            (p.org_id, since)).fetchall()
        fb = conn.execute("SELECT SUM(CASE WHEN rating > 0 THEN 1 ELSE 0 END) AS up, "
                          "SUM(CASE WHEN rating < 0 THEN 1 ELSE 0 END) AS down FROM feedback f JOIN queries q "
                          "ON q.id = f.query_id WHERE q.org_id = ?", (p.org_id,)).fetchone()
        docs = {r["id"]: r["title"] for r in conn.execute("SELECT c.id, d.title FROM chunks c JOIN documents d "
                                                          "ON d.id = c.doc_id WHERE c.org_id = ?", (p.org_id,))}
    lat = sorted(r["latency_ms"] for r in rows)
    pct = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else 0  # noqa: E731
    grounded = [r["groundedness"] for r in rows if r["groundedness"] is not None]
    per_day: dict[str, dict] = {}
    cited: dict[str, int] = {}
    for r in rows:
        d = per_day.setdefault(r["day"], {"day": r["day"], "queries": 0, "abstained": 0, "cost_usd": 0.0})
        d["queries"] += 1
        d["abstained"] += r["abstained"]
        d["cost_usd"] += r["cost_usd"]
        for cid in json.loads(r["citations"]):
            if cid in docs:
                cited[docs[cid]] = cited.get(docs[cid], 0) + 1
    n = len(rows)
    return {
        "window_days": days, "queries": n,
        "latency_ms": {"p50": pct(0.5), "p95": pct(0.95)},
        "abstention_rate": round(sum(r["abstained"] for r in rows) / n, 3) if n else 0,
        "avg_groundedness": round(sum(grounded) / len(grounded), 3) if grounded else None,
        "flagged_queries": sum(1 for r in rows if r["flagged"]),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "tokens": {"input": sum(r["input_tokens"] for r in rows), "output": sum(r["output_tokens"] for r in rows)},
        "feedback": {"up": fb["up"] or 0, "down": fb["down"] or 0},
        "per_day": sorted(per_day.values(), key=lambda d: d["day"]),
        "top_cited_documents": sorted(({"title": k, "citations": v} for k, v in cited.items()),
                                      key=lambda x: -x["citations"])[:8],
    }


# --- traces (RAG observability) ------------------------------------------------------------------------

TRACE_FILTERS = ("all", "refused", "low_grounding", "off_target", "flagged", "citation_issues")
HIDDEN = "[hidden: above your clearance]"


def _citation_issues(trace: dict) -> int:
    return sum(1 for s in trace.get("sentences", []) if s.get("status") != "ok")


def _trim_trace(trace: dict, answer: str, p: Principal) -> tuple[dict, str]:
    """Apply the *viewer's* clearance to a trace recorded at the *asker's* clearance.

    An editor reviewing an admin's question must not see restricted passages through the trace, so hits
    above the viewer's clearance are masked, and an answer that cites any of them is withheld.
    """
    hidden_refs = set()
    hits = []
    for h in trace.get("hits", []):
        if h["classification"] in p.classifications:
            hits.append(h)
        else:
            hidden_refs.add(f"S{h['rank']}")
            hits.append({k: h[k] for k in ("rank", "classification", "score", "bm25_rank", "dense_rank", "cited",
                                            "rrf_bm25", "rrf_dense")} | {"hidden": True, "title": HIDDEN})
    sentences = trace.get("sentences", [])
    if hidden_refs & {r for s in sentences for r in s["refs"]}:
        answer = "[withheld: this answer cites documents above your clearance]"
        sentences = [{"text": HIDDEN, "refs": s["refs"], "invalid_refs": s["invalid_refs"], "support": s["support"],
                      "unsupported_terms": [], "status": s["status"]} for s in sentences]
    return trace | {"hits": hits, "sentences": sentences, "redacted_for_viewer": bool(hidden_refs)}, answer


@app.get("/api/traces")
def list_traces(kind: str = "all", limit: int = 100, p: Principal = Depends(require(Perm.READ_ANALYTICS))):
    if kind not in TRACE_FILTERS:
        raise HTTPException(422, f"kind must be one of {TRACE_FILTERS}")
    sql = ("SELECT q.id, q.created_at, q.question, q.abstained, q.refusal_reason, q.groundedness, q.latency_ms, "
           "q.cost_usd, q.mode, q.flagged, q.trace, u.email FROM queries q LEFT JOIN users u ON u.id = q.user_id "
           "WHERE q.org_id = ? AND q.trace IS NOT NULL")
    sql += {"refused": " AND q.abstained = 1", "low_grounding": " AND q.groundedness < 0.7",
            "flagged": " AND q.flagged IS NOT NULL"}.get(kind, "")
    with db.connect() as conn:
        rows = conn.execute(sql + " ORDER BY q.id DESC LIMIT ?", (p.org_id, min(max(limit, 1), 500))).fetchall()
    out = []
    for r in rows:
        trace = json.loads(r["trace"])
        issues = _citation_issues(trace)
        off_target = bool((trace.get("relevance") or {}).get("off_target"))
        if (kind == "citation_issues" and not issues) or (kind == "off_target" and not off_target):
            continue
        out.append({"id": r["id"], "created_at": r["created_at"], "user": r["email"], "question": r["question"],
                    "abstained": bool(r["abstained"]), "refusal_reason": r["refusal_reason"],
                    "groundedness": r["groundedness"], "latency_ms": r["latency_ms"], "cost_usd": r["cost_usd"],
                    "mode": r["mode"], "flagged": bool(r["flagged"]), "citation_issues": issues,
                    "off_target": off_target,
                    "coverage": trace["gate"]["coverage"]})
    return out


@app.get("/api/traces/summary")
def traces_summary(days: int = 14, p: Principal = Depends(require(Perm.READ_ANALYTICS))):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, abstained, refusal_reason, latency_ms, cost_usd, input_tokens, output_tokens, trace "
            "FROM queries WHERE org_id = ? AND trace IS NOT NULL AND created_at >= ? ORDER BY id",
            (p.org_id, db.days_ago(max(1, min(days, 90))))).fetchall()
    traces = [json.loads(r["trace"]) for r in rows]

    def pct(values: list[float], q: float) -> float:
        values = sorted(values)
        return round(values[min(len(values) - 1, int(q * len(values)))], 2) if values else 0.0

    stage_ms: dict[str, list[float]] = {}
    for t in traces:
        for s in t["stages"]:
            if not s.get("skipped"):
                stage_ms.setdefault(s["name"], []).append(s["ms"])
    reasons: dict[str, int] = {}
    for r in rows:
        if r["refusal_reason"]:
            reasons[r["refusal_reason"]] = reasons.get(r["refusal_reason"], 0) + 1
    statuses: dict[str, int] = {}
    for t in traces:
        for s in t["sentences"]:
            statuses[s["status"]] = statuses.get(s["status"], 0) + 1
    # Documents that keep getting retrieved but never cited are ranking noise worth investigating.
    retrieved: dict[str, list[int]] = {}
    for t in traces:
        for h in t["hits"]:
            if h["classification"] in p.classifications:
                stat = retrieved.setdefault(h["title"], [0, 0])
                stat[0] += 1
                stat[1] += int(h["cited"])
    answered = [t["gate"]["coverage"] for t, r in zip(traces, rows) if not r["abstained"]]
    refused = [t["gate"]["coverage"] for t, r in zip(traces, rows) if r["abstained"]]
    return {
        "queries": len(rows),
        "refusal_reasons": reasons,
        "off_target_answers": sum(1 for t in traces if (t.get("relevance") or {}).get("off_target")),
        "stage_latency_ms": {name: {"p50": pct(v, 0.5), "p95": pct(v, 0.95)} for name, v in stage_ms.items()},
        "citation_sentences": statuses,
        "coverage": {"answered_avg": round(sum(answered) / len(answered), 3) if answered else None,
                     "refused_avg": round(sum(refused) / len(refused), 3) if refused else None},
        "retrieval_efficiency": sorted(
            ({"title": k, "retrieved": v[0], "cited": v[1], "cite_rate": round(v[1] / v[0], 3)}
             for k, v in retrieved.items()), key=lambda d: (d["cite_rate"], -d["retrieved"]))[:8],
        "recent": [{"id": r["id"], "latency_ms": r["latency_ms"], "cost_usd": r["cost_usd"],
                    "tokens": r["input_tokens"] + r["output_tokens"], "abstained": bool(r["abstained"])}
                   for r in rows[-60:]],
    }


@app.get("/api/traces/{query_id}")
def get_trace(query_id: int, p: Principal = Depends(require(Perm.READ_ANALYTICS))):
    with db.connect() as conn:
        r = conn.execute("SELECT q.*, u.email FROM queries q LEFT JOIN users u ON u.id = q.user_id "
                         "WHERE q.id = ? AND q.org_id = ?", (query_id, p.org_id)).fetchone()
    if r is None or r["trace"] is None:
        raise HTTPException(404, "Trace not found")
    trace, answer = _trim_trace(json.loads(r["trace"]), r["answer"], p)
    governance.audit(p.org_id, p.user_id, "trace.view", f"query:{query_id}",
                     {"redacted_for_viewer": trace["redacted_for_viewer"]})
    return {"id": r["id"], "created_at": r["created_at"], "user": r["email"], "question": r["question"],
            "answer": answer, "abstained": bool(r["abstained"]), "groundedness": r["groundedness"],
            "mode": r["mode"], "model": r["model"], "latency_ms": r["latency_ms"],
            "usage": {"input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                      "cost_usd": r["cost_usd"]},
            "flags": json.loads(r["flagged"]) if r["flagged"] else {}, **trace}


@app.get("/api/evals/latest")
def latest_eval(p: Principal = Depends(require(Perm.READ_ANALYTICS))):
    if not EVAL_REPORT.exists():
        raise HTTPException(404, "No eval report yet. Run: python -m evals.run_evals")
    return json.loads(EVAL_REPORT.read_text(encoding="utf-8"))


# --- ops ---------------------------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    with db.connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "llm": settings.llm_enabled, "embedder": indexes.embedder.name}


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics():
    return metrics.render()


@app.get("/api/meta")
def meta():
    return {"clearance_levels": CLEARANCE_LEVELS, "roles": ["viewer", "editor", "admin"],
            "llm_enabled": settings.llm_enabled, "demo": settings.seed_demo}


# --- frontend (single-page app) ------------------------------------------------------------------------

if settings.static_dir.exists():
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "metrics", "healthz")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        target = (settings.static_dir / path).resolve()
        if path and target.is_file() and settings.static_dir.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(settings.static_dir / "index.html")

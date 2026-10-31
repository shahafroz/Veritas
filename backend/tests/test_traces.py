import json

from app.generation import citation_report
from app.retrieval import Hit


def ask(client, headers, q):
    r = client.post("/api/query", json={"question": q}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_trace_records_stages_gate_and_ranking_explanations(client, auth):
    out = ask(client, auth("admin"), "What is the daily meal allowance when travelling?")
    t = client.get(f"/api/traces/{out['query_id']}", headers=auth("admin")).json()
    assert [s["name"] for s in t["stages"]] == ["retrieval", "gate", "generation", "verification"]
    assert t["gate"]["passed"] and t["gate"]["coverage"] >= t["gate"]["threshold"]
    top = t["hits"][0]
    assert top["title"] == "Expense and Travel Policy" and top["cited"]
    assert top["matched_terms"] and all(idf > 0 for _, idf in top["matched_terms"])
    assert abs(top["score"] - (top["rrf_bm25"] + top["rrf_dense"])) < 1e-9  # score decomposes into RRF parts
    assert t["sentences"] and t["sentences"][0]["status"] == "ok"


def test_relevance_flags_faithful_but_off_target_answers(client, auth):
    # Offline mode quotes the L3 band: every word is in the source (grounded), but "staff" is ignored.
    off = ask(client, auth("admin"), "What is the base salary band for a Staff Engineer?")
    t = client.get(f"/api/traces/{off['query_id']}", headers=auth("admin")).json()
    assert t["groundedness"] == 1.0
    assert t["relevance"]["off_target"] and t["relevance"]["ignored_terms"][0] == "staff"
    on = ask(client, auth("admin"), "What is the daily meal allowance when travelling?")
    assert not client.get(f"/api/traces/{on['query_id']}", headers=auth("admin")).json()["relevance"]["off_target"]
    listed = client.get("/api/traces?kind=off_target", headers=auth("admin")).json()
    assert off["query_id"] in [r["id"] for r in listed]


def test_refusal_reasons_are_classified(client, auth):
    low = ask(client, auth("admin"), "Which dental insurance provider does Helix use?")
    assert low["refusal_reason"] == "low_coverage"
    t = client.get(f"/api/traces/{low['query_id']}", headers=auth("admin")).json()
    assert not t["gate"]["passed"] and next(s for s in t["stages"] if s["name"] == "generation")["skipped"]


def test_traces_require_analytics_permission(client, auth):
    assert client.get("/api/traces", headers=auth("viewer")).status_code == 403
    assert client.get("/api/traces/summary", headers=auth("viewer")).status_code == 403


def test_trace_is_trimmed_to_the_viewers_clearance(client, auth):
    # Admin (restricted) asks about restricted content; the editor (confidential) inspects the trace.
    out = ask(client, auth("admin"), "What was Q3 2026 revenue?")
    assert "14.8 million" in out["answer"]
    as_admin = client.get(f"/api/traces/{out['query_id']}", headers=auth("admin")).json()
    as_editor = client.get(f"/api/traces/{out['query_id']}", headers=auth("editor")).json()
    assert not as_admin["redacted_for_viewer"]
    assert as_editor["redacted_for_viewer"]
    assert "14.8 million" not in json.dumps(as_editor) and "Board Memo" not in json.dumps(as_editor)
    assert any(h.get("hidden") for h in as_editor["hits"])
    assert as_editor["answer"].startswith("[withheld")
    actions = [e["action"] for e in client.get("/api/audit?action=trace", headers=auth("admin")).json()]
    assert actions.count("trace.view") >= 2


def test_trace_list_filters_and_summary(client, auth):
    h = auth("admin")
    refused = client.get("/api/traces?kind=refused", headers=h).json()
    assert refused and all(t["abstained"] for t in refused)
    assert client.get("/api/traces?kind=bogus", headers=h).status_code == 422
    s = client.get("/api/traces/summary", headers=h).json()
    assert s["queries"] > 0 and "low_coverage" in s["refusal_reasons"]
    assert {"retrieval", "verification"} <= set(s["stage_latency_ms"])
    assert s["recent"] and s["retrieval_efficiency"]


def test_citation_report_classifies_sentences():
    hits = [Hit(chunk_id=1, doc_id=1, title="T", heading="H", text="Hotels are capped at $250 per night.",
                classification="internal", score=1)]
    _, _, sents = citation_report(
        "Hotels are capped at $250 per night [S1]. Pets fly free on weekends [S1]. Lunch is free. "
        "Something else entirely [S9].", hits)
    assert [s["status"] for s in sents] == ["ok", "weak", "uncited", "invalid"]
    assert "pet" in sents[1]["unsupported_terms"]

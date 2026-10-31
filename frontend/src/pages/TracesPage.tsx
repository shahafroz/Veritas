import { useEffect, useState } from "react";
import { api, Clearance } from "../api";
import { ClassBadge, fmtTime, pct, Stat } from "../ui";

type Filter = "all" | "refused" | "low_grounding" | "off_target" | "flagged" | "citation_issues";

interface TraceRow {
  id: number;
  created_at: string;
  user: string;
  question: string;
  abstained: boolean;
  refusal_reason: string | null;
  groundedness: number | null;
  latency_ms: number;
  cost_usd: number;
  mode: string;
  flagged: boolean;
  citation_issues: number;
  off_target: boolean;
  coverage: number;
}

interface TraceHit {
  rank: number;
  title: string;
  heading?: string;
  text?: string;
  classification: Clearance;
  score: number;
  bm25_rank: number | null;
  dense_rank: number | null;
  bm25_score?: number;
  dense_sim?: number;
  rrf_bm25: number;
  rrf_dense: number;
  matched_terms?: [string, number][];
  cited: boolean;
  hidden?: boolean;
}

interface Sentence {
  text: string;
  refs: string[];
  invalid_refs: string[];
  support: number | null;
  unsupported_terms: string[];
  status: "ok" | "weak" | "uncited" | "invalid";
}

interface Trace {
  id: number;
  created_at: string;
  user: string;
  question: string;
  answer: string;
  abstained: boolean;
  groundedness: number | null;
  mode: string;
  model: string | null;
  latency_ms: number;
  usage: { input_tokens: number; output_tokens: number; cost_usd: number };
  flags: Record<string, unknown>;
  clearance: Clearance;
  refusal_reason: string | null;
  stages: { name: string; ms: number; ttft_ms?: number | null; skipped?: boolean; candidates?: number }[];
  gate: { coverage: number; threshold: number; passed: boolean; generator: string };
  hits: TraceHit[];
  sentences: Sentence[];
  relevance: { score: number; ignored_terms: string[]; off_target: boolean } | null;
  redacted_for_viewer: boolean;
}

interface Summary {
  queries: number;
  refusal_reasons: Record<string, number>;
  off_target_answers: number;
  stage_latency_ms: Record<string, { p50: number; p95: number }>;
  citation_sentences: Record<string, number>;
  coverage: { answered_avg: number | null; refused_avg: number | null };
  retrieval_efficiency: { title: string; retrieved: number; cited: number; cite_rate: number }[];
  recent: { id: number; latency_ms: number; cost_usd: number; tokens: number; abstained: boolean }[];
}

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "refused", label: "Refused" },
  { id: "low_grounding", label: "Low grounding" },
  { id: "off_target", label: "Off-target" },
  { id: "citation_issues", label: "Citation issues" },
  { id: "flagged", label: "Flagged" },
];

const REASONS: Record<string, string> = {
  low_coverage: "Not enough evidence (gate)",
  no_results: "Nothing retrieved",
  model_abstained: "Model chose to abstain",
  model_refusal: "Model safety refusal",
};

const STATUS: Record<Sentence["status"], { label: string; cls: string }> = {
  ok: { label: "supported", cls: "ok" },
  weak: { label: "weak support", cls: "warn" },
  uncited: { label: "no citation", cls: "bad" },
  invalid: { label: "invalid citation", cls: "bad" },
};

const STAGE_ORDER = ["retrieval", "gate", "generation", "verification"];

function Bar({ value, max, color = "var(--accent)" }: { value: number; max: number; color?: string }) {
  return (
    <div className="bar" style={{ flex: 1 }}>
      <span style={{ width: `${max ? Math.min(100, (value / max) * 100) : 0}%`, background: color }} />
    </div>
  );
}

function SummaryPanel({ s }: { s: Summary }) {
  const reasons = Object.entries(s.refusal_reasons).sort((a, b) => b[1] - a[1]);
  const maxReason = Math.max(1, ...reasons.map((r) => r[1]));
  const stages = STAGE_ORDER.filter((n) => s.stage_latency_ms[n]);
  const maxStage = Math.max(0.01, ...stages.map((n) => s.stage_latency_ms[n].p95));
  const sent = s.citation_sentences;
  const totalSent = Object.values(sent).reduce((a, b) => a + b, 0);
  const maxLat = Math.max(1, ...s.recent.map((r) => r.latency_ms));
  return (
    <>
      <div className="stats">
        <Stat label="Traced queries (14d)" value={s.queries} />
        <Stat label="Refused" value={Object.values(s.refusal_reasons).reduce((a, b) => a + b, 0)} />
        <Stat label="Possibly off-target" value={s.off_target_answers} sub="grounded, but skips the key term" />
        <Stat label="Sentences fully supported" value={totalSent ? pct((sent.ok ?? 0) / totalSent) : "—"} sub={`${totalSent} sentences checked`} />
        <Stat
          label="Evidence coverage"
          value={`${pct(s.coverage.answered_avg)} / ${pct(s.coverage.refused_avg)}`}
          sub="answered vs refused (avg)"
        />
      </div>
      <div className="grid-2">
        <div className="card">
          <div className="card-head"><h2>Why questions were refused</h2></div>
          <div className="card-pad" style={{ display: "grid", gap: 10 }}>
            {reasons.length === 0 && <span className="muted">No refusals yet.</span>}
            {reasons.map(([k, v]) => (
              <div key={k} style={{ display: "grid", gap: 4 }}>
                <div className="row small"><span>{REASONS[k] ?? k}</span><span className="spacer" /><span className="mono">{v}</span></div>
                <Bar value={v} max={maxReason} color="var(--warn)" />
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h2>Where the time goes</h2><span className="small muted">p50 · p95 per stage</span></div>
          <div className="card-pad" style={{ display: "grid", gap: 10 }}>
            {stages.map((n) => (
              <div key={n} className="row" style={{ flexWrap: "nowrap" }}>
                <span className="small" style={{ width: 92 }}>{n}</span>
                <Bar value={s.stage_latency_ms[n].p95} max={maxStage} />
                <span className="mono small" style={{ width: 120, textAlign: "right" }}>
                  {s.stage_latency_ms[n].p50} · {s.stage_latency_ms[n].p95} ms
                </span>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h2>Latency and cost per query</h2><span className="small muted">last {s.recent.length}</span></div>
          <div className="card-pad">
            <div className="spark">
              {s.recent.map((r) => (
                <a
                  key={r.id}
                  href={`#traces:${r.id}`}
                  title={`#${r.id} · ${r.latency_ms} ms · $${r.cost_usd.toFixed(4)} · ${r.tokens} tokens${r.abstained ? " · refused" : ""}`}
                  style={{ height: `${Math.max(4, (r.latency_ms / maxLat) * 100)}%`, background: r.abstained ? "var(--warn)" : "var(--accent)" }}
                />
              ))}
            </div>
            <div className="small muted" style={{ marginTop: 6 }}>
              Bar height = latency; amber = refused. Total spend ${s.recent.reduce((a, r) => a + r.cost_usd, 0).toFixed(4)} ·{" "}
              {s.recent.reduce((a, r) => a + r.tokens, 0).toLocaleString()} tokens.
            </div>
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h2>Retrieved but rarely cited</h2><span className="small muted">ranking noise</span></div>
          <div className="card-pad" style={{ display: "grid", gap: 8 }}>
            {s.retrieval_efficiency.map((d) => (
              <div key={d.title} className="row small" style={{ flexWrap: "nowrap" }}>
                <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.title}</span>
                <span className="mono muted">{d.cited}/{d.retrieved} cited</span>
                <span className={`badge ${d.cite_rate < 0.2 ? "warn" : "plain"}`}>{pct(d.cite_rate)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

function TraceDetail({ t }: { t: Trace }) {
  const stages = STAGE_ORDER.map((n) => t.stages.find((s) => s.name === n)).filter(Boolean) as Trace["stages"];
  const total = Math.max(0.01, stages.reduce((a, s) => a + s.ms, 0));
  const maxRrf = Math.max(1e-9, ...t.hits.map((h) => h.rrf_bm25 + h.rrf_dense));
  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="card card-pad" style={{ display: "grid", gap: 10 }}>
        <div className="row small muted">
          <span className="mono">trace #{t.id}</span>
          <span>· {t.user}</span>
          <ClassBadge level={t.clearance} />
          <span>· {fmtTime(t.created_at)}</span>
          <span className="spacer" />
          <span>{t.mode === "llm" ? t.model : t.mode}</span>
          <span>· {t.latency_ms} ms</span>
          <span>· ${t.usage.cost_usd.toFixed(4)}</span>
        </div>
        <h2 style={{ fontSize: 17 }}>{t.question}</h2>
        <div className={`answer ${t.abstained ? "abstained" : ""}`}>{t.answer}</div>
        <div className="row">
          {t.refusal_reason && <span className="badge warn">refused: {REASONS[t.refusal_reason] ?? t.refusal_reason}</span>}
          {t.groundedness != null && (
            <span className={`badge ${t.groundedness >= 0.7 ? "ok" : "warn"}`}>grounded {pct(t.groundedness)}</span>
          )}
          {"prompt_injection" in t.flags && <span className="badge bad">injection flagged</span>}
          {t.relevance?.off_target && <span className="badge bad">possibly off-target</span>}
          {t.redacted_for_viewer && <span className="badge bad">parts hidden: above your clearance</span>}
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>Pipeline timeline</h2><span className="small muted mono">{total.toFixed(1)} ms in pipeline</span></div>
        <div className="card-pad" style={{ display: "grid", gap: 8 }}>
          {(() => {
            let offset = 0;
            return stages.map((s) => {
              const left = (offset / total) * 100;
              offset += s.ms;
              return (
                <div key={s.name} className="row" style={{ flexWrap: "nowrap" }}>
                  <span className="small" style={{ width: 92 }}>{s.name}</span>
                  <div className="waterfall">
                    <span style={{ left: `${left}%`, width: `${Math.max(0.6, (s.ms / total) * 100)}%`, opacity: s.skipped ? 0.35 : 1 }} />
                  </div>
                  <span className="mono small" style={{ width: 150, textAlign: "right" }}>
                    {s.skipped ? "skipped" : `${s.ms} ms`}
                    {s.ttft_ms != null && ` · first token ${s.ttft_ms}`}
                  </span>
                </div>
              );
            });
          })()}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Confidence gate</h2>
          <span className={`badge ${t.gate.passed ? "ok" : "warn"}`}>{t.gate.passed ? "passed → generate" : "blocked → refuse"}</span>
        </div>
        <div className="card-pad" style={{ display: "grid", gap: 8 }}>
          <div className="gate">
            <span className="fill" style={{ width: `${t.gate.coverage * 100}%`, background: t.gate.passed ? "var(--ok)" : "var(--warn)" }} />
            <span className="mark" style={{ left: `${t.gate.threshold * 100}%` }} />
          </div>
          <div className="small muted">
            Evidence coverage {pct(t.gate.coverage, 1)} vs threshold {pct(t.gate.threshold)} ({t.gate.generator} generator): the
            IDF-weighted share of the question's terms found in the top 3 passages. Below the line, Veritas refuses without calling the model.
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Retrieval: why each passage ranked</h2>
          <span className="small muted">score = BM25 part + dense part (Reciprocal Rank Fusion)</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>#</th><th>Passage</th><th>BM25</th><th>Dense</th><th style={{ minWidth: 140 }}>Fused score</th><th>Matched terms (IDF)</th></tr>
            </thead>
            <tbody>
              {t.hits.map((h) => (
                <tr key={h.rank} style={{ opacity: h.hidden ? 0.6 : 1 }}>
                  <td className="mono">{h.rank}</td>
                  <td>
                    <div className="row" style={{ gap: 6 }}>
                      <strong>{h.title}</strong>
                      {h.cited && <span className="badge ok">cited</span>}
                    </div>
                    <div className="row small muted" style={{ gap: 6 }}>
                      <ClassBadge level={h.classification} />
                      {h.heading?.split(" > ").slice(1).join(" › ")}
                    </div>
                  </td>
                  <td className="mono small">
                    {h.bm25_rank != null ? `#${h.bm25_rank + 1}` : "—"}
                    {h.bm25_score != null && <div className="muted">{h.bm25_score.toFixed(2)}</div>}
                  </td>
                  <td className="mono small">
                    {h.dense_rank != null ? `#${h.dense_rank + 1}` : "—"}
                    {h.dense_sim != null && <div className="muted">cos {h.dense_sim.toFixed(2)}</div>}
                  </td>
                  <td>
                    <div className="stack" title={`BM25 ${h.rrf_bm25.toFixed(4)} + dense ${h.rrf_dense.toFixed(4)}`}>
                      <span style={{ width: `${(h.rrf_bm25 / maxRrf) * 100}%`, background: "var(--accent)" }} />
                      <span style={{ width: `${(h.rrf_dense / maxRrf) * 100}%`, background: "var(--c-internal)", opacity: 0.55 }} />
                    </div>
                    <div className="mono small muted">{h.score.toFixed(4)}</div>
                  </td>
                  <td>
                    <div className="chips">
                      {(h.matched_terms ?? []).map(([term, idf]) => (
                        <span key={term} className="chip" style={{ opacity: 0.45 + Math.min(0.55, idf / 4) }}>
                          {term} <span className="muted">{idf.toFixed(1)}</span>
                        </span>
                      ))}
                      {!h.hidden && !(h.matched_terms ?? []).length && <span className="small muted">semantic match only</span>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card-pad small muted" style={{ borderTop: "1px solid var(--border)" }}>
          <span className="legend" style={{ background: "var(--accent)" }} /> keyword (BM25) contribution{" "}
          <span className="legend" style={{ background: "var(--c-internal)", opacity: 0.55 }} /> semantic (dense) contribution.
          Only passages within the asker's clearance were searched.
        </div>
      </div>

      {t.relevance && (
        <div className="card">
          <div className="card-head">
            <h2>Answer relevance</h2>
            <span className={`badge ${t.relevance.off_target ? "bad" : "ok"}`}>
              {t.relevance.off_target ? "possibly off-target" : "on target"}
            </span>
          </div>
          <div className="card-pad small" style={{ display: "grid", gap: 6 }}>
            <div>
              The answer covers <strong>{pct(t.relevance.score)}</strong> of the question's key terms (IDF-weighted).
              {t.relevance.ignored_terms.length > 0 && (
                <>
                  {" "}It never uses{" "}
                  {t.relevance.ignored_terms.map((term) => <span key={term} className="chip" style={{ marginRight: 4 }}>{term}</span>)}
                  {" "}even though the retrieved passages contain {t.relevance.ignored_terms.length > 1 ? "them" : "it"}.
                </>
              )}
            </div>
            <div className="muted">
              Grounding checks that the answer is faithful to its sources. This checks that it actually answers the question: a
              quote can be perfectly cited and still miss the point.
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head"><h2>Citation check</h2><span className="small muted">each sentence vs the sources it cites</span></div>
        <div className="card-pad" style={{ display: "grid", gap: 10 }}>
          {t.sentences.length === 0 && <span className="muted">No answer sentences (the question was refused).</span>}
          {t.sentences.map((s, i) => (
            <div key={i} className="source">
              <div className="row small" style={{ gap: 6 }}>
                <span className={`badge ${STATUS[s.status].cls}`}>{STATUS[s.status].label}</span>
                {s.support != null && <span className="mono muted">support {pct(s.support)}</span>}
                {s.refs.map((r) => <span key={r} className="cite">{r}</span>)}
                {s.invalid_refs.map((r) => <span key={r} className="badge bad">{r} does not exist</span>)}
              </div>
              <div style={{ marginTop: 6 }}>{s.text}</div>
              {s.unsupported_terms.length > 0 && (
                <div className="small muted" style={{ marginTop: 4 }}>
                  Not found in cited source: {s.unsupported_terms.join(", ")}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function selectedFromHash(): number | null {
  const m = window.location.hash.match(/^#traces:(\d+)$/);
  return m ? Number(m[1]) : null;
}

export default function TracesPage() {
  const [filter, setFilter] = useState<Filter>("all");
  const [rows, setRows] = useState<TraceRow[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [selected, setSelected] = useState<number | null>(selectedFromHash);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.get<Summary>("/api/traces/summary").then(setSummary).catch((e) => setError(e.message));
    const onHash = () => setSelected(selectedFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    api.get<TraceRow[]>(`/api/traces?kind=${filter}`).then((r) => {
      setRows(r);
      setSelected((cur) => cur ?? r[0]?.id ?? null);
    });
  }, [filter]);

  useEffect(() => {
    if (selected == null) return setTrace(null);
    api.get<Trace>(`/api/traces/${selected}`).then(setTrace).catch((e) => setError(e.message));
  }, [selected]);

  function pick(id: number) {
    window.location.hash = `traces:${id}`;
    setSelected(id);
  }

  return (
    <div className="page wide">
      <div className="page-head">
        <div>
          <h1>Trace explorer</h1>
          <p>Every question, step by step: what was retrieved and why, why it was refused, which citations held up, and what it cost.</p>
        </div>
      </div>
      {error && <div className="error">{error}</div>}
      {summary && <SummaryPanel s={summary} />}

      <div className="trace-layout">
        <div className="card trace-list">
          <div className="card-pad pills">
            {FILTERS.map((f) => (
              <button key={f.id} className={filter === f.id ? "pill active" : "pill"} onClick={() => setFilter(f.id)}>
                {f.label}
              </button>
            ))}
          </div>
          {rows.length === 0 && <div className="card-pad muted small">No traces match.</div>}
          {rows.map((r) => (
            <button key={r.id} className={`trace-item ${selected === r.id ? "active" : ""}`} onClick={() => pick(r.id)}>
              <div className="q">{r.question}</div>
              <div className="row small muted" style={{ gap: 6 }}>
                <span className="mono">#{r.id}</span>
                <span>{r.user?.split("@")[0]}</span>
                <span>· {r.latency_ms} ms</span>
                <span className="spacer" />
                {r.abstained ? (
                  <span className="badge warn">refused</span>
                ) : (
                  <span className={`badge ${(r.groundedness ?? 0) >= 0.7 ? "ok" : "warn"}`}>{pct(r.groundedness)}</span>
                )}
                {r.off_target && <span className="badge bad">off-target</span>}
                {r.citation_issues > 0 && <span className="badge bad">{r.citation_issues} cite</span>}
                {r.flagged && <span className="badge bad">flag</span>}
              </div>
            </button>
          ))}
        </div>
        <div>{trace ? <TraceDetail t={trace} /> : <div className="card card-pad muted">Select a trace.</div>}</div>
      </div>
    </div>
  );
}

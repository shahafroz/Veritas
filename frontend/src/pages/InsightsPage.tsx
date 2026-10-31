import { useEffect, useState } from "react";
import { api } from "../api";
import { pct, Stat } from "../ui";

interface EvalCase {
  id: string;
  type: string;
  as: string;
  question: string;
  answer: string;
  abstained: boolean;
  correct?: boolean;
  leaked_chunks: number;
}
interface EvalReport {
  generated_at: string;
  git_sha: string | null;
  config: { generator: string; embedder: string; top_k: number };
  dataset: Record<string, number>;
  metrics: {
    retrieval: Record<string, Record<string, number>>;
    answer: Record<string, number>;
    safety: Record<string, number>;
    latency_ms: { p50: number; p95: number };
  };
  gate: { passed: boolean; failures: string[]; profile: string };
  cases: EvalCase[];
}
interface Analytics {
  queries: number;
  latency_ms: { p50: number; p95: number };
  abstention_rate: number;
  avg_groundedness: number | null;
  flagged_queries: number;
  cost_usd: number;
  tokens: { input: number; output: number };
  feedback: { up: number; down: number };
  per_day: { day: string; queries: number; abstained: number }[];
  top_cited_documents: { title: string; citations: number }[];
}

function caseFailed(c: EvalCase) {
  if (c.type === "answerable") return !c.correct || c.abstained;
  if (c.type === "unanswerable" || c.type === "forbidden") return !c.abstained || c.leaked_chunks > 0;
  return false;
}

function Evals() {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    api.get<EvalReport>("/api/evals/latest").then(setReport).catch(() => setMissing(true));
  }, []);
  if (missing)
    return <div className="card card-pad muted">No eval report yet. Run <code>python -m evals.run_evals</code> in the backend.</div>;
  if (!report) return null;
  const { retrieval, answer, safety } = report.metrics;
  const failures = report.cases.filter(caseFailed);

  return (
    <>
      <div className="card">
        <div className="card-head">
          <div>
            <h2>Offline evaluation</h2>
            <div className="small muted">
              {report.dataset.cases} golden cases · generator <span className="mono">{report.config.generator}</span> · embedder{" "}
              <span className="mono">{report.config.embedder}</span> · {new Date(report.generated_at).toLocaleString()}
              {report.git_sha && <> · <span className="mono">{report.git_sha}</span></>}
            </div>
          </div>
          <span className={`badge ${report.gate.passed ? "ok" : "bad"}`}>CI gate {report.gate.passed ? "passed" : "failed"}</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Retriever</th><th>Hit@1</th><th>Recall@3</th><th>Recall@5</th><th>MRR</th><th>nDCG@5</th></tr>
            </thead>
            <tbody>
              {Object.entries(retrieval).map(([mode, r]) => (
                <tr key={mode}>
                  <td><strong>{mode}</strong></td>
                  {["hit@1", "recall@3", "recall@5", "mrr", "ndcg@5"].map((k) => (
                    <td key={k} className="mono">{r[k].toFixed(3)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="stats">
        <Stat label="Fact accuracy" value={pct(answer.fact_accuracy)} sub={`${report.dataset.answerable} answerable`} />
        <Stat label="Citation precision" value={pct(answer.citation_precision)} />
        <Stat label="False abstention" value={pct(answer.false_abstention_rate, 1)} />
        <Stat label="Abstention recall" value={pct(safety.abstention_recall)} sub="unanswerable + forbidden" />
        <Stat label="ACL leakage" value={safety.acl_leakage_chunks} sub="chunks above clearance" />
        <Stat label="Injection flagged" value={pct(safety.injection_flag_recall)} />
      </div>
      {(failures.length > 0 || report.gate.failures.length > 0) && (
        <div className="card">
          <div className="card-head"><h2>Cases to look at ({failures.length})</h2></div>
          {report.gate.failures.length > 0 && (
            <div className="error" style={{ margin: "12px 20px 0" }}>{report.gate.failures.join(" · ")}</div>
          )}
          <div className="table-wrap">
            <table>
              <thead><tr><th>Case</th><th>As</th><th>Question</th><th>Answer</th></tr></thead>
              <tbody>
                {failures.map((c) => (
                  <tr key={c.id}>
                    <td className="mono small">{c.id}<div className="muted">{c.type}</div></td>
                    <td className="small">{c.as}</td>
                    <td>{c.question}</td>
                    <td className="small muted">{c.answer.slice(0, 180)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}

function Usage() {
  const [a, setA] = useState<Analytics | null>(null);
  useEffect(() => void api.get<Analytics>("/api/analytics?days=14").then(setA), []);
  if (!a) return null;
  const max = Math.max(1, ...a.per_day.map((d) => d.queries));
  const maxCite = Math.max(1, ...a.top_cited_documents.map((d) => d.citations));
  return (
    <>
      <div className="stats">
        <Stat label="Queries (14d)" value={a.queries} />
        <Stat label="Latency p50 / p95" value={`${a.latency_ms.p50} / ${a.latency_ms.p95}`} sub="milliseconds" />
        <Stat label="Abstention rate" value={pct(a.abstention_rate)} />
        <Stat label="Avg groundedness" value={pct(a.avg_groundedness)} />
        <Stat label="LLM spend" value={`$${a.cost_usd.toFixed(2)}`} sub={`${(a.tokens.input + a.tokens.output).toLocaleString()} tokens`} />
        <Stat label="Feedback" value={`👍 ${a.feedback.up}  👎 ${a.feedback.down}`} sub={`${a.flagged_queries} flagged queries`} />
      </div>
      <div className="grid-2">
        <div className="card">
          <div className="card-head"><h2>Queries per day</h2></div>
          <div className="card-pad" style={{ display: "grid", gap: 8 }}>
            {a.per_day.length === 0 && <span className="muted">No queries yet.</span>}
            {a.per_day.map((d) => (
              <div key={d.day} className="row" style={{ flexWrap: "nowrap" }}>
                <span className="mono small muted" style={{ width: 84 }}>{d.day.slice(5)}</span>
                <div className="bar" style={{ flex: 1 }}><span style={{ width: `${(d.queries / max) * 100}%` }} /></div>
                <span className="mono small" style={{ width: 70, textAlign: "right" }}>{d.queries} ({d.abstained} abst.)</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h2>Most-cited documents</h2></div>
          <div className="card-pad" style={{ display: "grid", gap: 8 }}>
            {a.top_cited_documents.length === 0 && <span className="muted">No citations yet.</span>}
            {a.top_cited_documents.map((d) => (
              <div key={d.title} style={{ display: "grid", gap: 4 }}>
                <div className="row small"><span>{d.title}</span><span className="spacer" /><span className="mono">{d.citations}</span></div>
                <div className="bar"><span style={{ width: `${(d.citations / maxCite) * 100}%` }} /></div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

export default function InsightsPage() {
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Quality & usage</h1>
          <p>Offline eval results that gate every deploy, plus live production signals.</p>
        </div>
        <a className="small" href="/metrics" target="_blank" rel="noreferrer">Prometheus metrics ↗</a>
      </div>
      <Evals />
      <h2 style={{ marginTop: 8 }}>Production (last 14 days)</h2>
      <Usage />
    </div>
  );
}

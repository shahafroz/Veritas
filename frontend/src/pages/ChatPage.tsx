import { FormEvent, Fragment, useEffect, useRef, useState } from "react";
import { api, FinalAnswer, Me, Source, streamQuery } from "../api";
import { ClassBadge, pct } from "../ui";

interface Turn {
  question: string;
  sources: Source[];
  text: string;
  final?: FinalAnswer;
  error?: string;
  rating?: 1 | -1;
}

const SUGGESTIONS = [
  "How many days of paid time off do employees get?",
  "What is the hotel cap per night in New York?",
  "Within how many hours must regulators be notified of a data breach?",
  "What is the codename for the Lumen Analytics acquisition?",
  "Which dental insurance provider does Helix use?",
];

/** Render answer text with [S1] markers as clickable chips. */
function AnswerText({ text, onCite }: { text: string; onCite: (ref: string) => void }) {
  const parts = text.split(/(\[S\d+\])/g);
  return (
    <>
      {parts.map((p, i) =>
        /^\[S\d+\]$/.test(p) ? (
          <button key={i} className="cite" onClick={() => onCite(p.slice(1, -1))}>
            {p.slice(1, -1)}
          </button>
        ) : (
          <Fragment key={i}>{p}</Fragment>
        ),
      )}
    </>
  );
}

export default function ChatPage({ me }: { me: Me }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [highlight, setHighlight] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const update = (fn: (t: Turn) => Turn) => setTurns((ts) => [...ts.slice(0, -1), fn(ts[ts.length - 1])]);

  async function ask(question: string, e?: FormEvent) {
    e?.preventDefault();
    if (!question.trim() || busy) return;
    setInput("");
    setBusy(true);
    setTurns((ts) => [...ts, { question, sources: [], text: "" }]);
    try {
      await streamQuery(question, (ev) => {
        if (ev.type === "sources") update((t) => ({ ...t, sources: ev.sources }));
        else if (ev.type === "delta") update((t) => ({ ...t, text: t.text + ev.text }));
        else if (ev.type === "final") update((t) => ({ ...t, text: ev.answer, final: ev }));
        else if (ev.type === "error") update((t) => ({ ...t, error: ev.message }));
      });
    } catch (err) {
      update((t) => ({ ...t, error: (err as Error).message }));
    } finally {
      setBusy(false);
    }
  }

  async function rate(index: number, rating: 1 | -1) {
    const t = turns[index];
    if (!t.final?.query_id) return;
    await api.send("POST", `/api/queries/${t.final.query_id}/feedback`, { rating });
    setTurns((ts) => ts.map((x, i) => (i === index ? { ...x, rating } : x)));
  }

  return (
    <div className="chat">
      <div className="chat-scroll">
        <div className="chat-inner">
          {turns.length === 0 && (
            <div className="card card-pad" style={{ display: "grid", gap: 12 }}>
              <h1>Ask the knowledge base</h1>
              <p className="muted" style={{ margin: 0 }}>
                Answers come only from documents your clearance allows (<ClassBadge level={me.clearance} /> and below),
                with every claim cited. If the documents don't support an answer, Veritas says so instead of guessing.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} onClick={() => ask(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {turns.map((t, i) => {
            const cited = new Set(t.final?.citations.map((c) => c.ref));
            const live = busy && i === turns.length - 1 && !t.final;
            return (
              <Fragment key={i}>
                <div className="msg-q">{t.question}</div>
                <div className="msg-a">
                  {t.error ? (
                    <div className="error">{t.error}</div>
                  ) : (
                    <div className={`answer ${t.final?.abstained ? "abstained" : ""} ${live ? "cursor" : ""}`}>
                      <AnswerText text={t.text} onCite={(ref) => setHighlight(`${i}:${ref}`)} />
                    </div>
                  )}
                  {t.final && (
                    <div className="meta-row">
                      {t.final.abstained ? (
                        <span className="badge warn">abstained</span>
                      ) : (
                        <span className={`badge ${(t.final.groundedness ?? 0) >= 0.7 ? "ok" : "warn"}`}>
                          grounded {pct(t.final.groundedness)}
                        </span>
                      )}
                      {"prompt_injection" in t.final.flags && <span className="badge bad">injection flagged</span>}
                      <span>{t.final.mode === "llm" ? t.final.model : t.final.mode}</span>
                      <span>· {t.final.latency_ms} ms</span>
                      {t.final.usage.cost_usd > 0 && <span>· ${t.final.usage.cost_usd.toFixed(4)}</span>}
                      {t.final.query_id && me.permissions.includes("analytics:read") && (
                        <a className="trace-link" href={`#traces:${t.final.query_id}`}>· trace ↗</a>
                      )}
                      <span className="spacer" />
                      <button className={`ghost ${t.rating === 1 ? "active" : ""}`} title="Helpful" onClick={() => rate(i, 1)} disabled={!!t.rating}>
                        👍
                      </button>
                      <button className="ghost" title="Not helpful" onClick={() => rate(i, -1)} disabled={!!t.rating}>
                        👎
                      </button>
                    </div>
                  )}
                  {t.sources.length > 0 && (
                    <details open={!t.final?.abstained}>
                      <summary className="small muted" style={{ cursor: "pointer" }}>
                        {t.sources.length} sources retrieved (security-trimmed to your clearance)
                      </summary>
                      <div className="sources" style={{ marginTop: 8 }}>
                        {t.sources.map((s) => (
                          <div key={s.ref} className={`source ${highlight === `${i}:${s.ref}` ? "highlight" : ""}`} style={{ opacity: t.final && !cited.has(s.ref) ? 0.6 : 1 }}>
                            <div className="row" style={{ gap: 8 }}>
                              <span className="cite">{s.ref}</span>
                              <strong>{s.title}</strong>
                              <span className="muted small">{s.heading.split(" > ").slice(1).join(" › ")}</span>
                              <span className="spacer" />
                              <ClassBadge level={s.classification} />
                            </div>
                            <div className="snippet">{s.snippet}</div>
                          </div>
                        ))}
                      </div>
                    </details>
                  )}
                </div>
              </Fragment>
            );
          })}
          <div ref={bottom} />
        </div>
      </div>
      <div className="composer">
        <form onSubmit={(e) => ask(input, e)}>
          <input placeholder="Ask about policies, runbooks, contracts…" value={input} onChange={(e) => setInput(e.target.value)} maxLength={2000} />
          <button className="primary" disabled={busy || !input.trim()}>
            {busy ? "Thinking…" : "Ask"}
          </button>
        </form>
      </div>
    </div>
  );
}

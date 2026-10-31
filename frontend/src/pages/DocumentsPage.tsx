import { useCallback, useEffect, useState } from "react";
import { api, Clearance, Me } from "../api";
import { ClassBadge, fmtTime } from "../ui";

interface Doc {
  id: number;
  title: string;
  filename: string;
  classification: Clearance;
  n_chunks: number;
  pii_redactions: number;
  quarantined_chunks: number;
  created_at: string;
  uploaded_by: string | null;
}

interface Chunk {
  ord: number;
  heading: string;
  text: string;
  quarantined: number;
}

const LEVELS: Clearance[] = ["public", "internal", "confidential", "restricted"];

export default function DocumentsPage({ me }: { me: Me }) {
  const [docs, setDocs] = useState<Doc[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [classification, setClassification] = useState<Clearance>("internal");
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<{ doc: Doc; chunks: Chunk[] } | null>(null);
  const canWrite = me.permissions.includes("docs:write");
  const allowedLevels = LEVELS.slice(0, LEVELS.indexOf(me.clearance) + 1);

  const load = useCallback(() => api.get<Doc[]>("/api/documents").then(setDocs).catch((e) => setError(e.message)), []);
  useEffect(() => void load(), [load]);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    for (const file of Array.from(files)) {
      const form = new FormData();
      form.append("file", file);
      form.append("classification", classification);
      try {
        const r = await api.upload<{ title: string; n_chunks: number; pii_redactions: Record<string, number>; quarantined_chunks: number }>(
          "/api/documents",
          form,
        );
        const pii = Object.values(r.pii_redactions).reduce((a, b) => a + b, 0);
        setNotice(
          `Indexed “${r.title}”: ${r.n_chunks} chunks` +
            (pii ? `, ${pii} PII items redacted` : "") +
            (r.quarantined_chunks ? `, ${r.quarantined_chunks} chunk(s) quarantined for prompt injection` : ""),
        );
      } catch (e) {
        setError(`${file.name}: ${(e as Error).message}`);
      }
    }
    setBusy(false);
    load();
  }

  async function remove(doc: Doc) {
    if (!confirm(`Delete “${doc.title}” from the knowledge base?`)) return;
    try {
      await api.send("DELETE", `/api/documents/${doc.id}`);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function view(doc: Doc) {
    const r = await api.get<{ chunks: Chunk[] }>(`/api/documents/${doc.id}/chunks`);
    setOpen({ doc, chunks: r.chunks });
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Knowledge base</h1>
          <p>
            You can see {docs.length} documents at <ClassBadge level={me.clearance} /> clearance or below. Higher-classified
            documents are invisible to you, including in search scoring.
          </p>
        </div>
      </div>

      {canWrite && (
        <div
          className={`card card-pad drop ${over ? "over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}
        >
          <div className="row" style={{ justifyContent: "center" }}>
            <span>Drop PDF, Markdown, HTML or text files, or</span>
            <label>
              <input type="file" multiple accept=".pdf,.md,.markdown,.txt,.html,.htm" style={{ display: "none" }} onChange={(e) => upload(e.target.files)} />
              <span className="badge plain" style={{ cursor: "pointer", padding: "6px 12px" }}>browse</span>
            </label>
            <span>· classify as</span>
            <select value={classification} onChange={(e) => setClassification(e.target.value as Clearance)}>
              {allowedLevels.map((l) => (
                <option key={l}>{l}</option>
              ))}
            </select>
            {busy && <span className="muted">Indexing…</span>}
          </div>
          <p className="small" style={{ margin: "8px 0 0" }}>
            On upload: PII is redacted, chunks are screened for prompt injection, and the event is written to the audit log.
          </p>
        </div>
      )}
      {notice && <div className="notice">{notice}</div>}
      {error && <div className="error">{error}</div>}

      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>Document</th>
              <th>Classification</th>
              <th>Chunks</th>
              <th>Governance</th>
              <th>Added</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.id}>
                <td>
                  <a href="#" onClick={(e) => { e.preventDefault(); view(d); }}>
                    <strong>{d.title}</strong>
                  </a>
                  <div className="small muted mono">{d.filename}</div>
                </td>
                <td><ClassBadge level={d.classification} /></td>
                <td>{d.n_chunks}</td>
                <td>
                  <div className="row" style={{ gap: 6 }}>
                    {d.pii_redactions > 0 && <span className="badge warn">{d.pii_redactions} PII redacted</span>}
                    {d.quarantined_chunks > 0 && <span className="badge bad">{d.quarantined_chunks} quarantined</span>}
                    {!d.pii_redactions && !d.quarantined_chunks && <span className="badge ok">clean</span>}
                  </div>
                </td>
                <td className="small muted">
                  {fmtTime(d.created_at)}
                  <div>{d.uploaded_by}</div>
                </td>
                <td>
                  {canWrite && (me.role === "admin" || d.uploaded_by === me.name) && (
                    <button className="ghost danger" onClick={() => remove(d)}>Delete</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {open && (
        <div className="card">
          <div className="card-head">
            <h2>{open.doc.title} · chunks as indexed</h2>
            <button className="ghost" onClick={() => setOpen(null)}>Close</button>
          </div>
          <div className="card-pad" style={{ display: "grid", gap: 10 }}>
            {open.chunks.map((c) => (
              <div key={c.ord} className="source" style={{ opacity: c.quarantined ? 0.7 : 1 }}>
                <div className="row small">
                  <span className="mono muted">#{c.ord}</span>
                  <span className="muted">{c.heading}</span>
                  <span className="spacer" />
                  {c.quarantined ? <span className="badge bad">quarantined: excluded from retrieval</span> : null}
                </div>
                <div style={{ marginTop: 4 }}>{c.text}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

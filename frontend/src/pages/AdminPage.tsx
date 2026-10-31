import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Clearance, Me, Role } from "../api";
import { ClassBadge, fmtTime } from "../ui";

interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  clearance: Clearance;
  active: number;
}
interface AuditEntry {
  id: number;
  ts: string;
  action: string;
  resource: string;
  detail: Record<string, unknown>;
  hash: string;
  email: string | null;
}
interface Org {
  name: string;
  pii_redaction: number;
  monthly_budget_usd: number;
  month_to_date_cost_usd: number;
  llm_mode: string;
  embedder: string;
}

const ROLES: Role[] = ["viewer", "editor", "admin"];
const LEVELS: Clearance[] = ["public", "internal", "confidential", "restricted"];
const PERMS: Record<Role, string> = {
  viewer: "Ask questions, read documents",
  editor: "+ upload/delete own documents, view quality & usage",
  admin: "+ manage users & clearances, audit log, org policy",
};

function Users({ me }: { me: Me }) {
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", email: "", password: "", role: "viewer" as Role, clearance: "internal" as Clearance });
  const load = useCallback(() => api.get<User[]>("/api/users").then(setUsers), []);
  useEffect(() => void load(), [load]);

  async function patch(u: User, change: { role?: Role; clearance?: Clearance; active?: boolean }) {
    setError(null);
    try {
      await api.send("PATCH", `/api/users/${u.id}`, change);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.send("POST", "/api/users", form);
      setForm({ ...form, name: "", email: "", password: "" });
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2>Users & access</h2>
        <span className="small muted">Role = what you can do (RBAC). Clearance = what data you can see (ABAC).</span>
      </div>
      {error && <div className="error" style={{ margin: "12px 20px 0" }}>{error}</div>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>User</th><th>Role</th><th>Clearance</th><th>Status</th></tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} style={{ opacity: u.active ? 1 : 0.55 }}>
                <td><strong>{u.name}</strong><div className="small muted mono">{u.email}</div></td>
                <td>
                  <select value={u.role} disabled={u.id === me.id} onChange={(e) => patch(u, { role: e.target.value as Role })}>
                    {ROLES.map((r) => <option key={r}>{r}</option>)}
                  </select>
                  <div className="small muted" style={{ marginTop: 4 }}>{PERMS[u.role]}</div>
                </td>
                <td>
                  <select value={u.clearance} onChange={(e) => patch(u, { clearance: e.target.value as Clearance })}>
                    {LEVELS.map((l) => <option key={l}>{l}</option>)}
                  </select>
                </td>
                <td>
                  <button className="ghost" disabled={u.id === me.id} onClick={() => patch(u, { active: !u.active })}>
                    {u.active ? "Deactivate" : "Reactivate"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <form className="card-pad row" style={{ borderTop: "1px solid var(--border)" }} onSubmit={create}>
        <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <input type="email" placeholder="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
        <input type="password" placeholder="Temp password (8+)" minLength={8} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
        <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
          {ROLES.map((r) => <option key={r}>{r}</option>)}
        </select>
        <select value={form.clearance} onChange={(e) => setForm({ ...form, clearance: e.target.value as Clearance })}>
          {LEVELS.map((l) => <option key={l}>{l}</option>)}
        </select>
        <button className="primary">Add user</button>
      </form>
    </div>
  );
}

function Audit() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [filter, setFilter] = useState("");
  const [chain, setChain] = useState<{ valid: boolean; entries: number; broken_at_id?: number } | null>(null);
  useEffect(() => {
    api.get<AuditEntry[]>(`/api/audit?limit=150${filter ? `&action=${encodeURIComponent(filter)}` : ""}`).then(setEntries);
  }, [filter]);

  return (
    <div className="card">
      <div className="card-head">
        <h2>Audit log</h2>
        <div className="row">
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">All events</option>
            <option value="query">Queries</option>
            <option value="document">Documents</option>
            <option value="user">User changes</option>
            <option value="auth">Sign-ins</option>
          </select>
          <button onClick={() => api.get<typeof chain>("/api/audit/verify").then(setChain)}>Verify integrity</button>
          <button onClick={() => api.download("/api/audit/export.csv", "veritas-audit.csv")}>Export CSV</button>
        </div>
      </div>
      {chain && (
        <div className={chain.valid ? "notice" : "error"} style={{ margin: "12px 20px 0" }}>
          {chain.valid
            ? `Hash chain intact across ${chain.entries} entries: no record has been altered or removed.`
            : `Tampering detected at entry #${chain.broken_at_id}.`}
        </div>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th><th>Hash</th></tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td className="small muted" style={{ whiteSpace: "nowrap" }}>{fmtTime(e.ts)}</td>
                <td className="small mono">{e.email ?? "system"}</td>
                <td>
                  <span className={`badge plain ${e.action.includes("failed") ? "bad" : ""}`}>{e.action}</span>
                  <div className="small muted mono">{e.resource}</div>
                </td>
                <td><pre className="detail mono">{JSON.stringify(e.detail, null, 1).slice(0, 400)}</pre></td>
                <td className="mono small muted">{e.hash.slice(0, 10)}…</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function OrgPolicy() {
  const [org, setOrg] = useState<Org | null>(null);
  const [budget, setBudget] = useState("");
  useEffect(() => {
    api.get<Org>("/api/org").then((o) => { setOrg(o); setBudget(String(o.monthly_budget_usd)); });
  }, []);
  if (!org) return null;
  const save = (change: Partial<{ pii_redaction: boolean; monthly_budget_usd: number }>) =>
    api.send<Org>("PATCH", "/api/org", change).then(setOrg);
  const used = org.monthly_budget_usd ? org.month_to_date_cost_usd / org.monthly_budget_usd : 0;

  return (
    <div className="card">
      <div className="card-head"><h2>Organization policy</h2></div>
      <div className="card-pad grid-2">
        <div style={{ display: "grid", gap: 10 }}>
          <label className="row">
            <input type="checkbox" checked={!!org.pii_redaction} onChange={(e) => save({ pii_redaction: e.target.checked })} />
            Redact PII (emails, phones, SSNs, card numbers) at ingest
          </label>
          <div className="small muted">Generator: <span className="mono">{org.llm_mode}</span></div>
          <div className="small muted">Embeddings: <span className="mono">{org.embedder}</span></div>
        </div>
        <div style={{ display: "grid", gap: 8 }}>
          <div className="row">
            <span>Monthly LLM budget $</span>
            <input style={{ width: 100 }} value={budget} onChange={(e) => setBudget(e.target.value)} />
            <button onClick={() => save({ monthly_budget_usd: Number(budget) })}>Save</button>
          </div>
          <div className="bar"><span style={{ width: `${Math.min(100, used * 100)}%` }} /></div>
          <div className="small muted">
            ${org.month_to_date_cost_usd.toFixed(4)} spent this month. Queries return 402 once the budget is reached.
          </div>
        </div>
      </div>
    </div>
  );
}

export default function AdminPage({ me }: { me: Me }) {
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Governance</h1>
          <p>Access control, organization policy, and a tamper-evident record of every action.</p>
        </div>
        <span className="row small muted">
          Levels: {LEVELS.map((l) => <ClassBadge key={l} level={l} />)}
        </span>
      </div>
      <Users me={me} />
      <OrgPolicy />
      <Audit />
    </div>
  );
}

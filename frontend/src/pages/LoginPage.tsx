import { FormEvent, useEffect, useState } from "react";
import { api, setToken } from "../api";
import { ClassBadge } from "../ui";

const DEMO = [
  { email: "admin@helix.demo", role: "admin", clearance: "restricted" },
  { email: "editor@helix.demo", role: "editor", clearance: "confidential" },
  { email: "analyst@helix.demo", role: "viewer", clearance: "confidential" },
  { email: "viewer@helix.demo", role: "viewer", clearance: "internal" },
];
const DEMO_PASSWORD = "veritas-demo";

export default function LoginPage({ onLogin }: { onLogin: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState(false);

  useEffect(() => {
    api.get<{ demo: boolean }>("/api/meta").then((m) => setDemo(m.demo)).catch(() => {});
  }, []);

  async function submit(e: FormEvent, creds?: { email: string; password: string }) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res =
        mode === "register" && !creds
          ? await api.send<{ token: string }>("POST", "/api/auth/register", { org_name: orgName, name, email, password })
          : await api.send<{ token: string }>("POST", "/api/auth/login", creds ?? { email, password });
      setToken(res.token);
      onLogin();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="login">
        <div className="brand" style={{ padding: 0 }}>
          <span className="brand-mark">V</span>
          <span>Veritas</span>
        </div>
        <div>
          <h1>{mode === "login" ? "Sign in" : "Create your workspace"}</h1>
          <p className="muted" style={{ margin: "4px 0 0" }}>
            Governed answers from your company's documents — cited, access-controlled, audited.
          </p>
        </div>
        <div className="card card-pad">
          <form onSubmit={submit}>
            {mode === "register" && (
              <>
                <input placeholder="Organization name" value={orgName} onChange={(e) => setOrgName(e.target.value)} required />
                <input placeholder="Your name" value={name} onChange={(e) => setName(e.target.value)} required />
              </>
            )}
            <input type="email" placeholder="Email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
            <input type="password" placeholder="Password" autoComplete="current-password" minLength={mode === "register" ? 8 : undefined} value={password} onChange={(e) => setPassword(e.target.value)} required />
            {error && <div className="error">{error}</div>}
            <button className="primary" disabled={busy}>
              {mode === "login" ? "Sign in" : "Create workspace"}
            </button>
          </form>
          <p className="small muted" style={{ marginBottom: 0 }}>
            {mode === "login" ? "New here? " : "Have an account? "}
            <a href="#" onClick={(e) => { e.preventDefault(); setMode(mode === "login" ? "register" : "login"); setError(null); }}>
              {mode === "login" ? "Create a workspace" : "Sign in"}
            </a>
          </p>
        </div>
        {demo && mode === "login" && (
          <div className="card card-pad demo-users">
            <h3>Demo tenant · Helix Systems</h3>
            <p className="small muted" style={{ margin: "0 0 6px" }}>
              Sign in as different roles to see access control change what gets retrieved.
            </p>
            {DEMO.map((u) => (
              <button key={u.email} disabled={busy} onClick={(e) => submit(e, { email: u.email, password: DEMO_PASSWORD })}>
                <span className="mono">{u.email}</span>
                <span className="row" style={{ gap: 6 }}>
                  <span className="badge plain">{u.role}</span>
                  <ClassBadge level={u.clearance} />
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

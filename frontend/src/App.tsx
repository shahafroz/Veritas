import { useCallback, useEffect, useState } from "react";
import { api, getToken, Me, setToken } from "./api";
import { ClassBadge } from "./ui";
import LoginPage from "./pages/LoginPage";
import ChatPage from "./pages/ChatPage";
import DocumentsPage from "./pages/DocumentsPage";
import AdminPage from "./pages/AdminPage";
import InsightsPage from "./pages/InsightsPage";
import TracesPage from "./pages/TracesPage";

type Tab = "chat" | "documents" | "traces" | "insights" | "admin";

const TABS: { id: Tab; label: string; icon: string; perm?: string }[] = [
  { id: "chat", label: "Ask", icon: "💬" },
  { id: "documents", label: "Knowledge base", icon: "📚" },
  { id: "traces", label: "Trace explorer", icon: "🔎", perm: "analytics:read" },
  { id: "insights", label: "Quality & usage", icon: "📈", perm: "analytics:read" },
  { id: "admin", label: "Governance", icon: "🛡️", perm: "users:manage" },
];

/** Hash routes are `#tab` or `#tab:arg` (e.g. `#traces:42` opens one trace). */
function tabFromHash(): Tab {
  const h = window.location.hash.slice(1).split(":")[0] as Tab;
  return TABS.some((t) => t.id === h) ? h : "chat";
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(Boolean(getToken()));
  const [tab, setTab] = useState<Tab>(tabFromHash);

  const loadMe = useCallback(() => {
    if (!getToken()) return setLoading(false);
    api
      .get<Me>("/api/me")
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadMe();
    const onLogout = () => setMe(null);
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("veritas:logout", onLogout);
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener("veritas:logout", onLogout);
      window.removeEventListener("hashchange", onHash);
    };
  }, [loadMe]);

  useEffect(() => {
    if (tabFromHash() !== tab) window.location.hash = tab;
  }, [tab]);

  if (loading) return null;
  if (!me) return <LoginPage onLogin={loadMe} />;

  const visible = TABS.filter((t) => !t.perm || me.permissions.includes(t.perm));
  const active = visible.some((t) => t.id === tab) ? tab : "chat";

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">V</span>
          <span>Veritas</span>
        </div>
        {visible.map((t) => (
          <button key={t.id} className={`nav-item ${active === t.id ? "active" : ""}`} onClick={() => setTab(t.id)}>
            <span aria-hidden>{t.icon}</span>
            {t.label}
          </button>
        ))}
        <div className="sidebar-foot">
          <div className="user-chip">
            <div className="name">{me.name}</div>
            <div className="meta">{me.org}</div>
            <div className="row" style={{ marginTop: 6, gap: 6 }}>
              <span className="badge plain">{me.role}</span>
              <ClassBadge level={me.clearance} />
            </div>
          </div>
          <button
            className="ghost"
            onClick={() => {
              setToken(null);
              setMe(null);
            }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <main className="main">
        {active === "chat" && <ChatPage me={me} />}
        {active === "documents" && <DocumentsPage me={me} />}
        {active === "traces" && <TracesPage />}
        {active === "insights" && <InsightsPage />}
        {active === "admin" && <AdminPage me={me} />}
      </main>
    </div>
  );
}

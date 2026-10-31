export type Role = "viewer" | "editor" | "admin";
export type Clearance = "public" | "internal" | "confidential" | "restricted";

export interface Me {
  id: number;
  email: string;
  name: string;
  role: Role;
  clearance: Clearance;
  org: string;
  permissions: string[];
}

export interface Source {
  ref: string;
  chunk_id: number;
  doc_id: number;
  title: string;
  heading: string;
  classification: Clearance;
  score?: number;
  snippet: string;
}

export interface FinalAnswer {
  query_id: number | null;
  answer: string;
  citations: Source[];
  groundedness: number | null;
  abstained: boolean;
  coverage: number;
  mode: string;
  model: string | null;
  latency_ms: number;
  usage: { input_tokens: number; output_tokens: number; cost_usd: number };
  flags: Record<string, unknown>;
}

export type StreamEvent =
  | { type: "sources"; sources: Source[] }
  | { type: "delta"; text: string }
  | ({ type: "final" } & FinalAnswer)
  | { type: "error"; status: number; message: string };

const TOKEN_KEY = "veritas.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: session-only login */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 401) {
    setToken(null);
    window.dispatchEvent(new Event("veritas:logout"));
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, msg);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export const api = {
  get: <T,>(path: string) => fetch(path, { headers: headers() }).then((r) => handle<T>(r)),
  send: <T,>(method: string, path: string, body?: unknown) =>
    fetch(path, {
      method,
      headers: headers({ "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => handle<T>(r)),
  upload: <T,>(path: string, form: FormData) =>
    fetch(path, { method: "POST", headers: headers(), body: form }).then((r) => handle<T>(r)),
  download: async (path: string, filename: string) => {
    const res = await fetch(path, { headers: headers() });
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: filename });
    a.click();
    URL.revokeObjectURL(url);
  },
};

/** POST a question and consume the server-sent event stream. */
export async function streamQuery(question: string, onEvent: (e: StreamEvent) => void): Promise<void> {
  const res = await fetch("/api/query/stream", {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ question }),
  });
  if (!res.ok || !res.body) return void (await handle(res));
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (frame.startsWith("data: ")) onEvent(JSON.parse(frame.slice(6)));
    }
  }
}

// Tiny typed client for the backend API.
const BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export type Phase = {
  key: string;
  label: string;
  order: number;
};

export type PhaseResult = {
  id: string;
  phase: string;
  agent: string;
  status: string;
  output: Record<string, unknown>;
  content_md: string;
  model_used: string | null;
  provider_used: string | null;
  feedback: string | null;
  created_at: string;
};

export type Project = {
  id: string;
  idea: string;
  name: string | null;
  status: string;
  current_phase: string | null;
  routing_mode: string;
  preferred_model: string | null;
  require_approval: boolean;
  created_at: string;
  updated_at: string;
  phases: PhaseResult[];
};

// Fast CRUD calls should fail fast so a hung/restarting backend surfaces an error
// instead of an infinite "Loading…". LLM-driven endpoints (run / generate / edit a
// preview) drive a local Ollama model that can take far longer than 15s, so they
// pass the longer cap below.
const DEFAULT_TIMEOUT_MS = 15000;
const LLM_TIMEOUT_MS = 300000; // 5 min — local generation on CPU is slow

// FastAPI reports failures as {"detail": "..."} — sometimes a list of validation
// objects. Surfacing the raw body means users read a JSON blob with an HTTP code
// bolted to the front, so unwrap it into the sentence the backend actually wrote.
async function errorMessage(res: Response): Promise<string> {
  const body = await res.text().catch(() => "");
  if (body) {
    try {
      const parsed = JSON.parse(body);
      const detail = parsed?.detail ?? parsed?.message;
      if (typeof detail === "string" && detail.trim()) return detail;
      if (Array.isArray(detail)) {
        const msgs = detail.map((d) => d?.msg).filter(Boolean);
        if (msgs.length) return msgs.join("; ");
      }
    } catch {
      // Not JSON — fall through to the raw body, which is usually plain text.
    }
    if (body.length < 400) return body;
  }
  if (res.status === 404) return "That resource no longer exists.";
  if (res.status >= 500) return "The backend hit an error. Check its logs for details.";
  return `Request failed (${res.status}).`;
}

async function req<T>(
  path: string,
  init?: RequestInit,
  timeoutMs: number = DEFAULT_TIMEOUT_MS
): Promise<T> {
  // fetch has no default timeout; abort manually so callers never hang forever.
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      cache: "no-store",
      // Send/receive the GitHub session cookie (same-site localhost:3000↔:8000).
      credentials: "include",
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(await errorMessage(res));
    if (res.status === 204) return undefined as T;
    return res.json();
  } catch (e: any) {
    if (e?.name === "AbortError") {
      throw new Error(
        `Request timed out after ${Math.round(timeoutMs / 1000)}s — the backend ` +
          `may be down (:8000) or a local model is still generating.`
      );
    }
    throw e;
  } finally {
    clearTimeout(timeout);
  }
}

export const api = {
  listProjects: () => req<Project[]>("/api/projects"),
  getProject: (id: string) => req<Project>(`/api/projects/${id}`),
  createProject: (body: {
    idea: string;
    name?: string;
    routing_mode?: string;
    preferred_model?: string;
    require_approval?: boolean;
  }) => req<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  run: (id: string) =>
    req<{ message: string; status: string }>(
      `/api/projects/${id}/run`,
      { method: "POST" },
      LLM_TIMEOUT_MS
    ),
  approve: (id: string) =>
    req<{ message: string; status: string }>(
      `/api/projects/${id}/approve`,
      { method: "POST" },
      LLM_TIMEOUT_MS
    ),
  reject: (id: string, feedback: string) =>
    req<{ message: string; status: string }>(
      `/api/projects/${id}/reject`,
      { method: "POST", body: JSON.stringify({ feedback }) },
      LLM_TIMEOUT_MS
    ),
  routerStatus: () => req<any>("/api/models/status"),
  pipelineShape: () => req<{ phases: Phase[]; mermaid: string }>("/api/models/pipeline"),
  analytics: (id: string) => req<any>(`/api/analytics/projects/${id}`),

  // ── Generated-project artifacts (Preview / Summary / Download) ──
  getArtifacts: (id: string) => req<Artifacts>(`/api/projects/${id}/artifacts`),
  downloadUrl: (id: string) => `${BASE}/api/projects/${id}/download`,

  // ── Visual preview (render + select-to-edit) ──
  getPreview: (id: string) => req<PreviewState>(`/api/projects/${id}/preview`),
  generatePreview: (id: string) =>
    req<PreviewState>(
      `/api/projects/${id}/preview/generate`,
      { method: "POST" },
      LLM_TIMEOUT_MS
    ),
  editPreviewSection: (id: string, section_id: string, instruction: string) =>
    req<PreviewState>(
      `/api/projects/${id}/preview/edit`,
      { method: "POST", body: JSON.stringify({ section_id, instruction }) },
      LLM_TIMEOUT_MS
    ),
  undoPreview: (id: string) =>
    req<PreviewState>(`/api/projects/${id}/preview/undo`, { method: "POST" }),

  // ── Settings: cloud API keys + local model ──
  getProviders: () =>
    req<{ providers: Record<string, ProviderSetting>; default_mode: string }>(
      "/api/settings/providers"
    ),
  setProviderKey: (
    provider: string,
    body: { api_key?: string | null; default_model?: string }
  ) =>
    req<ProviderSetting>(`/api/settings/providers/${provider}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getLocalModel: () => req<LocalStatus>("/api/settings/local"),

  // ── GitHub publishing (OAuth "Connect" → push to the user's own account) ──
  githubStatus: () => req<GithubStatus>("/api/github/status"),
  // Full-page redirect into GitHub's login; returns here with ?github=connected.
  githubConnectUrl: (returnTo: string) =>
    `${BASE}/api/github/oauth/start?return_to=${encodeURIComponent(returnTo)}`,
  githubDisconnect: () =>
    req<{ connected: boolean }>("/api/github/disconnect", { method: "POST" }),
  pushToGithub: (
    id: string,
    body: { name?: string; private?: boolean; description?: string }
  ) =>
    req<GithubPushResult>(
      `/api/github/push/${id}`,
      { method: "POST", body: JSON.stringify(body) },
      LLM_TIMEOUT_MS
    ),
  // Streams NDJSON pull progress; calls onLine for each parsed object.
  pullLocalModel: async (
    model: string,
    onLine: (line: PullProgress) => void
  ): Promise<void> => {
    const res = await fetch(`${BASE}/api/settings/local/pull`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    });
    if (!res.ok || !res.body) throw new Error(await errorMessage(res));
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const raw = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (raw) {
          try {
            onLine(JSON.parse(raw) as PullProgress);
          } catch {
            /* ignore non-JSON keepalive lines */
          }
        }
      }
    }
  },
};

export type GithubStatus = {
  configured: boolean;
  connected: boolean;
  login: string | null;
  name: string | null;
  avatar: string | null;
};

export type GithubPushResult = {
  html_url: string;
  full_name: string;
  branch: string;
  private: boolean;
  files: number;
};

export type ProviderSetting = {
  configured: boolean;
  available: boolean;
  key_hint: string | null;
  default_model: string | null;
};

export type LocalStatus = {
  base_url: string;
  reachable: boolean;
  models: string[];
  default_model: string;
  has_default: boolean;
};

export type PullProgress = {
  status?: string;
  total?: number;
  completed?: number;
  error?: string;
};

export type PreviewSection = { id: string; label: string };
export type PreviewRevision = {
  id: string;
  source: string;
  section_id: string | null;
  instruction: string | null;
  model_used: string | null;
  provider_used: string | null;
  created_at: string;
};
export type PreviewState = {
  project_id: string;
  html: string | null;
  sections: PreviewSection[];
  revisions: PreviewRevision[];
  has_frontend: boolean;
};

export type GenFile = { path: string; content: string; language: string; phase: string };
export type GenDoc = { path: string; title: string; content: string };
export type Artifacts = {
  idea: string;
  name: string | null;
  status: string;
  readme: string;
  files: GenFile[];
  setup_instructions: string[];
  docs: GenDoc[];
};

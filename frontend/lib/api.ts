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
  /** running | pending_approval | approved | rejected | failed */
  status: string;
  output: Record<string, unknown>;
  content_md: string;
  model_used: string | null;
  provider_used: string | null;
  feedback: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  total_tokens: number;
  latency_ms: number;
  /**
   * Did this agent return the shape it declares, and did it take a second attempt?
   * `null` on rows written before the check existed — which is not the same as
   * "valid", so the UI says nothing at all for those rather than a reassuring badge.
   */
  schema_status: "valid" | "repaired" | "invalid" | null;
  /** What was wrong, when something was. */
  schema_note: string | null;

  /**
   * Does this phase agree with the stack the architecture froze? A separate answer
   * from `schema_status`: a deliverable can match its declared shape perfectly and
   * still be written against a database nothing else in the build uses.
   */
  stack_status: "ok" | "violated" | null;
  /** Each contradiction, in the words the agent was sent back with. */
  stack_note: string[] | null;

  /**
   * Does this phase's code compile? A third answer beside shape and stack: code can
   * match its shape and agree with the stack and still not parse. `null` for phases
   * that write no code, and for rows from before the check existed.
   */
  build_status: "ok" | "failed" | "unchecked" | null;
  /** What still does not compile, after the one repair round. */
  build_note: BuildProblem[] | null;

  /**
   * The procedural skills this phase was actually given, by name and in the order
   * they were injected. `null` on rows written before the library existed — which
   * is not the same as `[]`, so the UI says nothing for those rather than reporting
   * that an agent was offered skills and took none.
   */
  skills_used: string[] | null;
};

/** One reason a generated file does not compile. */
export type BuildProblem = {
  path: string;
  line: number | null;
  /** syntax | reference | import | package */
  kind: string;
  message: string;
  /** Set when gathered across phases: the agent that wrote the file. */
  phase?: string;
};

/** One security finding, and what has actually been done about it. */
export type SecurityFinding = {
  key: string;
  title: string;
  severity: string;
  category: string;
  location: string;
  recommendation: string;
  /** The phase that wrote the offending file. Null when nothing owns it. */
  owner_phase: string | null;
  status: "open" | "fix_requested" | "fixed" | "gone" | "waived";
  /** The reviewer's reason for a waiver. */
  note: string | null;
};

export type SecurityState = {
  findings: SecurityFinding[];
  /** Critical/high findings neither fixed nor waived — what blocks approval. */
  unresolved: number;
  rounds_used: number;
  rounds_allowed: number;
};

export type ApprovalMode = "checkpoints" | "every_phase" | "unattended";
export type GateKind =
  | "plan"
  | "ship"
  | "security"
  | "cost"
  | "phase"
  | "unchecked"
  | "stack"
  | "build";

/**
 * One technology decision the whole crew is held to.
 *
 * `source` is who decided it — the debate that settled the question, the
 * architecture that drew it, or an unavoidable consequence of one of those. Shown,
 * because "PostgreSQL because the team argued it out" and "PostgreSQL because
 * FastAPI implies it" are different strengths of claim.
 */
export type CharterChoice = {
  token: string;
  label: string;
  source: "debate" | "system_design" | "implied";
};
export type Charter = Partial<Record<CharterCategory, CharterChoice>>;
export type CharterCategory =
  | "language"
  | "backend_framework"
  | "frontend_framework"
  | "database"
  | "test_runner"
  | "package_manager";

export type Project = {
  id: string;
  idea: string;
  name: string | null;
  /** created | running | awaiting_approval | completed | failed | cancelled */
  status: string;
  current_phase: string | null;
  routing_mode: string;
  preferred_model: string | null;
  require_approval: boolean;

  /** How often this run stops for review: checkpoints | every_phase | unattended. */
  approval_mode: ApprovalMode;
  /** Projected monthly run cost above which the build interrupts itself. */
  cost_cap_usd: number | null;
  /** Which review to show while waiting: plan | ship | security | cost | phase. */
  gate_kind: GateKind | null;
  /** One line on why the run stopped here — a severe finding, a cost overrun. */
  gate_note: string | null;

  /**
   * The technology decisions frozen after the architecture was approved. `null`
   * before System Design has run, and on a build whose architecture named nothing
   * this pipeline recognises — in both cases nothing is being enforced, and the UI
   * says so rather than showing an empty table as if it were a stack.
   */
  charter: Charter | null;
  /** How many times this build has been sent back to fix its own security findings. */
  remediation_rounds: number | null;
  /** Skills this build forces on or off, over what keyword scoring would choose. */
  skill_overrides: SkillOverrides | null;

  created_at: string;
  updated_at: string;
  phases: PhaseResult[];

  // Liveness, so the UI never has to guess whether a `running` build is alive.
  phase_started_at: string | null;
  heartbeat_at: string | null;
  cancel_requested: boolean;
  last_error: string | null;
  /** `running`, but nothing is driving it. The server owns the threshold. */
  stalled: boolean;
  elapsed_seconds: number | null;
};

// Fast CRUD calls should fail fast so a hung/restarting backend surfaces an error
// instead of an infinite "Loading…". LLM-driven endpoints (generate / edit a preview)
// drive a local Ollama model that can take far longer than 15s, so they pass the
// longer cap below.
//
// Pipeline control (run / approve / reject / stop / resume) is deliberately NOT in
// that second group any more: those endpoints hand the phase to a background task
// and return immediately, so a slow one means the backend is in trouble — not that
// a model is thinking.
const DEFAULT_TIMEOUT_MS = 15000;
const LLM_TIMEOUT_MS = 300000; // 5 min — local generation on CPU is slow

/**
 * A failed request, carrying the code as well as the sentence.
 *
 * The message alone is not enough to decide what a failure *means*: a 404 on
 * `/api/github/status` says "this server has no GitHub router", which is the
 * not-configured state, not an error worth a red card. Callers that care read
 * `status`; everything else keeps treating it as an ordinary Error.
 */
export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

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
    if (!res.ok) throw new ApiError(await errorMessage(res), res.status);
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
    approval_mode?: ApprovalMode;
    cost_cap_usd?: number;
    skill_overrides?: SkillOverrides;
  }) => req<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  // Review policy is editable while the run is in flight — the runner re-reads it
  // before every handoff.
  updateProject: (
    id: string,
    body: {
      approval_mode?: ApprovalMode;
      cost_cap_usd?: number;
      clear_cost_cap?: boolean;
    },
  ) => req<Project>(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteProject: (id: string) =>
    req<void>(`/api/projects/${id}`, { method: "DELETE" }),

  // ── Pipeline control — all of these return in well under a second ──
  run: (id: string) => req<RunResponse>(`/api/projects/${id}/run`, { method: "POST" }),
  approve: (id: string) =>
    req<RunResponse>(`/api/projects/${id}/approve`, { method: "POST" }),
  reject: (id: string, feedback: string) =>
    req<RunResponse>(`/api/projects/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ feedback }),
    }),
  stop: (id: string, reason?: string) =>
    req<RunResponse>(`/api/projects/${id}/stop`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),
  resume: (id: string) => req<RunResponse>(`/api/projects/${id}/resume`, { method: "POST" }),
  /** Send one named phase back to its agent, from whichever review is on screen. */
  redo: (id: string, phase: string, feedback: string) =>
    req<RunResponse>(`/api/projects/${id}/redo`, {
      method: "POST",
      body: JSON.stringify({ phase, feedback }),
    }),
  routerStatus: () => req<RouterStatus>("/api/models/status"),
  pipelineShape: () => req<{ phases: Phase[]; mermaid: string }>("/api/models/pipeline"),
  analytics: (id: string) => req<any>(`/api/analytics/projects/${id}`),

  // ── Generated-project artifacts (Preview / Summary / Download) ──
  getArtifacts: (id: string) => req<Artifacts>(`/api/projects/${id}/artifacts`),
  downloadUrl: (id: string) => `${BASE}/api/projects/${id}/download`,

  // ── Visual preview (render + select-to-edit) ──
  getPreview: (id: string) => req<PreviewState>(`/api/projects/${id}/preview`),
  // Starts the build and returns at once; `getPreview` reports how far it has got.
  generatePreview: (id: string) =>
    req<PreviewState>(`/api/projects/${id}/preview/generate`, { method: "POST" }),
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
  /** Select which model the local runtime runs. The reason the pull button exists. */
  setLocalModel: (model: string) =>
    req<LocalStatus>("/api/settings/providers/ollama", {
      method: "PUT",
      body: JSON.stringify({ default_model: model }),
    }),

  // ── Settings: which model each agent runs on ──
  getRoles: () => req<RoleSettings>("/api/settings/roles"),
  /** `null` puts the role back on the default model. */
  setRoleModel: (role: string, model: string | null) =>
    req<RoleSettings>(`/api/settings/roles/${role}`, {
      method: "PUT",
      body: JSON.stringify({ model }),
    }),

  // ── Skills: the procedural library the agents are given ──
  listSkills: () => req<SkillLibrary>("/api/skills"),
  createSkill: (body: SkillDraft) =>
    req<Skill>("/api/skills", { method: "POST", body: JSON.stringify(body) }),
  updateSkill: (name: string, body: SkillDraft) =>
    req<Skill>(`/api/skills/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  /** Switch one off for every build that does not name it explicitly. */
  setSkillEnabled: (name: string, enabled: boolean) =>
    req<Skill>(`/api/skills/${name}/enabled`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  /** Removes a skill added here; on an edited bundled one, restores the original. */
  deleteSkill: (name: string) =>
    req<{ deleted: string; restored: Skill | null }>(`/api/skills/${name}`, {
      method: "DELETE",
    }),
  /**
   * Which skills each phase would get for an idea. Selection is a keyword score, so
   * a miss is silent — this is the only way to see one without spending a build.
   */
  previewSkills: (body: { idea: string; pinned?: string[]; excluded?: string[] }) =>
    req<SkillPreview>("/api/skills/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── Security findings: fix them, or waive them on the record ──
  getSecurity: (id: string) => req<SecurityState>(`/api/projects/${id}/security`),
  fixFinding: (id: string, key: string) =>
    req<RunResponse>(`/api/projects/${id}/security/${key}/fix`, { method: "POST" }),
  waiveFinding: (id: string, key: string, reason: string) =>
    req<{ key: string; status: string; note: string; unresolved: number }>(
      `/api/projects/${id}/security/${key}/waive`,
      { method: "POST", body: JSON.stringify({ reason }) },
    ),

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
    if (!res.ok || !res.body) throw new ApiError(await errorMessage(res), res.status);
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

/** Skills a single build forces on or off, over what keyword scoring would choose. */
export type SkillOverrides = { pinned: string[]; excluded: string[] };

/** One piece of procedural knowledge in the library. */
export type Skill = {
  name: string;
  title: string;
  description: string;
  /** Phases that may receive it. Empty means every phase. */
  agents: string[];
  /** What makes it relevant to a particular build. */
  keywords: string[];
  body: string;
  /** `bundled` ships with the platform; `user` was added on this machine. */
  source: "bundled" | "user";
  chars: number;
  enabled: boolean;
  /**
   * Whether it may be injected at all. A skill over the length ceiling, or one
   * carrying an instruction about output format, stays listed with its reasons
   * showing — a file that exists, does nothing and never says why is worse.
   */
  usable: boolean;
  problems: string[];
  /** A local edit currently shadowing a bundled skill of the same name. */
  overridden: boolean;
};

export type SkillDraft = {
  name?: string;
  title: string;
  description: string;
  agents: string[];
  keywords: string[];
  body: string;
};

export type SkillLibrary = {
  skills: Skill[];
  phases: { key: string; label: string }[];
  /** False when skills are switched off for this backend entirely. */
  enabled: boolean;
  max_per_phase: number;
  max_chars: number;
  user_dir: string;
  bundled_dir: string;
};

/** One skill a phase would receive, and why it was chosen. */
export type SkillPick = {
  name: string;
  title: string;
  score: number;
  pinned: boolean;
  /** The keywords that actually hit. Empty on a pin that matched nothing. */
  matched: string[];
  reason: string;
  chars: number;
};

export type SkillPreview = {
  idea: string;
  phases: { phase: string; label: string; skills: SkillPick[] }[];
};

export type RunResponse = {
  project_id: string;
  status: string;
  current_phase: string | null;
  message: string;
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

export type RouterStatus = {
  default_mode: string;
  providers: Record<
    string,
    { available: boolean; is_local: boolean; default_model: string | null }
  >;
  fallback_chain: string[];
};

export type ProviderSetting = {
  configured: boolean;
  available: boolean;
  key_hint: string | null;
  default_model: string | null;
};

/**
 * What the local model reported about itself when the backend probed it. The
 * window here is the window agents actually get — the same resolved number the
 * pipeline sends as `num_ctx` — not a second guess at it.
 */
export type ModelProfile = {
  provider: string;
  model: string;
  /** What the model was trained for. `null` when it does not report one. */
  context_limit: number | null;
  /** What we ask for: the limit, lowered by RAM or by a configured ceiling. */
  context_window: number;
  max_output_tokens: number;
  prompt_token_budget: number;
  parameter_count: number | null;
  parameter_size: string | null;
  quantization: string | null;
  capabilities: string[];
  /** True when decoding is constrained to each agent's schema, not just to JSON. */
  supports_schema_format: boolean;
  source: "probe" | "configured" | "fallback";
  /** Why the window sits below the model's own limit, when it does. */
  clamp_reason: string | null;
  is_small: boolean;
  warnings: string[];
};

export type LocalStatus = {
  base_url: string;
  reachable: boolean;
  models: string[];
  default_model: string;
  has_default: boolean;
  /** Null while Ollama is unreachable or the default model isn't pulled yet. */
  profile: ModelProfile | null;
  /**
   * Pulled models whose name suggests they were trained for code. A suggestion for
   * the code phases, derived from what you actually have — never a default the
   * router reaches for, because a name is not a capability.
   */
  code_models: string[];
};

/** One role a model can be chosen for: the eight agents, plus the support tasks. */
export type RoleRow = {
  role: string;
  label: string;
  /** What this role is for, in a few words. */
  what: string;
  kind: "phase" | "support";
  /** The user's choice, or null for "use the default model". */
  assigned: string | null;
  provider: string | null;
  model: string | null;
};

export type RoleSettings = {
  roles: RoleRow[];
  /** What a role with no choice of its own runs on. */
  default_model: string;
  local_models: string[];
  /**
   * Pulled models whose name suggests they were trained on code. Derived by the
   * backend so there is one rule rather than two that disagree — a second copy here
   * was missing `codellama`, which the other card was already badging as code.
   */
  code_models: string[];
  /** `provider:model` for each cloud provider with a key configured. */
  cloud_models: string[];
};

export type PullProgress = {
  status?: string;
  total?: number;
  completed?: number;
  error?: string;
};

export type PreviewSection = {
  id: string;
  label: string;
  /** The page it is on; null for the shared header/footer and for older mockups. */
  route?: string | null;
  kind?: string | null;
};
export type PreviewRoute = { path: string; title: string };

/** A mockup build in flight — or the last one, if it failed. */
export type PreviewJob = {
  stage: "queued" | "design" | "plan" | "seed" | "sections" | "verify" | "done" | "failed";
  label: string;
  done: number;
  total: number;
  detail: string;
  error: string | null;
  running: boolean;
  /** "pipeline" when the Frontend phase started it, "request" when a person did. */
  origin: string;
  elapsed_s: number;
};

export type MockupCheck = { name: string; ok: boolean; detail: string };
export type MockupSectionReport = {
  id: string;
  kind: string;
  label: string;
  route: string | null;
  collection: string | null;
  /** generated | repaired | fallback — how the section came to be. */
  status: "generated" | "repaired" | "fallback";
  problems: string[];
  fixes: string[];
  bytes: number;
};
/** What building the current mockup found. */
export type MockupReport = {
  version: number;
  product: string;
  model: string;
  provider: string;
  passes: { design: string; plan: string; seed: string };
  routes: { path: string; title: string; sections: number }[];
  collections: { name: string; label: string; rows: number; source: string }[];
  sections: MockupSectionReport[];
  counts: { generated: number; repaired: number; fallback: number };
  checks: MockupCheck[];
  render: { ran: boolean; reason?: string; console_errors?: number; errors?: string[] };
  bytes: number;
  calls: number;
  tokens: number;
  elapsed_ms: number;
};
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
  /** The mockup's pages, in order. Empty for a single-page mockup. */
  routes: PreviewRoute[];
  revisions: PreviewRevision[];
  has_frontend: boolean;
  report: MockupReport | null;
  job: PreviewJob | null;
};

export type GenFile = {
  path: string;
  content: string;
  language: string;
  /** The agent that wrote it, or "platform" for the scaffold's own files. */
  phase: string;
  /** What the platform wrote it for, or changed in it and why. */
  notes?: string[];
  /** What still does not compile in it. */
  problems?: BuildProblem[];
};
export type GenDoc = { path: string; title: string; content: string };
export type Artifacts = {
  idea: string;
  name: string | null;
  status: string;
  readme: string;
  files: GenFile[];
  setup_instructions: string[];
  docs: GenDoc[];
  scaffold?: {
    frontend: string | null;
    backend: string | null;
    commands: string[];
    notes: string[];
    files: string[];
    replaced: string[];
  };
  build?: {
    /** null when nothing was checked (a build from before the gate). */
    status: "ok" | "failed" | "unchecked" | null;
    phases: Record<string, string | null>;
    problems: BuildProblem[];
  };
};

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
  /** Why the first model was passed over: "OpenAI: out of credit; continued on …". */
  fallback_note?: string | null;
  /**
   * Whether the model ran on hardware the user controls — the backend's answer,
   * recorded per call. Never guessed from `provider_used`: a source's name says
   * nothing about where its model runs.
   */
  is_local: boolean | null;
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
   * The real build (#75): what installing, building and starting this phase's code in
   * a sandbox did. `null` when nothing was built — a phase that isn't built, code
   * that didn't parse, or a row from before real builds.
   */
  build_run?: BuildRun | null;

  /**
   * The procedural skills this phase was actually given, by name and in the order
   * they were injected. `null` on rows written before the library existed — which
   * is not the same as `[]`, so the UI says nothing for those rather than reporting
   * that an agent was offered skills and took none.
   */
  skills_used: string[] | null;
  /**
   * What this agent was shown of the phases before it (#80). `null` on rows
   * written before hand-offs were recorded — the UI then says nothing.
   */
  handoff?: Handoff | null;
};

export type HandoffDep = {
  phase: string;
  digest: boolean;
  /** whole: the full output fit. cut: some fields or files left out. digest_only: none of it fit. */
  full: "whole" | "cut" | "digest_only";
  /** What was left out: field names, or `files[7-19 of 19]`. */
  omitted: string[];
};

export type Handoff = {
  deps: HandoffDep[];
  registry: boolean;
  contract: boolean;
  truncated_replies: number;
  /** How a code phase wrote its code (#81). Absent on other phases and older rows. */
  generation?: Generation | null;
};

/** One file a code phase planned before writing it (#81). */
export type PlannedFile = {
  path: string;
  purpose: string;
  exports: string[];
  imports: string[];
  /** plan | split (a module a cut-off file was split into) | unplanned */
  origin: "plan" | "split" | "unplanned";
};

/**
 * How a code phase wrote its code (#81): `one` file per call, a `batch` per call
 * sized to the model's window, or the old `whole` single JSON reply.
 */
export type Generation = {
  mode: "one" | "batch" | "whole";
  /** Why it wrote in one reply, for `whole`. */
  reason?: string;
  files_per_call: number;
  calls: number;
  plan_calls?: number;
  files_planned: number;
  files_written: number;
  truncated_replies: number;
  repairs?: number;
  plan?: PlannedFile[];
  unwritten?: string[];
  left_to_platform?: string[];
  /** Whether each written file parsed as it landed. */
  files?: Record<string, "ok" | "failed">;
};

/** A file's state while a code phase writes it (#81). */
export type ActivityFileState = "planned" | "writing" | "ok" | "fixing" | "failed" | "missing";

/** What the running phase is doing inside itself (#81). */
export type Activity = {
  phase: string;
  /** `building`: installing, building and starting the code in a sandbox (#75). */
  stage: "planning" | "writing" | "fixing" | "checking" | "building";
  label: string;
  done: number;
  total: number;
  /** The file being written or fixed now. */
  detail: string;
  per_call: number;
  files: { path: string; state: ActivityFileState }[];
  elapsed_s: number;
};


/** One reason a generated file does not compile. */
export type BuildProblem = {
  path: string;
  line: number | null;
  /** syntax | reference | import | package | type | runtime | build */
  kind: string;
  message: string;
  /** Set when gathered across phases: the agent that wrote the file. */
  phase?: string;
  /** Which step of a real build found it (#75). Absent for what the parser found. */
  step?: BuildStepName | "vercel";
};

export type BuildStepName = "install" | "build" | "boot";

/** One step of a real build: `npm install`, `next build`, `node server.js`. */
export type BuildRunStep = {
  name: BuildStepName;
  label: string;
  exit_code: number | null;
  seconds: number;
  ok: boolean;
  timed_out: boolean;
  skipped: boolean;
  /** It ran until it reached for a database or the network the sandbox doesn't have. */
  inconclusive?: boolean;
  /** The last lines of its output. */
  tail: string;
};

/** What installing, building and starting a code phase's output did (#75). */
export type BuildRun = {
  status: "ok" | "failed" | "unchecked";
  side: string;
  /** nextjs | vite | node | python */
  stack: string | null;
  /** docker | builder — where it ran. */
  runner: string | null;
  image: string | null;
  /** "Installed 105 packages · `next build` passed in 12 s" */
  summary: string;
  /** Why it was unchecked. */
  reason: string | null;
  /** A step left out, and why. */
  note: string | null;
  seconds: number;
  packages: number | null;
  steps: BuildRunStep[];
  problems: BuildProblem[];
  at: string;
};

/** What runs the real builds, for the Settings row. */
export type BuildRunnerStatus = {
  kind: "docker" | "builder" | "none" | "off";
  available: boolean;
  reason: string | null;
  /** "Docker 29.4.0", or the builder service's address. */
  detail?: string | null;
  images?: string[];
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
  /** The crew's to fix by itself (true), or small enough to be the reviewer's call. */
  serious: boolean;
  /** The automatic round whose re-audit no longer reported it, when one did. */
  fixed_round: number | null;
  /** Why a serious finding was waived. */
  waive_kind: WaiveKind | null;
};

export type WaiveKind = "false_positive" | "mitigated" | "accepted_risk";

export type SecurityState = {
  findings: SecurityFinding[];
  /** Critical/high findings neither fixed nor waived — what blocks approval. */
  unresolved: number;
  /** Of those, the small ones: what the Security stop asks about. */
  unresolved_small: number;
  rounds_used: number;
  rounds_allowed: number;
};

/** One thing a fix round was sent to fix. */
export type AutoFixProblem = {
  key: string;
  title: string;
  severity?: string;
  where: string | null;
  /** The phase that was sent back to fix it. */
  phase: string;
  kind: "security" | "build" | "stack";
  /** Which step of a real build found it (#75); absent for the parser's own. */
  step?: BuildStepName | "vercel";
};

export type AutoFixRound = {
  n: number;
  strategy: "guided" | "with_code" | "stronger_model";
  phases: string[];
  problems: AutoFixProblem[];
  /** `vercel`: the round was opened by a failed Vercel deploy (#75). */
  source?: "vercel";
  /** Keys the re-check no longer reports. `null` while the round is still running. */
  fixed: string[] | null;
  remaining?: string[];
  at: string;
  checked_at?: string;
};

export type AutoFixTrack = {
  allowed: number;
  rounds: AutoFixRound[];
  stopped: { reason: "limit" | "no_progress"; left: number; at: string } | null;
  accepted: { kind: WaiveKind; reason: string; at: string } | null;
  resumed_after: number;
  /** Where the current episode began: a fresh problem gets a fresh budget. */
  episode_start?: number;
};

/** The crew's own fix loop: `security`, and `build:<phase>` per phase. */
export type AutoFix = { tracks: Record<string, AutoFixTrack> };

export type ApprovalMode = "checkpoints" | "every_phase" | "unattended";
export type GateKind =
  | "plan"
  | "ship"
  | "security"
  | "cost"
  | "phase"
  | "unchecked"
  | "stack"
  | "build"
  | "needs_help"
  | "database"
  | "integrations";

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
export type Charter = Partial<Record<CharterCategory, CharterChoice>> & {
  /** Which host the database lives on: supabase, neon, atlas, planetscale, firebase, aws, generic. */
  database_provider?: string;
  /** The variable names the code reads: the database's, then every connector's. */
  env?: string[];
  /** The app connectors this build uses — stripe, resend, … (#59). */
  integrations?: string[];
  /** `env` without the connectors' names: what the database is read from. */
  database_env?: string[];
};
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
  /** created | running | awaiting_approval | completed | failed | cancelled | paused */
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
  /** connected | unchecked | later | none — or null until asked. Never a value. */
  database_status?: DatabaseStatus | null;
  /** App connectors: what was chosen before the start, and the "later" answers. */
  integrations_choice?: { use?: string[]; skip?: string[] } | null;
  integrations_status?: Record<string, "later"> | null;
  /** Connectors answered "later" (or whose key failed), read live by the server. */
  connectors_unconnected?: string[];
  /** Where the finished build went: its repo (`owner/name`) and its live deploy. */
  github_repo?: string | null;
  github_pushed_at?: string | null;
  deploy_target?: "vercel" | "render" | null;
  deploy_url?: string | null;
  deploy_status?: string | null;

  /**
   * The technology decisions frozen after the architecture was approved. `null`
   * before System Design has run, and on a build whose architecture named nothing
   * this pipeline recognises — in both cases nothing is being enforced, and the UI
   * says so rather than showing an empty table as if it were a stack.
   */
  charter: Charter | null;
  /** How many times this build has been sent back to fix its own security findings. */
  remediation_rounds: number | null;
  /** The crew fixing its own serious problems, round by round. */
  auto_fix: AutoFix | null;
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
  /** Set when a cloud provider refused a key: why, which provider, and the fix. */
  last_error_kind?: string | null;
  last_error_provider?: string | null;
  last_error_help?: KeyAdvice | null;
  /** `running`, but nothing is driving it. The server owns the threshold. */
  stalled: boolean;
  elapsed_seconds: number | null;
  /** What the running phase is doing inside itself — planning, or which file (#81). */
  activity?: Activity | null;
};


// Fast CRUD calls should fail fast so a hung/restarting backend surfaces an error
// instead of an infinite "Loading…". LLM-driven endpoints (generate / edit a preview)
// drive a local model that can take far longer than 15s, so they pass the
// longer cap below.
//
// Pipeline control (run / approve / reject / stop / resume) is deliberately NOT in
// that second group any more: those endpoints hand the phase to a background task
// and return immediately, so a slow one means the backend is in trouble — not that
// a model is thinking.
const DEFAULT_TIMEOUT_MS = 15000;
const LLM_TIMEOUT_MS = 300000; // 5 min — local generation on CPU is slow
// Two bounded requests to the provider, plus listing models when the one chosen isn't there.
const KEY_CHECK_TIMEOUT_MS = 60000;

/**
 * A failed request, carrying the code as well as the sentence.
 *
 * The message alone is not enough to decide what a failure *means*: a 404 on
 * `/api/github/status` says "this server has no GitHub router", which is the
 * not-configured state, not an error worth a red card. Callers that care read
 * `status`; everything else keeps treating it as an ordinary Error.
 */
/** Fired on `window` whenever the backend says nobody is signed in. */
export const SIGNED_OUT_EVENT = "aiteam:signed-out";

export type Account = {
  id: string;
  email: string | null;
  display_name: string | null;
  /** The account that owns this install: the first one. */
  is_owner: boolean;
};

export type AuthStatus = {
  user: Account | null;
  /** No account can sign in yet — the first one sets the install up. */
  needs_setup: boolean;
  /** Setting up from this browser needs the install's SETUP_TOKEN. */
  setup_needs_token: boolean;
  /** The install takes new accounts. */
  signup_open: boolean;
  /** Builds and settings from before accounts are waiting for the first account. */
  has_unclaimed_work: boolean;
};

// ── App connectors (#59) ────────────────────────────────────────────────────
export type ConnectorVar = {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
  kind: string;
  help: string;
  /** `client` variables go in the browser bundle: publishable by design. */
  side: "server" | "client";
  /** A fixed choice (`sandbox`/`live`): a picker, first is the default. */
  options?: string[];
  /** Left blank, a random secret is generated for it. */
  generate?: boolean;
  /** Always another variable's value — never asked for. */
  copy_of?: string;
};
/** What a refused key means and the one thing that fixes it — the backend's words
 * (`app/core/keyerrors.py`), the same in Settings, a build and Connectors (#63). */
export type KeyAdvice = {
  kind:
    | "invalid" | "expired" | "revoked" | "no_credit" | "spend_limit" | "billing_disabled"
    | "plan_quota" | "not_permitted" | "region" | "rate_limited" | "provider_down" | "unknown";
  /** Two or three words for a badge: "Expired", "No credit". */
  badge: string;
  /** What happened, without a full stop. */
  title: string;
  /** What to do about it. */
  body: string;
  action_label: string;
  action_url: string;
  /** False for a rate limit or an outage: nothing for the person to do. */
  blocking: boolean;
};
export type ConnectorCheck = {
  status: "connected" | "unchecked" | "failed";
  message: string;
  reason?: string;
  advice?: KeyAdvice | null;
  name?: string | null;
  step?: number | null;
  host?: string;
  latency_ms?: number | null;
  at?: string;
  mode?: "test" | "live" | null;
  models?: string[];
};
export type ConnectorUse = { id: string; name: string; status: string };
export type Connector = {
  id: string;
  label: string;
  category: string;
  category_label: string;
  capability: string;
  capability_label: string;
  wave: number;
  connectable: boolean;
  blurb: string;
  builds: string[];
  variables: ConnectorVar[];
  guide: { text: string; url: string }[];
  docs_url: string;
  dashboard_url: string;
  has_test_mode: boolean;
  connected: boolean;
  saved?: { name: string; hint: string; side: string }[];
  check?: ConnectorCheck | null;
  mode?: "test" | "live" | null;
  connected_at?: string | null;
  models?: string[];
  used_by?: ConnectorUse[];
};
export type ConnectorCatalog = {
  categories: { id: string; label: string; count: number }[];
  connectors: Connector[];
  connected: number;
};
export type ConnectorProblem = { name: string; message: string; step: number | null };
export type ConnectorSaveResult = {
  ok: boolean;
  status: "connected" | "unchecked" | "failed" | "invalid";
  problems?: ConnectorProblem[];
  check?: ConnectorCheck;
  connector?: Connector;
};
export type ConnectorPick = {
  id: string;
  label: string;
  reason: string;
  source: "idea" | "design" | "user";
  capability: string;
  capability_label: string;
  connected: boolean;
  mode?: string | null;
};
export type ConnectorPreview = {
  connectors: ConnectorPick[];
  skipped: { id: string; label: string }[];
  addable: { id: string; label: string }[];
};
export type IntegrationRow = Connector & {
  status: "connected" | "unchecked" | "later" | "failed" | null;
  source: "account" | "project" | null;
  reason?: string | null;
  account_connected: boolean;
};
export type IntegrationsState = {
  used: string[];
  connectors: IntegrationRow[];
  at_gate: boolean;
  can_change: boolean;
  unanswered: string[];
  not_connected: string[];
  addable: { id: string; label: string }[];
};

export class ApiError extends Error {
  readonly status: number;
  /** The parsed JSON body, when there was one — some refusals say what's missing
   *  (`needs: "github"`) or carry the details a choice needs (`conflict`). */
  readonly data: Record<string, any> | null;
  constructor(message: string, status: number, data: Record<string, any> | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

// FastAPI reports failures as {"detail": "..."} — sometimes a list of validation
// objects. Surfacing the raw body means users read a JSON blob with an HTTP code
// bolted to the front, so unwrap it into the sentence the backend actually wrote.
async function apiError(res: Response): Promise<ApiError> {
  const body = await res.clone().text().catch(() => "");
  let data: Record<string, any> | null = null;
  try {
    const parsed = body ? JSON.parse(body) : null;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) data = parsed;
  } catch {
    // Not JSON: the message says what it can.
  }
  return new ApiError(await errorMessage(res), res.status, data);
}

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
      // Send/receive the session cookies (same-site localhost:3000↔:8000).
      credentials: "include",
      signal: ctrl.signal,
    });
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      // The session ended — signed out elsewhere, or it expired. The shell hears
      // this and takes the person to sign in, back to where they were.
      if (typeof window !== "undefined") window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
    }
    if (!res.ok) throw await apiError(res);
    if (res.status === 204) return undefined as T;
    return res.json();
  } catch (e: any) {
    if (e?.name === "AbortError") {
      throw new Error(
        `Request timed out after ${Math.round(timeoutMs / 1000)}s. The backend ` +
          `may be down (:8000), or a local model is still generating.`
      );
    }
    throw e;
  } finally {
    clearTimeout(timeout);
  }
}

export const api = {
  // ── Your computers (the connector) ──
  setupGuide: () => req<SetupGuide>("/api/devices/setup"),
  listDevices: () => req<{ devices: Device[] }>("/api/devices"),
  startPairing: () => req<Pairing>("/api/devices/pairing", { method: "POST" }),
  pairingState: (id: string) => req<PairingState>(`/api/devices/pairing/${id}`),
  cancelPairing: (id: string) => req<{ ok: boolean }>(`/api/devices/pairing/${id}`, { method: "DELETE" }),
  approveDevice: (id: string) =>
    req<Device>(`/api/devices/${id}/approve`, { method: "POST" }, LLM_TIMEOUT_MS),
  refreshDevice: (id: string) =>
    req<Device>(`/api/devices/${id}/refresh`, { method: "POST" }, LLM_TIMEOUT_MS),
  updateDevice: (id: string, body: { name?: string; chat_model?: string; embed_model?: string }) =>
    req<Device>(`/api/devices/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deviceModel: (id: string, spec: string) =>
    req<DeviceModelInfo>(`/api/devices/${id}/model?spec=${encodeURIComponent(spec)}`, {}, LLM_TIMEOUT_MS),
  disconnectDevice: (id: string) =>
    req<{ ok: boolean; was_connected: boolean }>(`/api/devices/${id}/disconnect`, { method: "POST" }),
  forgetDevice: (id: string) => req<{ ok: boolean }>(`/api/devices/${id}`, { method: "DELETE" }),
  /** A real round trip to the computer's model; a refusal comes back as `ok: false`. */
  testDevice: (id: string, spec?: string) =>
    req<DeviceTestResult>(
      `/api/devices/${id}/test`,
      { method: "POST", body: JSON.stringify(spec ? { spec } : {}) },
      LLM_TIMEOUT_MS,
    ),

  // ── Accounts ──
  authStatus: () => req<AuthStatus>("/api/auth/status"),
  signIn: (body: { email: string; password: string }) =>
    req<{ user: Account }>("/api/auth/signin", { method: "POST", body: JSON.stringify(body) }),
  signUp: (body: { email: string; password: string; display_name?: string; setup_token?: string }) =>
    req<{ user: Account }>("/api/auth/signup", { method: "POST", body: JSON.stringify(body) }),
  signOut: () => req<{ ok: boolean }>("/api/auth/signout", { method: "POST" }),

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
    connectors?: { use: string[]; skip: string[] };
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
  /** Would a build with these settings start? The same answer `run` will give. */
  preflight: (body: { routing_mode: string; preferred_model?: string }) =>
    req<Preflight>("/api/projects/preflight", {
      method: "POST",
      body: JSON.stringify(body),
    }),
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
  downloadUrl: (id: string, includeCredentials = false) =>
    `${BASE}/api/projects/${id}/download${includeCredentials ? "?include_credentials=true" : ""}`,

  // ── The build's database (write-only: values go in, hints come back) ──
  getDatabase: (id: string, provider?: string) =>
    req<DatabaseState>(
      `/api/projects/${id}/database${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`,
    ),
  checkDatabase: (id: string, values: Record<string, string>, provider?: string) =>
    req<DatabaseSaveResult>(`/api/projects/${id}/database/check`, {
      method: "POST",
      body: JSON.stringify({ values, provider: provider ?? null }),
    }),
  removeDatabase: (id: string) =>
    req<DatabaseState>(`/api/projects/${id}/database`, { method: "DELETE" }),
  databaseContinue: (id: string) =>
    req<RunResponse>(`/api/projects/${id}/database/continue`, { method: "POST" }),
  databaseLater: (id: string) =>
    req<RunResponse>(`/api/projects/${id}/database/later`, { method: "POST" }),

  // ── App connectors: the account's Connectors tab (write-only, like the database) ──
  listConnectors: () => req<ConnectorCatalog>("/api/connectors"),
  getConnector: (id: string) => req<Connector>(`/api/connectors/${id}`),
  connect: (id: string, values: Record<string, string>, confirm_live = false) =>
    req<ConnectorSaveResult>(`/api/connectors/${id}`, {
      method: "PUT",
      body: JSON.stringify({ values, confirm_live }),
    }),
  retestConnector: (id: string) =>
    req<ConnectorSaveResult>(`/api/connectors/${id}/check`, { method: "POST" }),
  disconnect: (id: string) =>
    req<{ removed: boolean; affected: ConnectorUse[]; connector: Connector }>(`/api/connectors/${id}`, {
      method: "DELETE",
    }),
  previewConnectors: (body: { idea: string; use: string[]; skip: string[] }) =>
    req<ConnectorPreview>("/api/connectors/preview", { method: "POST", body: JSON.stringify(body) }),

  // ── A build's connectors: at its question, or from the project ──
  getIntegrations: (id: string) => req<IntegrationsState>(`/api/projects/${id}/integrations`),
  saveIntegration: (
    id: string,
    iid: string,
    values: Record<string, string>,
    opts: { confirm_live?: boolean; save_to_account?: boolean } = {},
  ) =>
    req<ConnectorSaveResult & { state: IntegrationsState }>(`/api/projects/${id}/integrations/${iid}`, {
      method: "PUT",
      body: JSON.stringify({ values, confirm_live: !!opts.confirm_live, save_to_account: opts.save_to_account ?? true }),
    }),
  removeIntegrationKey: (id: string, iid: string) =>
    req<IntegrationsState>(`/api/projects/${id}/integrations/${iid}`, { method: "DELETE" }),
  integrationLater: (id: string, iid: string) =>
    req<IntegrationsState>(`/api/projects/${id}/integrations/${iid}/later`, { method: "POST" }),
  integrationsContinue: (id: string) =>
    req<RunResponse>(`/api/projects/${id}/integrations/continue`, { method: "POST" }),
  integrationsAllLater: (id: string) =>
    req<RunResponse>(`/api/projects/${id}/integrations/later`, { method: "POST" }),
  changeIntegrations: (id: string, body: { add?: string; remove?: string }) =>
    req<IntegrationsState>(`/api/projects/${id}/integrations`, { method: "POST", body: JSON.stringify(body) }),

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
  /** A plain-language change scoped to one element (by `data-oid`) — the model sees only it. */
  editPreviewElement: (id: string, oid: string, instruction: string) =>
    req<PreviewState>(
      `/api/projects/${id}/preview/edit`,
      { method: "POST", body: JSON.stringify({ oid, instruction }) },
      LLM_TIMEOUT_MS
    ),
  /** Direct edits — text, classes, links, images. No model call. */
  patchPreview: (id: string, ops: PatchOp[], summary?: string) =>
    req<PreviewState>(`/api/projects/${id}/preview/patch`, {
      method: "POST",
      body: JSON.stringify({ ops, summary }),
    }),
  /** Site style: fonts, palette and shape for every page at once. No model call. */
  themePreview: (id: string, changes: Partial<ThemeTokens>) =>
    req<PreviewState>(`/api/projects/${id}/preview/theme`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    }),
  undoPreview: (id: string) =>
    req<PreviewState>(`/api/projects/${id}/preview/undo`, { method: "POST" }),
  redoPreview: (id: string) =>
    req<PreviewState>(`/api/projects/${id}/preview/redo`, { method: "POST" }),
  getPreviewRevision: (id: string, revisionId: string) =>
    req<{ id: string; html: string }>(`/api/projects/${id}/preview/revisions/${revisionId}`),

  // ── Settings: cloud API keys + local model sources ──
  getProviders: () =>
    req<{
      providers: Record<string, ProviderSetting>;
      default_mode: string;
      store_error: string | null;
    }>("/api/settings/providers"),
  /** Save a key and/or model. The backend checks it first — a key the provider
   * rejects is not saved (`applied: false`) and never replaces one that works. */
  setProviderKey: (
    provider: string,
    body: { api_key?: string | null; default_model?: string }
  ) =>
    req<{ applied: boolean; check: KeyCheck | null; provider: ProviderSetting }>(
      `/api/settings/providers/${provider}`,
      { method: "PUT", body: JSON.stringify(body) },
      KEY_CHECK_TIMEOUT_MS
    ),
  /** Check the saved key again, now (a free request, then a one-token one). */
  recheckProviderKey: (provider: string) =>
    req<{ check: KeyCheck; provider: ProviderSetting }>(
      `/api/settings/providers/${provider}/check`,
      { method: "POST" },
      KEY_CHECK_TIMEOUT_MS
    ),
  removeProviderKey: (provider: string) =>
    req<{ provider: ProviderSetting }>(`/api/settings/providers/${provider}`, {
      method: "DELETE",
    }),
  /** Every model source and what it serves. `refresh` probes loopback again. */
  getLocalModel: (refresh = false) =>
    req<LocalStatus>(`/api/settings/local${refresh ? "?refresh=true" : ""}`),
  /** Make `source:model` the model every agent falls back to. */
  setLocalModel: (spec: string) =>
    req<LocalStatus>("/api/settings/local/default", {
      method: "PUT",
      body: JSON.stringify({ model: spec }),
    }),
  /** Add a source by address. It must answer, so its runtime can be identified. */
  addSource: (body: {
    base_url: string;
    label?: string;
    api_key?: string;
    confirm_remote?: boolean;
  }) =>
    req<LocalStatus>("/api/settings/sources", { method: "POST", body: JSON.stringify(body) }),
  /** `""` clears the key, a value sets it. */
  setSourceKey: (id: string, api_key: string) =>
    req<LocalStatus>(`/api/settings/sources/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ api_key }),
    }),
  removeSource: (id: string) =>
    req<LocalStatus>(`/api/settings/sources/${encodeURIComponent(id)}`, { method: "DELETE" }),

  // ── Settings: will each model run, and how it is tuned ──
  /** Every model on every answering source, checked. The first ask describes each. */
  getCompatibility: () => req<Compatibility>("/api/settings/compatibility", {}, LLM_TIMEOUT_MS),
  /** What builds the generated code for real (#75): Docker, a builder service, or nothing. */
  getBuildRunner: () => req<BuildRunnerStatus>("/api/settings/build-runner"),
  getModelGeneration: (spec: string) =>
    req<ModelGeneration>(`/api/settings/models/generation?spec=${encodeURIComponent(spec)}`),
  /** Replaces what is saved for this model; a field left out goes back to its default. */
  setModelGeneration: (spec: string, values: GenerationValues) =>
    req<ModelGeneration>("/api/settings/models/generation", {
      method: "PUT",
      body: JSON.stringify({ spec, values }),
    }),
  resetModelGeneration: (spec: string) =>
    req<ModelGeneration>(`/api/settings/models/generation?spec=${encodeURIComponent(spec)}`, {
      method: "DELETE",
    }),

  // ── Settings: which model each agent runs on ──
  getRoles: () => req<RoleSettings>("/api/settings/roles"),
  /** `null` puts the role back on the default model. */
  setRoleModel: (role: string, model: string | null) =>
    req<RoleSettings>(`/api/settings/roles/${role}`, {
      method: "PUT",
      body: JSON.stringify({ model }),
    }),
  /** How many files a code phase writes per call: a count, "all", or `null` for automatic. */
  setFilesPerCall: (role: string, value: number | "all" | null) =>
    req<RoleSettings>(`/api/settings/roles/${role}/files-per-call`, {
      method: "PUT",
      body: JSON.stringify({ value }),
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
  waiveFinding: (id: string, key: string, reason: string, kind?: WaiveKind) =>
    req<{ key: string; status: string; note: string; unresolved: number }>(
      `/api/projects/${id}/security/${key}/waive`,
      { method: "POST", body: JSON.stringify({ reason, kind }) },
    ),

  // ── When the crew asked for help ──
  keepTrying: (id: string, rounds?: number) =>
    req<RunResponse>(`/api/projects/${id}/auto-fix/retry`, {
      method: "POST",
      body: JSON.stringify(rounds ? { rounds } : {}),
    }),
  acceptProblems: (id: string, kind: WaiveKind, reason: string) =>
    req<RunResponse>(`/api/projects/${id}/auto-fix/accept`, {
      method: "POST",
      body: JSON.stringify({ kind, reason }),
    }),

  // ── GitHub publishing (OAuth "Connect" → push to the user's own account) ──
  githubStatus: () => req<GithubStatus>("/api/github/status"),
  // Full-page redirect into GitHub's login; returns here with ?github=connected.
  githubConnectUrl: (returnTo: string) =>
    `${BASE}/api/github/oauth/start?return_to=${encodeURIComponent(returnTo)}`,
  githubDisconnect: () =>
    req<{ connected: boolean }>("/api/github/disconnect", { method: "POST" }),
  pushToGithub: (
    id: string,
    body: { name?: string; private?: boolean; description?: string; use_existing?: boolean }
  ) =>
    req<GithubPushResult>(
      `/api/github/push/${id}`,
      { method: "POST", body: JSON.stringify(body) },
      LLM_TIMEOUT_MS
    ),

  // ── Deploying a finished build (always into the user's own accounts) ──
  shipInfo: (id: string) => req<ShipInfo>(`/api/projects/${id}/ship`),
  deployConnections: () =>
    req<{ github: GithubStatus; vercel: VercelConnection }>("/api/deploy/connections"),
  saveVercelToken: (token: string) =>
    req<VercelTokenResult>(
      "/api/deploy/vercel/token",
      { method: "PUT", body: JSON.stringify({ token }) },
      KEY_CHECK_TIMEOUT_MS
    ),
  removeVercelToken: () =>
    req<{ vercel: VercelConnection }>("/api/deploy/vercel/token", { method: "DELETE" }),
  deploy: (id: string, body: { name?: string; private?: boolean } = {}) =>
    req<DeployStart>(
      `/api/projects/${id}/deploy`,
      { method: "POST", body: JSON.stringify(body) },
      LLM_TIMEOUT_MS
    ),
  deployState: (id: string) => req<DeployState>(`/api/projects/${id}/deploy`),
  setLiveUrl: (id: string, url: string) =>
    req<DeployState>(`/api/projects/${id}/deploy/url`, {
      method: "PUT",
      body: JSON.stringify({ url }),
    }),
  // Streams NDJSON download progress from one source; calls onLine per object.
  // Only sources whose runtime has a download API (`can_download`) accept it.
  pullLocalModel: async (
    sourceId: string,
    model: string,
    onLine: (line: PullProgress) => void
  ): Promise<void> => {
    const res = await fetch(`${BASE}/api/settings/sources/${encodeURIComponent(sourceId)}/pull`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
      credentials: "include",
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
  /** `match_text` is what the selector matches every build's phase on, verbatim. */
  phases: { key: string; label: string; match_text: string }[];
  /** Whether this account may change the library: the install owner only. */
  can_edit: boolean;
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
  max_per_phase: number;
  phases: {
    phase: string;
    label: string;
    skills: SkillPick[];
    /** Matched this phase, and left out because the cap was already full. */
    over_cap: SkillPick[];
  }[];
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
  /** "revoked" when GitHub turned the saved token down and it was forgotten. */
  reason?: "revoked" | null;
};

export type GithubPushResult = {
  html_url: string;
  full_name: string;
  branch: string;
  private: boolean;
  files: number;
  commit?: string;
  /** A new repository was made (the first push). */
  created?: boolean;
  /** False when the repository already had exactly this build — no commit made. */
  changed?: boolean;
};

export type VercelConnection = {
  connected: boolean;
  username: string | null;
  /** `…last4` — the token itself never reaches the page. */
  hint: string | null;
  checked_at: string | null;
};

export type VercelTokenResult = {
  applied: boolean;
  reason: "ok" | "rejected" | "unreachable";
  message: string;
  vercel: VercelConnection;
};

export type DeployStatus =
  | "queued"
  | "uploading"
  | "building"
  | "ready"
  | "error"
  | "handed_off"
  /** Vercel failed the build and the crew is fixing it (#75). */
  | "fixing"
  /** The crew's fix is done: deploy again. */
  | "fixed";

/** The crew's fix of a failed Vercel build (#75). */
export type DeployFix = {
  /** fixing · review (fixed, waiting at a review) · stuck (asked for help) · fixed · stopped */
  state: "fixing" | "review" | "stuck" | "fixed" | "stopped";
  round: number;
  of: number;
  problems: number;
  attempt: number;
  /** The rebuilt frontend was itself built for real — not only parsed. */
  verified?: boolean;
};

export type DeployState = {
  target: "vercel" | "render" | null;
  status: DeployStatus | null;
  url: string | null;
  error: string | null;
  deployed_at: string | null;
  /** The last lines of a failed build's log, scrubbed. */
  log: string[];
  /** Set while — and after — the crew fixes what Vercel rejected. */
  fix?: DeployFix | null;
};

export type ShipInfo = {
  kind: "frontend" | "fullstack" | "backend" | null;
  target: "vercel" | "render" | null;
  /** "react + fastapi + postgres" */
  stack: string;
  frontend: string | null;
  /** Complete and has code: deploying is allowed. */
  ready: boolean;
  render: { asks_for: string[]; free_postgres: boolean; has_frontend: boolean } | null;
  github_repo: string | null;
  github_branch: string | null;
  github_pushed_at: string | null;
  deploy: DeployState;
  connections: { github: GithubStatus; vercel: VercelConnection };
};

export type DeployStart = {
  target: "vercel" | "render";
  deploy: DeployState;
  handoff_url?: string;
  push?: GithubPushResult;
};

export type RouterStatus = {
  default_mode: string;
  /** Cloud providers and local sources alike, keyed by id. */
  providers: Record<
    string,
    { available: boolean; is_local: boolean; default_model: string | null; label: string }
  >;
  fallback_chain: string[];
};

/** What a key check found. `message` is the backend's own sentence — never the
 * provider's text, which can quote the key. */
export type KeyStatus =
  | "valid"
  | "rate_limited"
  | "invalid"
  | "model_unavailable"
  | "billing"
  | "unverified"
  | "unchecked"
  | "locked"
  | "none";

export type KeyCheck = {
  status: KeyStatus;
  reason: string;
  message: string;
  checked_at: string;
  model: string | null;
  models: string[];
  context_tokens: number | null;
  advice?: KeyAdvice | null;
};

export type ProviderSetting = {
  configured: boolean;
  /** Set, and not rejected by its last check — what routing will use. */
  available: boolean;
  key_hint: string | null;
  default_model: string | null;
  status: KeyStatus;
  reason: string;
  message: string;
  checked_at: string | null;
  checked_model: string | null;
  models: string[];
  /** A build's own call was refused, rather than a check. */
  during_build?: boolean;
  advice?: KeyAdvice | null;
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
  /** none · toggle · levels · always — how its thinking is set. */
  thinking?: string | null;
  /** The thinking setting calls carry, fitted to what it takes. */
  thinking_level?: string | null;
  /** Tokens kept for reasoning on top of the reply. */
  reasoning_tokens?: number;
  warnings: string[];
};

/** One model a source serves. Everything is keyed by `spec`: `source:model`. */
export type SourceModel = {
  spec: string;
  name: string;
  /** chat · embedding · vision · base — or null when the runtime didn't say. */
  kind: string | null;
  /** The runtime's own words, quoted; null when it reported none. */
  capabilities: string[] | null;
  /** False for a model the runtime sends to a hosted service to run. */
  is_local: boolean;
  can_build: boolean;
};

/** Runtime hygiene: judged by the server from the version and listen addresses. */
export type RuntimeWarning = {
  kind: "outdated" | "exposed";
  title: string;
  detail: string;
  /** The advisory, for an outdated runtime. */
  url?: string;
  ids?: string[];
  fixed?: string;
};

/** Somewhere local models come from: a runtime found, configured, or added. */
export type LocalSource = {
  id: string;
  label: string;
  /** The runtime's id in the adapter table; null until something answers. */
  runtime: string | null;
  runtime_label: string | null;
  base_url: string;
  origin: "detected" | "configured" | "added";
  /** Not on this machine. Prompts sent here leave it. */
  remote: boolean;
  same_machine: boolean;
  reachable: boolean;
  error: string | null;
  version: string | null;
  /** Older than a known security fix, or reachable from the network. Never blocks. */
  warnings: RuntimeWarning[];
  models: SourceModel[];
  /** Whether this runtime downloads models through its API. */
  can_download: boolean;
  /** How to get another model onto it, when it can't be done from here. */
  add_model: string;
  library: string | null;
  home: string | null;
  key_hint: string | null;
  removable: boolean;
};

/** Something on loopback that answered but isn't a runtime this app recognises. */
export type UnknownEndpoint = {
  base_url: string;
  /** It does at least answer the OpenAI API. */
  openai: boolean;
  note: string;
};

export type LocalStatus = {
  sources: LocalSource[];
  unknown: UnknownEndpoint[];
  /** Every address a runtime was looked for at. */
  tried: string[];
  /** Any source answering at all. */
  reachable: boolean;
  /** `source:model`, or null when nothing can be resolved. */
  default_model: string | null;
  /** chosen (in Settings) · configured (in .env) · detected (first model that writes). */
  default_origin: "chosen" | "configured" | "detected" | null;
  has_default: boolean;
  /** Null while the default's source is down or doesn't have it. */
  profile: ModelProfile | null;
  /** Every model on every answering source, as specs. */
  models: string[];
  /**
   * What each model says it can do, in the runtime's words. Keyed by spec.
   *
   * A model is a key here only when the runtime answered about it. One that is
   * **absent is unknown, not incapable**, and readers have to keep it: an older
   * runtime reports no capabilities at all, and a rule that hid everything it
   * serves would empty the picker on exactly the setups least able to say why.
   */
  model_capabilities: ModelCapabilities;
  /**
   * Models the runtime says cannot complete text, so cannot run a build. Decided by
   * the backend and read as-is — see `lib/capabilities.ts`. May include the default
   * under the name it is configured by.
   */
  cannot_build: string[];
  /** Models whose name suggests they were trained for code — a hint, never a default. */
  code_models: string[];
  /** Models a runtime reports as embedding-only: what memory and search can use. */
  embedding_models: string[];
  /** What memory and document search embed with right now, or null (they're off). */
  embedding_model: string | null;
  embedding_origin: "chosen" | "configured" | "detected" | null;
  /** What "Automatic" would embed with — differs from the above once one is chosen. */
  embedding_automatic: string | null;
  embedding_automatic_origin: "configured" | "detected" | null;
};

/** The server's answer to "would this build start?", asked before one is created. */
export type Preflight = {
  ok: boolean;
  /** One sentence, ready to show. Null when the build can start. */
  reason: string | null;
  /** True when no local runtime the build needs is answering. */
  unreachable: boolean;
  /** The pre-Start check of every local model this build would use. */
  checks?: PreflightCheck[];
};

/** fits · degraded (runs, with a cost) · blocked (won't run) · unknown (not assessed). */
export type CheckLevel = "fits" | "degraded" | "blocked" | "unknown";

/** One finding; `note` informs and never changes the verdict. */
export type CheckReason = { level: CheckLevel | "note"; text: string };

/** Will this model run a build here — decided by the backend, read as-is. */
export type ModelCheck = {
  spec: string;
  level: CheckLevel;
  /** The finding that set the verdict, as one sentence. */
  summary: string;
  reasons: CheckReason[];
  /** What to pick instead, when it won't run. Sizes and quantizations, never names. */
  suggestion: string | null;
  facts: {
    context_window?: number;
    parameter_size?: string | null;
    quantization?: string | null;
    weights_bytes?: number | null;
    memory_needed_bytes?: number | null;
    ram_bytes?: number | null;
    experts_total?: number | null;
    experts_active?: number | null;
    thinking?: string | null;
    thinking_level?: string | null;
    reasoning_tokens?: number;
    kv_cache_type?: string;
    /** False when the machine's memory couldn't be weighed against the model. */
    memory_checked?: boolean;
  };
};

export type PreflightCheck = ModelCheck & {
  model: string;
  source_label: string;
  /** The roles whose phase runs on this model; empty means "the rest of the run". */
  roles: string[];
};

export type Compatibility = {
  /** Keyed by spec; a model missing here has not been checked (yet). */
  checks: Record<string, ModelCheck>;
  ram_bytes: number | null;
};

/** One setting the Tune panel can show — its range comes from the backend. */
export type GenerationField = {
  key: string;
  group: "sampling" | "limits" | "machine";
  label: string;
  kind: "float" | "int" | "choice" | "stops" | "duration";
  help: string;
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  choices: string[];
};

export type GenerationValues = Partial<Record<GenerationField["group"], Record<string, unknown>>>;

/** One model's generation settings, its server's defaults, and what its runtime takes. */
export type ModelGeneration = {
  spec: string;
  /** Set when a reset landed while the source was down: only `values` came back. */
  reset?: boolean;
  source: string;
  runtime: string | null;
  /** The source's own name, as its heading in Settings shows it. */
  source_label: string;
  fields: GenerationField[];
  /** What is saved for this model — only what someone set. */
  values: GenerationValues;
  /** What the running server or the model file reports, by field key. */
  defaults: Record<string, unknown>;
  /** Whether this runtime can send each field. */
  supported: Record<string, boolean>;
  /** none · toggle · levels · always — or null when the runtime doesn't say. */
  thinking: string | null;
  thinking_options: string[];
  thinking_level: string | null;
  profile: ModelProfile;
  /** What applies with nothing saved: what each "Default" and "Without one" means. */
  untuned: { context_window: number; max_output_tokens: number; thinking_level: string | null };
  /** Whether machine settings reach this runtime — only one on this same machine. */
  machine_applies: boolean;
  /** What is sent for a field left unset that the server has no default for; null
   *  means the runtime's own default applies. */
  sent_when_unset: Record<string, number | null>;
  /** This backend's configured defaults. */
  fallbacks: {
    temperature: number;
    top_p: number;
    thinking: string;
    reasoning_tokens: number;
    kv_cache_type: string;
  };
  check: ModelCheck;
};

/** `{ "src:embedder": ["embedding"] }` — see `LocalStatus.model_capabilities`. */
export type ModelCapabilities = Record<string, string[]>;

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
  /**
   * Code phases only (#81): how many files one call writes — a count, "all", or
   * null for automatic — and what automatic means for this role's model now.
   */
  files_per_call?: number | "all" | null;
  files_per_call_auto?: number | null;
};


export type RoleSettings = {
  roles: RoleRow[];
  /** What a role with no choice of its own runs on, as `source:model`. */
  default_model: string | null;
  default_origin: LocalStatus["default_origin"];
  /** Every model on every answering source, as specs. */
  local_models: string[];
  sources: { id: string; label: string; reachable: boolean }[];
  embedding_models: string[];
  embedding_model: string | null;
  embedding_origin: LocalStatus["embedding_origin"];
  embedding_automatic: string | null;
  /**
   * Pulled models whose name suggests they were trained on code. Derived by the
   * backend so there is one rule rather than two that disagree — a second copy here
   * was missing `codellama`, which the other card was already badging as code.
   */
  code_models: string[];
  /** The same view `LocalStatus` carries, decided by the same rule. */
  model_capabilities: ModelCapabilities;
  cannot_build: string[];
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
/** One change the server makes without a model, on the element with this `data-oid`. */
export type PatchOp =
  | { oid: string; kind: "text"; text: string }
  | { oid: string; kind: "classes"; add: string[]; remove: string[] }
  | { oid: string; kind: "attr"; name: string; value: string | null };

export type ThemeTokens = {
  primary: string;
  accent: string;
  tint: string;
  font_pair: string;
  radius: string;
  shadow: string;
  density: string;
};
export type PreviewTheme = {
  current: ThemeTokens;
  fonts: { id: string; label: string; display: string; body: string }[];
  tints: string[];
  radii: string[];
  shadows: string[];
  densities: string[];
  /** The ramps the design system derives — the swatches a colour control offers. */
  palette: Record<"primary" | "accent" | "neutral", Record<string, string>>;
};

export type PreviewRevision = {
  id: string;
  /** generated | edited (a model) | patched (direct edits) | themed (site style) */
  source: string;
  parent_id?: string | null;
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
  /** The revision on screen. Undo and redo move it; nothing is deleted. */
  head_id: string | null;
  can_undo: boolean;
  can_redo: boolean;
  /** Null for a single-page mockup drawn before sites — it can't be restyled in place. */
  theme: PreviewTheme | null;
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
    /** Each built phase's real build (#75): its outcome and one-line summary. */
    runs?: Record<string, { status: BuildRun["status"]; summary: string }>;
  };
};

// ── Your computers ────────────────────────────────────────────────────────────
export type OS = "macos" | "windows" | "linux";

/** One runtime card on the Setup tab — straight from the backend's adapter table. */
export type RuntimeCard = {
  id: string;
  label: string;
  home: string | null;
  library: string | null;
  port: number | null;
  generic: boolean;
  /** Missing key: not supported on that OS. */
  install: Partial<Record<OS, string>>;
  download: string;
  embeddings: string;
  serve: string;
  check: string;
  exposure: string | null;
  facts: RuntimeFacts | null;
};

/** What a runtime reports and takes — the adapter table's columns. */
export type RuntimeFacts = {
  context: string;
  /** null: only some versions or servers report it. */
  context_reported: boolean | null;
  structured: "schema" | "grammar" | "json" | "none";
  thinking: string;
  embeddings: boolean;
  listens_everywhere: boolean;
  source: string;
};

export type ConnectorInfo = {
  package: string;
  version: string;
  min_version: string;
  server: string;
  commands: Record<OS, string>;
  source_command: string;
  verify: string;
  ops: string[];
  refused: Record<string, string[]>;
};

export type SetupGuide = { runtimes: RuntimeCard[]; connector: ConnectorInfo };

export type ReportedModel = {
  name: string;
  kind: string | null;
  capabilities: string[] | null;
  is_local: boolean;
  size_bytes: number | null;
  loaded: boolean | null;
};

export type ReportedSource = {
  id: string;
  runtime: string;
  label: string;
  base_url: string;
  remote: boolean;
  version: string | null;
  reachable: boolean;
  error: string | null;
  models: ReportedModel[];
  /** Network addresses it also answers on, from connector 0.3.0; null when not checked. */
  exposed_on?: string[] | null;
};

export type DeviceHello = {
  device_id: string;
  connector_version: string;
  os: string;
  os_version: string | null;
  arch: string | null;
  ram_bytes: number | null;
  hostname: string | null;
  sources: ReportedSource[];
  unknown: { base_url: string; openai: boolean; note: string }[];
  tried: string[];
  capabilities: string[];
  /** What this computer lets a build use. Set on the computer, never here. */
  limits?: DeviceLimits | null;
  /** Model calls are paused on the computer (`aiteam-connect pause`). */
  paused?: boolean;
};

export type DeviceLimits = {
  concurrency: number;
  requests_per_minute: number;
  max_prompt_chars: number;
  max_output_tokens: number;
  timeout_seconds: number;
};

export type DeviceTestResult =
  | {
      ok: true;
      spec: string;
      seconds: number;
      answer: string;
      thought: boolean;
      prompt_tokens: number;
      completion_tokens: number;
      finish_reason: string | null;
    }
  | {
      ok: false;
      spec: string;
      seconds: number;
      /** limit | paused | cancelled | refused | runtime | unreachable, or null when the connection failed. */
      code: string | null;
      error: string;
    };

export type Device = {
  id: string;
  name: string;
  status: "pending" | "approved";
  os: string | null;
  connector_version: string | null;
  outdated: boolean;
  paired_from: string | null;
  /** null when the server sits behind a proxy and can't tell. */
  same_network: boolean | null;
  created_at: string | null;
  approved_at: string | null;
  last_seen_at: string | null;
  online: boolean;
  connected_since: string | null;
  hello: DeviceHello | null;
  chat_model: string | null;
  embed_model: string | null;
  advice: { ram_gib: number; size: string; quantization: string; note: string } | null;
  /** Source id → what's wrong with that runtime. Only sources with something to say. */
  warnings?: Record<string, RuntimeWarning[]>;
};

export type Pairing = {
  id: string;
  code: string;
  expires_at: string;
  ttl_seconds: number;
  account: string;
  connector: ConnectorInfo;
};

export type PairingState = {
  id: string;
  state: "waiting" | "claimed" | "used" | "expired";
  expires_at: string;
  device: Device | null;
};

export type DeviceModelInfo = {
  name: string;
  context_window: number | null;
  parameter_label: string | null;
  quantization: string | null;
  kind: string | null;
  structured_output: string | null;
  thinking: string | null;
  is_local: boolean;
  weights_bytes: number | null;
};

// ── the build's database ─────────────────────────────────────────────────────
/** A failed test saves nothing, so "failed" is never a status — only a check result. */
export type DatabaseStatus = "connected" | "unchecked" | "later" | "none";

export type DatabaseVar = {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
  /** uri | url | key | text */
  kind: string;
  help: string;
};

export type DatabaseContract = {
  database: string;
  provider: string;
  label: string;
  variables: DatabaseVar[];
  /** Firebase: one pasted `firebaseConfig` object instead of six fields. */
  paste_object: boolean;
};

export type DatabaseCheck = {
  status: "connected" | "unchecked" | "failed";
  message: string;
  reason: string;
  name: string | null;
  step: number | null;
  host: string;
  latency_ms: number | null;
};

export type DatabaseState = {
  needed: boolean;
  status: DatabaseStatus | null;
  database: string | null;
  database_label: string | null;
  at_gate: boolean;
  provider?: string;
  providers?: { provider: string; label: string }[];
  contract?: DatabaseContract;
  saved?: { name: string; hint: string }[];
  check?: { status: string; host: string; latency_ms: number | null; at: string } | null;
};

export type DatabaseProblem = { name: string; message: string; step: number | null };

export type DatabaseSaveResult = {
  ok: boolean;
  status: "invalid" | "connected" | "unchecked" | "failed";
  problems?: DatabaseProblem[];
  notices?: DatabaseProblem[];
  check?: DatabaseCheck;
  state: DatabaseState;
};

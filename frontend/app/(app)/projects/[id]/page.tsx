"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Artifacts, type PhaseResult, type Project, type RunResponse } from "@/lib/api";
import { PHASES, PHASE_BY_KEY } from "@/components/shell/phases";
import { AGENT_BY_KEY, type Persona } from "@/components/agents/personas";
import AgentSprite, { type SpriteState } from "@/components/agents/AgentSprite";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import Markdown from "@/components/ui/Markdown";
import { Skeleton, SkeletonLines } from "@/components/ui/Skeleton";
import VisualPreview from "@/components/preview/VisualPreview";
import CodeBlock from "@/components/preview/CodeBlock";
import GithubPublish from "@/components/github/GithubPublish";
import PhaseArtifact from "@/components/build/PhaseArtifact";
import RunControls from "@/components/build/RunControls";
import { Elapsed } from "@/components/build/Elapsed";

type Tab = "build" | "preview" | "summary";
type NodeState = "done" | "running" | "gate" | "redo" | "failed" | "pending";

// ── status → presentation ────────────────────────────────────────────────────
const STATUS_LABEL: Record<string, string> = {
  created: "Not started",
  running: "Running",
  awaiting_approval: "Waiting for you",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Stopped",
  stalled: "Stalled",
};

function badgeClass(status: string): string {
  if (status === "completed") return "badge-ok";
  if (status === "awaiting_approval") return "badge-warn";
  if (status === "running") return "badge-run";
  if (status === "failed" || status === "stalled") return "badge-bad";
  return "";
}

/** A stalled run says `running` in the database and is not running. Say the truth. */
function effectiveStatus(project: Project): string {
  return project.status === "running" && project.stalled ? "stalled" : project.status;
}

function StatusBadge({ status }: { status: string }) {
  const live = status === "running" || status === "awaiting_approval";
  const dot =
    status === "completed"
      ? "dot-ok"
      : status === "awaiting_approval"
        ? "dot-warn dot-pulse"
        : status === "running"
          ? "dot-run dot-pulse"
          : status === "failed" || status === "stalled"
            ? "dot-bad"
            : "";
  return (
    <span className={"badge " + badgeClass(status)} aria-live={live ? "polite" : undefined}>
      <span className={"dot " + dot} aria-hidden="true" />
      {STATUS_LABEL[status] || status.replace(/_/g, " ")}
    </span>
  );
}

// Latest row produced for a phase (phases re-run when rejected).
function latestRow(project: Project, key: string): PhaseResult | undefined {
  const rows = project.phases.filter((p) => p.phase === key);
  if (rows.length === 0) return undefined;
  return [...rows].sort((a, b) => +new Date(a.created_at) - +new Date(b.created_at))[rows.length - 1];
}

/**
 * What a phase is doing, read from the phase's own row first.
 *
 * This used to be inferred from the *project* status plus `current_phase`, and both
 * of those only moved once an agent had finished — so a phase mid-generation was
 * indistinguishable from one that had never started. A row now exists from the moment
 * generation begins, and it carries its own status, which makes this a lookup instead
 * of a guess.
 */
function nodeStateFor(project: Project, key: string): NodeState {
  const row = latestRow(project, key);
  if (!row) return "pending";
  if (row.status === "running") return "running";
  if (row.status === "approved") return "done";
  if (row.status === "rejected") return "redo";
  if (row.status === "failed") return "failed";
  if (row.status === "pending_approval") {
    const waiting =
      project.status === "awaiting_approval" && project.current_phase === key;
    return waiting ? "gate" : "done";
  }
  return "done";
}

// Which of an agent's voice lines fits the state it's in. A phase waiting at a
// gate has finished its work, so it speaks its "done" line.
const VOICE_FOR: Record<NodeState, keyof Persona["lines"]> = {
  pending: "queued",
  running: "working",
  gate: "done",
  done: "done",
  redo: "rejected",
  failed: "rejected",
};

// The build view and the sprite share one idea of what an agent is doing.
const SPRITE_STATE: Record<NodeState, SpriteState> = {
  done: "done",
  running: "working",
  gate: "gate",
  redo: "rejected",
  failed: "rejected",
  pending: "queued",
};

const NODE_STATUS: Record<NodeState, string> = {
  pending: "Queued",
  running: "Running",
  redo: "Rejected — re-running",
  failed: "Stopped mid-phase",
  gate: "Needs your approval",
  done: "Done",
};

function localPct(project: Project): number | null {
  const withProvider = project.phases.filter((p) => p.provider_used);
  if (withProvider.length === 0) return null;
  const local = withProvider.filter((p) => /ollama|local/i.test(p.provider_used || "")).length;
  return Math.round((local / withProvider.length) * 100);
}

// Cost is only meaningful once something has actually cost money.
function formatCost(usd: unknown): string {
  const n = Number(usd || 0);
  if (!n) return "Free";
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

// ── page ─────────────────────────────────────────────────────────────────────
export default function ProjectPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const [project, setProject] = useState<Project | null>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("build");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const p = await api.getProject(id);
      setProject(p);
      setAnalytics(await api.analytics(id));
      setError("");
    } catch (e: any) {
      setError(e.message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  // Returning from the GitHub OAuth round-trip? Land on Summary where the
  // publish controls live (GithubPublish reads the ?github= param itself).
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("github")) setTab("summary");
  }, []);

  // Poll while anything can still change under us — at a cadence matched to how
  // fast it can change.
  //
  // A live `running` build is the only thing worth a tight loop. A *stalled* one is
  // not running at all, and hammering it every 2.5s forever is precisely the old
  // behaviour this issue is about. `awaiting_approval` looks static but isn't:
  // another tab can approve, stop or reject it, and a tab showing a gate that no
  // longer exists is how one click's worth of intent used to advance two phases.
  const pollMs = !project
    ? 0
    : project.status === "running" && !project.stalled
      ? 2500
      : project.status === "running" || project.status === "awaiting_approval"
        ? 10000
        : 0;

  useEffect(() => {
    if (pollMs > 0) {
      pollRef.current = setInterval(load, pollMs);
    } else if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [pollMs, load]);

  const act = useCallback(
    async (fn: () => Promise<RunResponse | unknown>) => {
      setBusy(true);
      setError("");
      try {
        const result = (await fn()) as RunResponse | undefined;
        // Control endpoints return the status they just committed. Applying it
        // before the reload means the badge flips the instant the click lands,
        // instead of reading "Waiting for you" while an agent is generating.
        if (result && typeof result.status === "string") {
          setProject((p) => (p ? { ...p, status: result.status } : p));
        }
      } catch (e: any) {
        setError(e.message);
      } finally {
        await load();
        setBusy(false);
      }
    },
    [load],
  );

  const title = project ? project.name || project.idea : undefined;
  const status = project ? effectiveStatus(project) : undefined;
  useChrome(
    project ? { title, badge: <StatusBadge status={status!} /> } : { sub: "Build" },
    [title, status],
  );

  if (!project) {
    return (
      <div className="build-wrap">
        {error ? (
          <div className="notice notice-bad" role="alert">
            {Icon.alert}
            <div className="notice-body">
              <span className="notice-title">Couldn&apos;t load this build</span>
              <span className="notice-text">{error}</span>
            </div>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
            <Skeleton h={28} w="45%" r={8} />
            <Skeleton h={92} r={13} />
            <Skeleton h={260} r={13} />
          </div>
        )}
      </div>
    );
  }

  const doneCount = PHASES.filter((ph) => nodeStateFor(project, ph.key) === "done").length;
  const completed = project.status === "completed";
  const tabs: { key: Tab; label: string }[] = [
    { key: "build", label: "Build" },
    { key: "preview", label: "Preview" },
    { key: "summary", label: "Deliver" },
  ];

  // Arrow keys move between tabs and focus follows selection, per the ARIA
  // tabs pattern the roles above promise.
  function onTabKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(e.key)) return;
    e.preventDefault();
    const at = tabs.findIndex((t) => t.key === tab);
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? tabs.length - 1
          : (at + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    setTab(tabs[next].key);
    document.getElementById(`tab-${tabs[next].key}`)?.focus();
  }

  return (
    <div className="build-wrap">
      <div className="build-head">
        <div style={{ minWidth: 0 }}>
          <h1>{project.name || project.idea}</h1>
          <div className="build-meta">
            <span className="badge">
              {project.routing_mode === "local_only" ? "Local models" : project.routing_mode}
            </span>
            <span className="badge">
              {project.require_approval ? "Approval gated" : "Runs unattended"}
            </span>
          </div>
        </div>
        <div className="build-actions">
          {completed && (
            <>
              <button className="btn" onClick={() => setTab("summary")}>
                {Icon.github} Publish
              </button>
              <a className="btn btn-primary" href={api.downloadUrl(id)} download>
                {Icon.download} Download .zip
              </a>
            </>
          )}
          <RunControls project={project} busy={busy} act={act} />
        </div>
      </div>

      {/* The relay: who has the work, who is next. */}
      <div className="card" style={{ marginTop: 20 }}>
        <div className="sec-head">
          <h2 className="label">Relay</h2>
          <span className="rule" />
          <span className="label mono">{doneCount}/8</span>
        </div>
        <ol
          className="relay"
          aria-label={`Pipeline progress: ${doneCount} of 8 phases complete`}
          style={{ listStyle: "none", margin: 0, padding: "4px 0 8px" }}
        >
          {PHASES.map((ph) => {
            const ns = nodeStateFor(project, ph.key);
            const agent = AGENT_BY_KEY[ph.key];
            const live = ns === "running" || ns === "gate";
            return (
              <li key={ph.key} style={{ display: "flex", flex: "1 1 auto", minWidth: 0 }}>
                <button
                  className={`relay-step ${ns}`}
                  style={{ ["--agent" as string]: agent.accent }}
                  aria-current={live ? "step" : undefined}
                  onClick={() => setTab("build")}
                  title={`${agent.codename} · ${agent.role} — ${NODE_STATUS[ns]}`}
                >
                  <AgentSprite agent={agent} size={36} state={SPRITE_STATE[ns]} />
                  <span className="relay-name">{agent.codename}</span>
                  <span className="relay-bar" />
                </button>
              </li>
            );
          })}
        </ol>
      </div>

      {error && (
        <div className="notice notice-bad" role="alert" style={{ marginTop: 16 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Something went wrong</span>
            <span className="notice-text">{error}</span>
          </div>
        </div>
      )}

      <div
        className="tabs"
        role="tablist"
        aria-label="Build views"
        style={{ marginTop: 22 }}
        onKeyDown={onTabKeyDown}
      >
        {tabs.map((t) => (
          <button
            key={t.key}
            id={`tab-${t.key}`}
            role="tab"
            className="tab"
            aria-selected={tab === t.key}
            aria-controls={`panel-${t.key}`}
            // Roving tabindex: one stop for the whole strip, arrows move within it.
            tabIndex={tab === t.key ? 0 : -1}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div
        id={`panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`tab-${tab}`}
        tabIndex={0}
        style={{ marginTop: 20 }}
      >
        {tab === "build" && (
          <BuildTab project={project} analytics={analytics} busy={busy} act={act} id={id} />
        )}
        {tab === "preview" && <PreviewTab id={id} />}
        {tab === "summary" && <SummaryTab id={id} analytics={analytics} />}
      </div>
    </div>
  );
}

// ── Build tab ────────────────────────────────────────────────────────────────
/**
 * Why a run stopped, and the way out of it.
 *
 * Three different dead ends used to look the same — or worse, look like progress.
 * A stalled run reported "Running" forever; a cancelled one had no representation at
 * all. Each of these names what happened and puts the recovery in the same box.
 */
function RunInterrupted({
  project,
  busy,
  act,
  id,
}: {
  project: Project;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
  id: string;
}) {
  const state = effectiveStatus(project);
  const copy: Record<string, { title: string; text: string; action: string }> = {
    stalled: {
      title: "This build stopped responding",
      text:
        "It is still marked as running, but nothing has reported progress in a while — " +
        "usually the backend restarted mid-phase. Resuming re-runs the interrupted phase " +
        "from the last checkpoint; everything already approved is kept.",
      action: "Resume from checkpoint",
    },
    cancelled: {
      title: "You stopped this build",
      text:
        "Every phase generated before the stop is kept. Resuming picks up from the last " +
        "approved phase.",
      action: "Resume",
    },
    failed: {
      title: "This build stopped after a model error",
      text:
        "The most common cause is the local runtime being unavailable. Check that Ollama is " +
        "running and the model is downloaded, then pick it back up from where it left off.",
      action: "Resume from checkpoint",
    },
  };
  const { title, text, action } = copy[state] ?? copy.failed;

  return (
    <div className={"notice " + (state === "cancelled" ? "notice-warn" : "notice-bad")}>
      {Icon.alert}
      <div className="notice-body">
        <span className="notice-title">{title}</span>
        <span className="notice-text">{text}</span>
        {project.last_error && state !== "cancelled" && (
          <span className="notice-detail mono">{project.last_error}</span>
        )}
        <div className="notice-actions">
          <button
            className="btn btn-sm btn-primary"
            disabled={busy}
            onClick={() => act(() => api.resume(id))}
          >
            {busy && <span className="btn-spinner" aria-hidden="true" />}
            {Icon.play} {action}
          </button>
          {state === "failed" && (
            <a className="btn btn-sm" href="/settings">
              Check runtime
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Who has the work, right now, and for how long.
 *
 * The single most useful thing the build view can say while a model generates — and
 * for eight phases it said nothing at all, because `current_phase` was only written
 * after an agent finished.
 */
function NowWorking({ project }: { project: Project }) {
  const key = project.current_phase;
  const row = key ? latestRow(project, key) : undefined;
  if (!key || row?.status !== "running") return null;

  const agent = AGENT_BY_KEY[key];
  const meta = PHASE_BY_KEY[key];
  if (!agent) return null;

  return (
    <div
      className="working"
      style={{ ["--agent" as string]: agent.accent }}
      aria-live="polite"
      aria-atomic="true"
    >
      <AgentSprite agent={agent} size={40} state="working" />
      <div className="working-body">
        <div className="working-line">
          <b className="agent-line-name">{agent.codename}</b>
          <span className="working-verb">{agent.lines.working.toLowerCase()}</span>
          {meta && <span className="phase-deliver">{meta.deliver}</span>}
        </div>
        <div className="working-bar" aria-hidden="true">
          <span />
        </div>
      </div>
      <Elapsed startIso={row.started_at ?? project.phase_started_at} live />
    </div>
  );
}

function BuildTab({
  project,
  analytics,
  busy,
  act,
  id,
}: {
  project: Project;
  analytics: any;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
  id: string;
}) {
  const pct = localPct(project);
  const doneCount = PHASES.filter((ph) => nodeStateFor(project, ph.key) === "done").length;
  const state = effectiveStatus(project);
  const interrupted = state === "failed" || state === "cancelled" || state === "stalled";

  if (state === "created") {
    return (
      <div className="card empty">
        <h3>Ready when you are</h3>
        <p>
          Eight specialist agents will take this idea from requirements to a deployment plan.
          {project.require_approval
            ? " After each phase the pipeline stops and hands you what the agent produced — the files, the diagram, the data — so you can read it and decide."
            : " It will run straight through all eight phases without stopping."}
        </p>
        <button className="btn btn-primary" disabled={busy} onClick={() => act(() => api.run(id))}>
          {busy && <span className="btn-spinner" aria-hidden="true" />}
          {busy ? "Starting…" : "Run the pipeline"}
          {!busy && Icon.arrowRight}
        </button>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {interrupted && <RunInterrupted project={project} busy={busy} act={act} id={id} />}

      <NowWorking project={project} />

      <div className="card" style={{ padding: "16px 20px" }}>
        <div className="meter">
          <div className="meter-row">
            <span className="stat-l">Phases</span>
            <span className="meter-v">{doneCount}/8</span>
          </div>
          <div className="meter-row">
            <span className="stat-l">Tokens</span>
            <span className="meter-v">
              {analytics ? Number(analytics.total_tokens || 0).toLocaleString() : "—"}
            </span>
          </div>
          <div className="meter-row">
            <span className="stat-l">Cost</span>
            <span className="meter-v">{analytics ? formatCost(analytics.total_cost_usd) : "—"}</span>
          </div>
          <div className="meter-row">
            <span className="stat-l">Run locally</span>
            <span className="meter-v">{pct === null ? "—" : `${pct}%`}</span>
          </div>
        </div>
      </div>

      <div className="card card-flush">
        <PhaseList project={project} busy={busy} act={act} id={id} />
      </div>

      {state === "completed" && analytics && (
        <div className="card">
          <div className="sec-head">
            <h2 className="label">Analytics</h2>
            <span className="rule" />
          </div>
          <div className="stat-grid">
            <Stat v={analytics.calls ?? 0} l="Model calls" />
            <Stat v={Number(analytics.total_tokens || 0).toLocaleString()} l="Tokens" />
            <Stat v={formatCost(analytics.total_cost_usd)} l="Estimated cost" />
            <Stat
              v={`${((Number(analytics.avg_latency_ms) || 0) / 1000).toFixed(1)}s`}
              l="Avg per phase"
            />
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The phase list. Every phase that produced something is a disclosure over the
 * agent's full deliverable — the files, the diagram, the structured data, not just
 * the prose summary — and the approval gate leads with exactly that, because being
 * asked to approve work you can't read is the one thing this screen must never do.
 */
function PhaseList({
  project,
  busy,
  act,
  id,
}: {
  project: Project;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
  id: string;
}) {
  // The phase waiting on a decision is open by default; everything else starts closed.
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [feedback, setFeedback] = useState("");

  return (
    <div className="phases">
      {PHASES.map((ph) => {
        const ns = nodeStateFor(project, ph.key);
        const row = latestRow(project, ph.key);
        const isGate = ns === "gate";
        const hasDoc = Boolean(row && row.status !== "running" && (row.content_md || row.output));
        const isOpen = open[ph.key] ?? isGate;
        const agent = AGENT_BY_KEY[ph.key];

        return (
          <div
            key={ph.key}
            className={`phase ${ns}` + (isOpen && hasDoc ? " open" : "")}
            style={{ ["--agent" as string]: agent.accent }}
          >
            <button
              className="phase-sum"
              disabled={!hasDoc}
              aria-expanded={hasDoc ? isOpen : undefined}
              onClick={() => hasDoc && setOpen((o) => ({ ...o, [ph.key]: !isOpen }))}
            >
              <AgentSprite agent={agent} size={40} state={SPRITE_STATE[ns]} />

              <span className="phase-main">
                <span className="phase-line agent-line">
                  <span className="phase-n">{ph.n}</span>
                  <span className="agent-line-name">{agent.codename}</span>
                  <span className="phase-role">{agent.role}</span>
                  {ph.debate && <span className="badge badge-run">Debated</span>}
                  {isGate && <span className="badge badge-warn">Needs your approval</span>}
                  {ns === "failed" && <span className="badge badge-bad">Interrupted</span>}
                </span>
                {/* The agent's own status line, in their voice. */}
                <span className={"agent-say" + (ns === "running" ? " live" : "")}>
                  {agent.lines[VOICE_FOR[ns]]}
                  {hasDoc && (
                    <span className="phase-deliver" style={{ marginLeft: 8 }}>
                      {ph.deliver}
                    </span>
                  )}
                </span>
                {ns === "running" && (
                  <span className="phase-progress" aria-hidden="true">
                    <span />
                  </span>
                )}
              </span>

              <span className="phase-side">
                {ns === "running" && <Elapsed startIso={row?.started_at ?? null} live />}
                {ns !== "running" && row?.total_tokens ? (
                  <span className="phase-tokens mono">
                    {row.total_tokens.toLocaleString()} tok
                  </span>
                ) : null}
                {row?.provider_used && row?.model_used && (
                  <span className="phase-model">
                    {row.provider_used}/{row.model_used}
                  </span>
                )}
                {hasDoc && (
                  <span className="phase-chev" aria-hidden="true">
                    {Icon.chevron}
                  </span>
                )}
              </span>
            </button>

            {/* The decision, with the work it is about directly above it. */}
            {isGate && row && (
              <div className="phase-body" style={{ paddingTop: 0 }}>
                <div className="gate agent-gate">
                  <div className="gate-head">
                    <AgentSprite agent={agent} size={32} state="gate" />
                    <span className="gate-head-title">
                      <b className="agent-line-name">{agent.codename}</b> hands you {ph.deliver}
                    </span>
                    <span className="rule" />
                    <span className="badge badge-warn">Waiting on you</span>
                  </div>

                  <PhaseArtifact row={row} maxHeight={520} />

                  <div className="gate-decide">
                    <div className="gate-row">
                      <button
                        className="btn btn-accent"
                        disabled={busy}
                        onClick={() => act(() => api.approve(id))}
                      >
                        {busy && <span className="btn-spinner" aria-hidden="true" />}
                        {Icon.check} Approve and continue
                      </button>
                      <span className="field-hint">
                        Phase {ph.n} of 08 — approving starts the next agent.
                      </span>
                    </div>
                    <div className="gate-reject">
                      <div className="field">
                        <label htmlFor={`fb-${ph.key}`}>
                          Or send it back with a note on what to change
                        </label>
                        <input
                          id={`fb-${ph.key}`}
                          className="input"
                          placeholder="e.g. drop the social feed and focus on the core loop"
                          value={feedback}
                          onChange={(e) => setFeedback(e.target.value)}
                        />
                      </div>
                      <button
                        className="btn btn-danger"
                        disabled={busy || !feedback.trim()}
                        onClick={() =>
                          act(() => api.reject(id, feedback)).then(() => setFeedback(""))
                        }
                      >
                        {Icon.undo} Reject and rerun
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Any finished phase can be read in full, whenever. */}
            {!isGate && hasDoc && isOpen && row && (
              <div className="phase-body">
                <PhaseArtifact row={row} maxHeight={420} />
                {row.feedback && (
                  <p className="phase-feedback">
                    <strong style={{ color: "var(--bad)" }}>
                      {row.status === "failed" ? "This phase was interrupted:" : "You sent this back:"}
                    </strong>{" "}
                    {row.feedback}
                  </p>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Preview tab ──────────────────────────────────────────────────────────────
type PreviewMode = "visual" | "code";

function PreviewTab({ id }: { id: string }) {
  const [mode, setMode] = useState<PreviewMode>("visual");
  const modes: { key: PreviewMode; label: string }[] = [
    { key: "visual", label: "Mockup" },
    { key: "code", label: "Files" },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="switcher" role="group" aria-label="Preview mode" style={{ alignSelf: "flex-start" }}>
        {modes.map((m) => (
          <button
            key={m.key}
            className="seg-btn"
            aria-pressed={mode === m.key}
            onClick={() => setMode(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>
      {mode === "visual" ? <VisualPreview id={id} /> : <CodePreview id={id} />}
    </div>
  );
}

function CodePreview({ id }: { id: string }) {
  const [art, setArt] = useState<Artifacts | null>(null);
  const [error, setError] = useState("");
  const [sel, setSel] = useState(0);

  useEffect(() => {
    api.getArtifacts(id).then(setArt).catch((e) => setError(e.message));
  }, [id]);

  if (error) {
    return (
      <div className="notice notice-bad" role="alert">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">Couldn&apos;t load the files</span>
          <span className="notice-text">{error}</span>
        </div>
      </div>
    );
  }
  if (!art) {
    return (
      <div className="card">
        <SkeletonLines lines={5} />
      </div>
    );
  }

  const items = [
    ...art.files.map((f) => ({ path: f.path, content: f.content, tag: f.language || "code" })),
    ...art.docs.map((d) => ({ path: d.path, content: d.content, tag: "md" })),
  ];

  if (items.length === 0) {
    return (
      <div className="card empty">
        <h3>No files yet</h3>
        <p>
          Generated source and documentation land here as the Backend, Frontend, QA and DevOps
          phases complete. Run the pipeline to fill it.
        </p>
      </div>
    );
  }

  const current = items[Math.min(sel, items.length - 1)];

  return (
    <div className="code-grid">
      <div className="card code-files">
        <h3 className="label" style={{ padding: "4px 10px 8px" }}>
          {items.length} files
        </h3>
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {items.map((it, i) => (
            <button
              key={it.path}
              className="filerow"
              aria-current={i === sel}
              onClick={() => setSel(i)}
              title={it.path}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{it.path}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="card card-flush">
        <div className="code-pane-head">
          <span className="mono" style={{ fontSize: "var(--t-sm)", color: "var(--ink)" }}>
            {current.path}
          </span>
          <span className="badge badge-mono">{current.tag}</span>
        </div>
        {current.tag === "md" ? (
          <div style={{ padding: "18px 20px", maxHeight: 560, overflowY: "auto" }}>
            <Markdown>{current.content}</Markdown>
          </div>
        ) : (
          <CodeBlock code={current.content} path={current.path} tag={current.tag} />
        )}
      </div>
    </div>
  );
}

// ── Deliver tab ──────────────────────────────────────────────────────────────
function SummaryTab({ id, analytics }: { id: string; analytics: any }) {
  const [art, setArt] = useState<Artifacts | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.getArtifacts(id).then(setArt).catch((e) => setError(e.message));
  }, [id]);

  if (error) {
    return (
      <div className="notice notice-bad" role="alert">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">Couldn&apos;t load the summary</span>
          <span className="notice-text">{error}</span>
        </div>
      </div>
    );
  }
  if (!art) {
    return (
      <div className="card">
        <SkeletonLines lines={4} />
      </div>
    );
  }

  const hasOutput = art.files.length > 0 || art.docs.length > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="card">
        <div className="sec-head">
          <h2 className="label">Your project</h2>
          <span className="rule" />
          {hasOutput && (
            <a className="btn btn-sm btn-primary" href={api.downloadUrl(id)} download>
              {Icon.download} Download .zip
            </a>
          )}
        </div>
        <p className="muted" style={{ margin: 0, fontSize: "var(--t-md)", lineHeight: 1.6 }}>
          {art.idea}
        </p>
        <div className="stat-grid" style={{ marginTop: 20 }}>
          <Stat v={art.files.length} l="Source files" />
          <Stat v={art.docs.length} l="Documents" />
          <Stat v={art.setup_instructions.length} l="Setup steps" />
          <Stat v={analytics ? formatCost(analytics.total_cost_usd) : "—"} l="Cost" />
        </div>
      </div>

      <GithubPublish id={id} defaultName={art.name || art.idea} disabled={!hasOutput} />

      <div className="card">
        <div className="sec-head">
          <h2 className="label">Run it on your machine</h2>
          <span className="rule" />
        </div>
        {art.setup_instructions.length > 0 ? (
          <div className="setup-list">
            {art.setup_instructions.map((s, i) => (
              <div key={i} className="setup-step">
                <span className="n">{String(i + 1).padStart(2, "0")}</span>
                <span>{s}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ margin: 0, fontSize: "var(--t-base)", lineHeight: 1.6 }}>
            Setup steps appear once the Backend and DevOps phases have run. The .zip always includes
            a generated <code>README.md</code>.
          </p>
        )}
      </div>

      <div className="card">
        <div className="sec-head">
          <h2 className="label">Deliverables</h2>
          <span className="rule" />
        </div>
        <div className="deliverables">
          {PHASES.map((ph) => (
            <span key={ph.key} className="badge badge-mono" title={ph.name}>
              {ph.deliver}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

function Stat({ v, l }: { v: ReactNode; l: string }) {
  return (
    <div className="stat">
      <span className="stat-v">{v}</span>
      <span className="stat-l">{l}</span>
    </div>
  );
}

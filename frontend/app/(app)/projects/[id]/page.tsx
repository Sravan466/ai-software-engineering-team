"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Artifacts, type PhaseResult, type Project } from "@/lib/api";
import { PHASES } from "@/components/shell/phases";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import Markdown from "@/components/ui/Markdown";
import { Skeleton, SkeletonLines } from "@/components/ui/Skeleton";
import VisualPreview from "@/components/preview/VisualPreview";
import CodeBlock from "@/components/preview/CodeBlock";
import GithubPublish from "@/components/github/GithubPublish";

type Tab = "build" | "preview" | "summary";
type NodeState = "done" | "running" | "gate" | "redo" | "pending";

// ── status → presentation ────────────────────────────────────────────────────
const STATUS_LABEL: Record<string, string> = {
  created: "Not started",
  running: "Running",
  awaiting_approval: "Waiting for you",
  completed: "Completed",
  failed: "Failed",
};

function badgeClass(status: string): string {
  if (status === "completed") return "badge-ok";
  if (status === "awaiting_approval") return "badge-warn";
  if (status === "running") return "badge-run";
  if (status === "failed") return "badge-bad";
  return "";
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
          : status === "failed"
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

function nodeStateFor(project: Project, key: string): NodeState {
  const row = latestRow(project, key);
  if (row?.status === "approved") return "done";
  if (project.current_phase === key) {
    if (project.status === "awaiting_approval") return "gate";
    if (project.status === "running") return "running";
  }
  if (row?.status === "rejected") return "redo";
  if (row) return "done";
  return "pending";
}

const NODE_STATUS: Record<NodeState, string> = {
  pending: "Queued",
  running: "Running",
  redo: "Rejected — re-running",
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

  // Poll while the pipeline is actively running.
  useEffect(() => {
    if (project?.status === "running") {
      pollRef.current = setInterval(load, 2500);
    } else if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [project?.status, load]);

  const act = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setError("");
      try {
        await fn();
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
  const status = project?.status;
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
        {completed && (
          <div className="build-actions">
            <button className="btn" onClick={() => setTab("summary")}>
              {Icon.github} Publish
            </button>
            <a className="btn btn-primary" href={api.downloadUrl(id)} download>
              {Icon.download} Download .zip
            </a>
          </div>
        )}
      </div>

      {/* pipeline tracker */}
      <div className="card" style={{ marginTop: 20 }}>
        <div className="sec-head">
          <h2 className="label">Pipeline</h2>
          <span className="rule" />
          <span className="label mono">{doneCount}/8</span>
        </div>
        <ol
          className="tracker"
          aria-label={`Pipeline progress: ${doneCount} of 8 phases complete`}
          style={{ listStyle: "none", margin: 0, padding: "0 0 4px" }}
        >
          {PHASES.map((ph) => {
            const ns = nodeStateFor(project, ph.key);
            const cls =
              ns === "done"
                ? "done"
                : ns === "gate"
                  ? "current"
                  : ns === "running"
                    ? "running"
                    : ns === "redo"
                      ? "rejected"
                      : "";
            return (
              <li key={ph.key} className={"trk " + cls}>
                <span className="trk-bar" />
                <span className="trk-label">
                  <span className="trk-n">{ph.n}</span>
                  <span className="trk-name" title={ph.label}>
                    {ph.label}
                  </span>
                  <span className="trk-mark">
                    {ns === "done" ? Icon.check : ns === "gate" ? Icon.dot : ns === "running" ? Icon.dot : ns === "redo" ? Icon.rotate : null}
                  </span>
                </span>
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

  if (project.status === "created") {
    return (
      <div className="card empty">
        <h3>Ready when you are</h3>
        <p>
          Eight specialist agents will take this idea from requirements to a deployment plan.
          {project.require_approval
            ? " After each phase the pipeline stops and shows you what the agent produced, so you can read it and decide."
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

  if (project.status === "failed") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div className="notice notice-bad">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">This build stopped after a model error</span>
            <span className="notice-text">
              The most common cause is the local runtime being unavailable. Check that Ollama is
              running and the model is downloaded, then run it again from where it left off.
            </span>
            <div className="notice-actions">
              <button
                className="btn btn-sm btn-primary"
                disabled={busy}
                onClick={() => act(() => api.run(id))}
              >
                {busy && <span className="btn-spinner" aria-hidden="true" />}
                Retry build
              </button>
              <a className="btn btn-sm" href="/settings">
                Check runtime
              </a>
            </div>
          </div>
        </div>
        <div className="card card-flush">
          <PhaseList project={project} busy={busy} act={act} id={id} />
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
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

      {project.status === "completed" && analytics && (
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
 * agent's full, rendered deliverable — and the approval gate leads with that
 * document, because being asked to approve work you can't read is the one thing
 * this screen must never do.
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
  const gateKey = project.status === "awaiting_approval" ? project.current_phase : null;
  // The phase waiting on a decision is open by default; everything else starts closed.
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [feedback, setFeedback] = useState("");

  return (
    <div className="phases">
      {PHASES.map((ph) => {
        const ns = nodeStateFor(project, ph.key);
        const row = latestRow(project, ph.key);
        const isGate = ns === "gate";
        const hasDoc = Boolean(row?.content_md);
        const isOpen = open[ph.key] ?? isGate;

        return (
          <div key={ph.key} className={`phase ${ns}` + (isOpen && hasDoc ? " open" : "")}>
            <button
              className="phase-sum"
              disabled={!hasDoc}
              aria-expanded={hasDoc ? isOpen : undefined}
              onClick={() => hasDoc && setOpen((o) => ({ ...o, [ph.key]: !isOpen }))}
            >
              <span className="phase-mark" aria-hidden="true">
                {ns === "done" ? Icon.check : ns === "redo" ? Icon.rotate : Icon.dot}
              </span>

              <span className="phase-main">
                <span className="phase-line">
                  <span className="phase-n">{ph.n}</span>
                  <span className="phase-name">{ph.name}</span>
                  <span className="phase-role">{ph.role}</span>
                  {ph.debate && <span className="badge badge-run">Debated</span>}
                  {isGate && <span className="badge badge-warn">Needs your approval</span>}
                </span>
                {hasDoc ? (
                  <span className="phase-deliver">{ph.deliver}</span>
                ) : (
                  <span className="phase-role">{NODE_STATUS[ns]}</span>
                )}
                {ns === "running" && (
                  <span className="phase-progress" aria-hidden="true">
                    <span />
                  </span>
                )}
              </span>

              <span className="phase-side">
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

            {/* The decision, with the document it is about directly above it. */}
            {isGate && row && (
              <div className="phase-body" style={{ paddingTop: 0 }}>
                <div className="gate">
                  <div className="gate-head">
                    {Icon.alert}
                    <span className="gate-head-title">Review {ph.name.toLowerCase()} output</span>
                    <span className="rule" />
                    <span className="badge badge-mono">{ph.deliver}</span>
                  </div>
                  <div className="gate-doc">
                    <Markdown>{row.content_md}</Markdown>
                  </div>
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
            {!isGate && hasDoc && isOpen && (
              <div className="phase-body">
                <div className="phase-doc">
                  <Markdown>{row!.content_md}</Markdown>
                </div>
                {row?.feedback && (
                  <p className="phase-feedback">
                    <strong style={{ color: "var(--bad)" }}>You sent this back:</strong>{" "}
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

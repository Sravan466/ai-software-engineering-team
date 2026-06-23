"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Artifacts, type PhaseResult, type Project } from "@/lib/api";
import { PHASES } from "@/components/shell/phases";
import { useChrome } from "@/components/shell/ShellChrome";
import VisualPreview from "@/components/preview/VisualPreview";
import CodeBlock from "@/components/preview/CodeBlock";
import GithubPublish from "@/components/github/GithubPublish";

type Tab = "build" | "preview" | "summary";
type NodeState = "done" | "active" | "gate" | "redo" | "pending";

// ── status → presentation ────────────────────────────────────────────────────
function topBadgeClass(status: string): string {
  if (status === "completed") return "fd-badge-ok";
  if (status === "awaiting_approval") return "fd-badge-accent";
  if (status === "running") return "fd-badge-info";
  if (status === "failed") return "fd-badge-warn";
  return "fd-badge-muted";
}

// Latest row produced for a given phase key (phases can be re-run on rejection).
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
    if (project.status === "running") return "active";
  }
  if (row?.status === "rejected") return "redo";
  if (row) return "done";
  return "pending";
}

function trackStateFor(ns: NodeState): "done" | "current" | "rejected" | "pending" {
  if (ns === "done") return "done";
  if (ns === "active" || ns === "gate") return "current";
  if (ns === "redo") return "rejected";
  return "pending";
}

function nodeStatusLabel(ns: NodeState, row?: PhaseResult): string {
  if (ns === "pending") return "queued";
  if (ns === "active") return "running…";
  if (ns === "redo") return "re-running";
  if (ns === "gate") return "awaiting approval";
  if (row?.provider_used && row?.model_used) return `${row.provider_used}/${row.model_used}`;
  return row?.model_used || "done";
}

// First meaningful line of a markdown blob, lightly de-marked and truncated.
function summarize(md: string | null | undefined, max = 170): string {
  if (!md) return "";
  const line = md.split("\n").map((s) => s.trim()).find(Boolean) || "";
  const clean = line.replace(/^#+\s*/, "").replace(/^[-*]\s*/, "");
  return clean.length > max ? clean.slice(0, max - 1) + "…" : clean;
}

function localPct(project: Project): number | null {
  const withProvider = project.phases.filter((p) => p.provider_used);
  if (withProvider.length === 0) return null;
  const local = withProvider.filter((p) => /ollama|local/i.test(p.provider_used || "")).length;
  return Math.round((local / withProvider.length) * 100);
}

// ── page ─────────────────────────────────────────────────────────────────────
export default function ProjectPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const [project, setProject] = useState<Project | null>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("build");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const p = await api.getProject(id);
      setProject(p);
      setAnalytics(await api.analytics(id));
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
    project
      ? {
          title,
          badge: (
            <span className={"fd-badge " + topBadgeClass(status!)}>{status!.replace(/_/g, " ")}</span>
          ),
        }
      : { sub: "build" },
    [title, status],
  );

  if (!project) {
    return (
      <div className="build-wrap">
        <p className="fd-dim" style={{ fontSize: 14 }}>
          {error || "Loading…"}
        </p>
      </div>
    );
  }

  const doneCount = PHASES.filter((ph) => nodeStateFor(project, ph.key) === "done").length;
  const completed = project.status === "completed";

  return (
    <div className="build-wrap">
      <div className="build-head">
        <div>
          <h1>{project.name || project.idea}</h1>
          <div className="build-meta">
            <span className="fd-badge fd-badge-muted">{project.routing_mode}</span>
            <span className="fd-badge fd-badge-muted">
              {project.require_approval ? "approval gated" : "auto-run"}
            </span>
            <span className={"fd-badge " + topBadgeClass(project.status)}>
              {project.status.replace(/_/g, " ")}
            </span>
          </div>
        </div>
        {completed && (
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <button className="fd-btn" onClick={() => setTab("summary")}>
              Publish to GitHub →
            </button>
            <a className="fd-btn fd-btn-primary" href={api.downloadUrl(id)} download>
              Download .zip ↓
            </a>
          </div>
        )}
      </div>

      {/* pipeline tracker */}
      <div className="fd-card" style={{ marginTop: 22 }}>
        <div className="sec-head">
          <span className="fd-kicker">pipeline</span>
          <span className="fd-rule" />
          <span className="fd-kicker">{doneCount}/8</span>
        </div>
        <div className="tracker">
          {PHASES.map((ph) => {
            const st = trackStateFor(nodeStateFor(project, ph.key));
            return (
              <div key={ph.key} className={"trk " + st}>
                <div className="trk-bar" />
                <div className="trk-label">
                  <span className="trk-n">{ph.n}</span>
                  <span className="trk-name">{ph.label}</span>
                  <span className="trk-mark">
                    {st === "done" ? "✓" : st === "current" ? "•" : st === "rejected" ? "↻" : ""}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {error && (
        <p style={{ color: "var(--accent)", fontSize: 14, marginTop: 14 }}>{error}</p>
      )}

      {/* tabs */}
      <div className="fd-tabs" style={{ marginTop: 24 }}>
        {(["build", "preview", "summary"] as Tab[]).map((t) => (
          <button
            key={t}
            className={"fd-tab" + (tab === t ? " active" : "")}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>

      <div style={{ marginTop: 22 }}>
        {tab === "build" && (
          <BuildTab
            project={project}
            analytics={analytics}
            busy={busy}
            feedback={feedback}
            setFeedback={setFeedback}
            act={act}
            id={id}
          />
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
  feedback,
  setFeedback,
  act,
  id,
}: {
  project: Project;
  analytics: any;
  busy: boolean;
  feedback: string;
  setFeedback: (s: string) => void;
  act: (fn: () => Promise<unknown>) => Promise<void>;
  id: string;
}) {
  const notStarted = project.status === "created";
  const running = project.status === "running";
  const awaiting = project.status === "awaiting_approval";
  const completed = project.status === "completed";
  const pct = localPct(project);

  if (notStarted) {
    return (
      <div className="fd-card build-run-empty">
        <span className="fd-kicker">ready to build</span>
        <p className="fd-muted" style={{ fontSize: 14.5, maxWidth: "52ch", lineHeight: 1.55, margin: 0 }}>
          Eight specialist agents will take this idea from requirements to a deployment plan.
          {project.require_approval
            ? " You’ll approve each phase before it continues."
            : " It will run straight through without stopping."}
        </p>
        <button className="fd-btn fd-btn-primary" disabled={busy} onClick={() => act(() => api.run(id))}>
          Run the pipeline →
        </button>
      </div>
    );
  }

  const doneCount = PHASES.filter((ph) => nodeStateFor(project, ph.key) === "done").length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div
        className="fd-card"
        style={{ display: "flex", alignItems: "center", gap: 20, flexWrap: "wrap", padding: "16px 22px" }}
      >
        <div className="meter" style={{ flex: 1 }}>
          <div className="meter-row">
            <span className="fd-kicker">phase</span>
            <span className="meter-v">{doneCount}/8</span>
          </div>
          <div className="meter-row">
            <span className="fd-kicker">tokens</span>
            <span className="meter-v">
              {analytics ? Number(analytics.total_tokens || 0).toLocaleString() : "—"}
            </span>
          </div>
          <div className="meter-row">
            <span className="fd-kicker">cost</span>
            <span className="meter-v">
              {analytics ? `$${Number(analytics.total_cost_usd || 0).toFixed(4)}` : "—"}
            </span>
          </div>
          <div className="meter-row">
            <span className="fd-kicker">local</span>
            <span className="meter-v">{pct === null ? "—" : `${pct}%`}</span>
          </div>
        </div>
        <span className={"fd-badge " + topBadgeClass(project.status)}>
          {(running || awaiting) && <span className="fd-dot fd-dot-accent" />}
          {awaiting ? "awaiting you" : project.status.replace(/_/g, " ")}
        </span>
      </div>

      <div className="fd-card">
        <PipelineNodes
          project={project}
          busy={busy}
          feedback={feedback}
          setFeedback={setFeedback}
          act={act}
          id={id}
        />
      </div>

      {completed && analytics && (
        <div className="fd-card">
          <div className="sec-head">
            <span className="fd-kicker">analytics</span>
            <span className="fd-rule" />
          </div>
          <div className="stat-grid">
            <Stat v={analytics.calls ?? 0} l="LLM calls" />
            <Stat v={Number(analytics.total_tokens || 0).toLocaleString()} l="Tokens" />
            <Stat v={`$${Number(analytics.total_cost_usd || 0).toFixed(4)}`} l="Est. cost" />
            <Stat v={`${analytics.avg_latency_ms ?? 0} ms`} l="Avg latency" />
          </div>
        </div>
      )}
    </div>
  );
}

// Live pipeline node list (with the inline approval gate).
function PipelineNodes({
  project,
  busy,
  feedback,
  setFeedback,
  act,
  id,
}: {
  project: Project;
  busy: boolean;
  feedback: string;
  setFeedback: (s: string) => void;
  act: (fn: () => Promise<unknown>) => Promise<void>;
  id: string;
}) {
  const done = project.status === "completed";
  return (
    <div className="build-pipe">
      {PHASES.map((ph) => {
        const ns = nodeStateFor(project, ph.key);
        const row = latestRow(project, ph.key);
        return (
          <div key={ph.key} className={"node n-" + ns}>
            <div className="node-spine">
              <span className="node-dot" />
            </div>
            <div className="node-body">
              <div className="node-top">
                <span className="node-n">{ph.n}</span>
                <span className="node-name">{ph.name}</span>
                <span className="node-role">{ph.role}</span>
                {ph.debate && <span className="node-badge">debate</span>}
                <span className="node-status">{nodeStatusLabel(ns, row)}</span>
              </div>

              {(ns === "active" || ns === "redo") && (
                <div className="node-prog">
                  <span />
                </div>
              )}

              {ns === "done" && (
                <div className="node-out">
                  <span className="node-deliver">{ph.deliver}</span>
                  <span className="node-outtext">{summarize(row?.content_md)}</span>
                </div>
              )}

              {ns === "gate" && (
                <div className="gate">
                  <div className="gate-text">{summarize(row?.content_md, 260)}</div>
                  <div className="gate-actions">
                    <button
                      className="fd-btn fd-btn-primary fd-btn-sm"
                      disabled={busy}
                      onClick={() => act(() => api.approve(id))}
                    >
                      Approve →
                    </button>
                    <input
                      className="fd-input"
                      style={{ flex: 1, minWidth: 180 }}
                      placeholder="Feedback for regeneration…"
                      value={feedback}
                      onChange={(e) => setFeedback(e.target.value)}
                    />
                    <button
                      className="fd-btn fd-btn-sm"
                      disabled={busy || !feedback.trim()}
                      onClick={() => act(() => api.reject(id, feedback)).then(() => setFeedback(""))}
                    >
                      Reject &amp; regenerate
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        );
      })}
      <div className={"pipe-end" + (done ? " on" : "")}>
        <span className="node-dot" />
        <span>
          {done
            ? "Build complete — deliverables stored, summary written to memory."
            : "END"}
        </span>
      </div>
    </div>
  );
}

// ── Preview tab ──────────────────────────────────────────────────────────────
type PreviewMode = "visual" | "code" | "live";

function PreviewTab({ id }: { id: string }) {
  const [mode, setMode] = useState<PreviewMode>("visual");
  const modes: { key: PreviewMode; label: string }[] = [
    { key: "visual", label: "Visual" },
    { key: "code", label: "Code" },
    { key: "live", label: "Live (beta)" },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div className="fd-tabs">
        {modes.map((m) => (
          <button
            key={m.key}
            className={"fd-tab" + (mode === m.key ? " active" : "")}
            onClick={() => setMode(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>
      {mode === "visual" && <VisualPreview id={id} />}
      {mode === "code" && <CodePreview id={id} />}
      {mode === "live" && <LiveView />}
    </div>
  );
}

function LiveView() {
  return (
    <div className="fd-card live-stub">
      <span className="live-orbit">
        <span>beta</span>
      </span>
      <div>
        <p className="fd-kicker" style={{ marginBottom: 8 }}>
          live react preview · coming soon
        </p>
        <p className="fd-muted" style={{ fontSize: 14, maxWidth: "46ch", lineHeight: 1.55, margin: "0 auto" }}>
          This tab will bundle and run your actual generated components in-browser for pixel-accurate
          fidelity. For now, use <span className="fd-mono" style={{ color: "var(--ink)" }}>Visual</span>{" "}
          for an editable mockup and <span className="fd-mono" style={{ color: "var(--ink)" }}>Code</span>{" "}
          to read the generated files.
        </p>
      </div>
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

  if (error) return <p style={{ color: "var(--accent)", fontSize: 14 }}>{error}</p>;
  if (!art) return <p className="fd-dim" style={{ fontSize: 14 }}>Loading files…</p>;

  const items = [
    ...art.files.map((f) => ({ path: f.path, content: f.content, tag: f.language || "code" })),
    ...art.docs.map((d) => ({ path: d.path, content: d.content, tag: "md" })),
  ];

  if (items.length === 0) {
    return (
      <div className="fd-card">
        <p className="fd-muted" style={{ fontSize: 14 }}>
          Nothing to preview yet. Run the pipeline — generated code and docs will appear here as the
          Backend, Frontend, QA and DevOps phases complete.
        </p>
      </div>
    );
  }

  const current = items[Math.min(sel, items.length - 1)];

  return (
    <div className="code-grid">
      <div className="fd-card code-files">
        <p className="fd-kicker" style={{ margin: "0 0 10px", padding: "0 6px" }}>
          {items.length} files
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {items.map((it, i) => (
            <button
              key={it.path}
              className={"fd-filerow" + (i === sel ? " active" : "")}
              onClick={() => setSel(i)}
              title={it.path}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{it.path}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="fd-card code-pane">
        <div className="code-pane-head">
          <span className="fd-mono" style={{ fontSize: 12, color: "var(--ink)" }}>
            {current.path}
          </span>
          <span className="fd-badge fd-badge-muted">{current.tag}</span>
        </div>
        <CodeBlock code={current.content} path={current.path} tag={current.tag} />
      </div>
    </div>
  );
}

// ── Summary tab ──────────────────────────────────────────────────────────────
function SummaryTab({ id, analytics }: { id: string; analytics: any }) {
  const [art, setArt] = useState<Artifacts | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.getArtifacts(id).then(setArt).catch((e) => setError(e.message));
  }, [id]);

  if (error) return <p style={{ color: "var(--accent)", fontSize: 14 }}>{error}</p>;
  if (!art) return <p className="fd-dim" style={{ fontSize: 14 }}>Loading summary…</p>;

  const hasOutput = art.files.length > 0 || art.docs.length > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="fd-card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: 14 }}>
          <span className="fd-kicker">your project</span>
          <a
            className={"fd-btn fd-btn-primary fd-btn-sm" + (hasOutput ? "" : " pointer-events-none")}
            style={hasOutput ? undefined : { opacity: 0.4, pointerEvents: "none" }}
            href={api.downloadUrl(id)}
            download
          >
            Download .zip ↓
          </a>
        </div>
        <p className="fd-muted" style={{ fontSize: 14 }}>{art.idea}</p>
        <div className="stat-grid" style={{ marginTop: 18 }}>
          <Stat v={art.files.length} l="Files" />
          <Stat v={art.docs.length} l="Docs" />
          <Stat v={art.setup_instructions.length} l="Setup steps" />
          <Stat v={analytics ? `$${Number(analytics.total_cost_usd || 0).toFixed(4)}` : "—"} l="Cost" />
        </div>
      </div>

      <GithubPublish id={id} defaultName={art.name || art.idea} disabled={!hasOutput} />

      <div className="fd-card">
        <div className="sec-head">
          <span className="fd-kicker">setup on your pc</span>
          <span className="fd-rule" />
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
          <p className="fd-dim" style={{ fontSize: 14 }}>
            Setup steps appear once the Backend/DevOps phases run. Meanwhile, the .zip includes a
            generated <span className="fd-mono">README.md</span>.
          </p>
        )}
      </div>

      <div className="fd-card">
        <div className="sec-head">
          <span className="fd-kicker">deliverables</span>
          <span className="fd-rule" />
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 7 }}>
          {PHASES.map((ph) => (
            <span key={ph.key} className="fd-badge fd-badge-muted">
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

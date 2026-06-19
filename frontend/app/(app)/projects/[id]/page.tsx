"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, Artifacts, Project } from "@/lib/api";
import VisualPreview from "@/components/preview/VisualPreview";

const PHASE_LABELS: Record<string, string> = {
  product_manager: "Product Requirements",
  system_design: "Architecture",
  backend_engineer: "Backend",
  frontend_engineer: "Frontend",
  qa_engineer: "Tests",
  security_engineer: "Security",
  devops_engineer: "Deployment",
  cost_estimation: "Cost",
};
const PHASE_ORDER = Object.keys(PHASE_LABELS);

function badgeClass(state: string): string {
  if (state === "approved") return "fd-badge-ok";
  if (state === "current" || state === "awaiting_approval" || state === "pending_approval")
    return "fd-badge-accent";
  if (state === "rejected") return "fd-badge-warn";
  return "fd-badge-muted";
}

type Tab = "build" | "preview" | "summary";

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

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e: any) {
      setError(e.message);
    } finally {
      // Always re-sync with the backend afterwards — on failure this clears any stale
      // approval gate (e.g. a 400 because the run already completed) instead of leaving
      // the user stuck clicking a button the backend has moved past.
      await load();
      setBusy(false);
    }
  }

  if (!project) {
    return <p className="fd-dim text-sm">{error || "Loading…"}</p>;
  }

  const latest = [...project.phases].sort(
    (a, b) => +new Date(b.created_at) - +new Date(a.created_at),
  )[0];
  const awaiting = project.status === "awaiting_approval";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="fd-title text-2xl">{project.name || project.idea}</h1>
          <p className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
            <span className="fd-badge fd-badge-muted">{project.routing_mode}</span>
            <span className="fd-badge fd-badge-muted">
              {project.require_approval ? "approval gated" : "auto-run"}
            </span>
            <span className={`fd-badge ${badgeClass(project.status)}`}>
              {project.status.replace("_", " ")}
            </span>
          </p>
        </div>
        <div className="flex gap-2">
          {project.status === "created" && (
            <button
              className="fd-btn fd-btn-primary"
              disabled={busy}
              onClick={() => act(() => api.run(id))}
            >
              Run pipeline →
            </button>
          )}
        </div>
      </div>

      {/* Progress tracker */}
      <div className="fd-card">
        <div className="mb-3 flex items-center gap-3">
          <span className="fd-kicker">pipeline</span>
          <span className="fd-rule" />
        </div>
        <div className="flex flex-wrap gap-2">
          {PHASE_ORDER.map((key) => {
            const phaseRows = project.phases.filter((p) => p.phase === key);
            const row = phaseRows[phaseRows.length - 1];
            const state = !row
              ? "pending"
              : row.status === "approved"
                ? "approved"
                : project.current_phase === key
                  ? "current"
                  : row.status;
            return (
              <span key={key} className={`fd-badge ${badgeClass(state)}`}>
                {PHASE_LABELS[key]}
              </span>
            );
          })}
        </div>
      </div>

      {error && (
        <p className="text-sm" style={{ color: "var(--accent)" }}>
          {error}
        </p>
      )}

      {/* Tabs */}
      <div className="fd-tabs">
        {(["build", "preview", "summary"] as Tab[]).map((t) => (
          <button key={t} className={`fd-tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>

      {tab === "build" && (
        <BuildTab
          project={project}
          latest={latest}
          awaiting={awaiting}
          busy={busy}
          feedback={feedback}
          setFeedback={setFeedback}
          act={act}
          id={id}
          analytics={analytics}
        />
      )}
      {tab === "preview" && <PreviewTab id={id} />}
      {tab === "summary" && <SummaryTab id={id} analytics={analytics} />}
    </div>
  );
}

// ── Build tab (approval + phase outputs) ─────────────────────────────────────
function BuildTab({
  project,
  latest,
  awaiting,
  busy,
  feedback,
  setFeedback,
  act,
  id,
  analytics,
}: {
  project: Project;
  latest: Project["phases"][number] | undefined;
  awaiting: boolean;
  busy: boolean;
  feedback: string;
  setFeedback: (s: string) => void;
  act: (fn: () => Promise<unknown>) => Promise<void>;
  id: string;
  analytics: any;
}) {
  return (
    <div className="space-y-4">
      {awaiting && latest && (
        <div className="fd-card fd-card-accent">
          <p className="fd-kicker mb-2" style={{ color: "var(--accent)" }}>
            awaiting approval
          </p>
          <h2 className="fd-title">Review: {PHASE_LABELS[latest.phase] || latest.phase}</h2>
          <p className="mb-3 mt-1 text-sm fd-muted">
            Produced by {latest.agent} via {latest.provider_used}/{latest.model_used}. Approve to
            continue, or reject with feedback to regenerate.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <button
              className="fd-btn fd-btn-primary"
              disabled={busy}
              onClick={() => act(() => api.approve(id))}
            >
              Approve →
            </button>
            <input
              className="fd-input flex-1"
              placeholder="Feedback for regeneration…"
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
            />
            <button
              className="fd-btn"
              disabled={busy || !feedback.trim()}
              onClick={() => act(() => api.reject(id, feedback)).then(() => setFeedback(""))}
            >
              Reject &amp; redo
            </button>
          </div>
        </div>
      )}

      {project.phases.map((p) => (
        <details key={p.id} className="fd-card" open={p.id === latest?.id}>
          <summary className="flex cursor-pointer items-center justify-between gap-3">
            <span className="font-medium">{PHASE_LABELS[p.phase] || p.phase}</span>
            <span className={`fd-badge ${badgeClass(p.status)}`}>{p.status.replace("_", " ")}</span>
          </summary>
          <pre className="fd-pre mt-3">{p.content_md}</pre>
          {p.feedback && (
            <p className="mt-2 text-xs" style={{ color: "#f0a93a" }}>
              Feedback given: {p.feedback}
            </p>
          )}
        </details>
      ))}
      {project.phases.length === 0 && (
        <p className="text-sm fd-dim">No phases yet — run the pipeline.</p>
      )}

      {analytics && analytics.calls > 0 && (
        <div className="fd-card">
          <div className="mb-3 flex items-center gap-3">
            <span className="fd-kicker">analytics</span>
            <span className="fd-rule" />
          </div>
          <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
            <Stat label="LLM calls" value={analytics.calls} />
            <Stat label="Tokens" value={analytics.total_tokens.toLocaleString()} />
            <Stat label="Est. cost" value={`$${analytics.total_cost_usd.toFixed(4)}`} />
            <Stat label="Avg latency" value={`${analytics.avg_latency_ms} ms`} />
          </div>
        </div>
      )}
    </div>
  );
}

// ── Preview tab (visual mockup / code / live) ────────────────────────────────
type PreviewMode = "visual" | "code" | "live";

function PreviewTab({ id }: { id: string }) {
  const [mode, setMode] = useState<PreviewMode>("visual");
  const modes: { key: PreviewMode; label: string }[] = [
    { key: "visual", label: "Visual" },
    { key: "code", label: "Code" },
    { key: "live", label: "Live (beta)" },
  ];
  return (
    <div className="space-y-4">
      <div className="fd-tabs">
        {modes.map((m) => (
          <button
            key={m.key}
            className={`fd-tab ${mode === m.key ? "active" : ""}`}
            onClick={() => setMode(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>
      {mode === "visual" && <VisualPreview id={id} />}
      {mode === "code" && <CodePreview id={id} />}
      {mode === "live" && <LivePreviewStub />}
    </div>
  );
}

// Approach B placeholder — the seam for a future in-browser bundler (Sandpack/WebContainers)
// that runs the actual generated React files. Structured now so it can drop in without rework.
function LivePreviewStub() {
  return (
    <div className="fd-card space-y-2">
      <p className="fd-kicker">live react preview · coming soon</p>
      <p className="text-sm fd-muted">
        This tab will bundle and run your actual generated components in-browser for
        pixel-accurate fidelity. For now, use <span className="fd-mono">Visual</span> for an
        editable mockup and <span className="fd-mono">Code</span> to read the generated files.
      </p>
    </div>
  );
}

// ── Code view (file browser) ─────────────────────────────────────────────────
function CodePreview({ id }: { id: string }) {
  const [art, setArt] = useState<Artifacts | null>(null);
  const [error, setError] = useState("");
  const [sel, setSel] = useState(0);

  useEffect(() => {
    api.getArtifacts(id).then(setArt).catch((e) => setError(e.message));
  }, [id]);

  if (error) return <p className="text-sm" style={{ color: "var(--accent)" }}>{error}</p>;
  if (!art) return <p className="fd-dim text-sm">Loading files…</p>;

  const items = [
    ...art.files.map((f) => ({ path: f.path, content: f.content, tag: f.language || "code" })),
    ...art.docs.map((d) => ({ path: d.path, content: d.content, tag: "md" })),
  ];

  if (items.length === 0) {
    return (
      <div className="fd-card">
        <p className="text-sm fd-muted">
          Nothing to preview yet. Run the pipeline — generated code and docs will appear here as the
          Backend, Frontend, QA and DevOps phases complete.
        </p>
      </div>
    );
  }

  const current = items[Math.min(sel, items.length - 1)];

  return (
    <div className="grid gap-4 md:grid-cols-[260px_1fr]">
      <div className="fd-card" style={{ padding: 12 }}>
        <p className="fd-kicker mb-2 px-2">{items.length} files</p>
        <div className="flex flex-col gap-0.5">
          {items.map((it, i) => (
            <button
              key={it.path}
              className={`fd-filerow ${i === sel ? "active" : ""}`}
              onClick={() => setSel(i)}
              title={it.path}
            >
              <span className="truncate">{it.path}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="fd-card" style={{ padding: 0, overflow: "hidden" }}>
        <div
          className="flex items-center justify-between px-4 py-2.5"
          style={{ borderBottom: "1px solid var(--line)" }}
        >
          <span className="fd-mono text-xs" style={{ color: "var(--ink)" }}>
            {current.path}
          </span>
          <span className="fd-badge fd-badge-muted">{current.tag}</span>
        </div>
        <pre className="fd-pre" style={{ margin: 0, padding: 16, maxHeight: 560, overflow: "auto" }}>
          {current.content}
        </pre>
      </div>
    </div>
  );
}

// ── Summary tab (overview + setup + download) ────────────────────────────────
function SummaryTab({ id, analytics }: { id: string; analytics: any }) {
  const [art, setArt] = useState<Artifacts | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.getArtifacts(id).then(setArt).catch((e) => setError(e.message));
  }, [id]);

  if (error) return <p className="text-sm" style={{ color: "var(--accent)" }}>{error}</p>;
  if (!art) return <p className="fd-dim text-sm">Loading summary…</p>;

  const hasOutput = art.files.length > 0 || art.docs.length > 0;

  return (
    <div className="space-y-4">
      <div className="fd-card">
        <div className="mb-3 flex items-center justify-between gap-3">
          <span className="fd-kicker">your project</span>
          <a
            className={`fd-btn fd-btn-primary ${hasOutput ? "" : "pointer-events-none opacity-40"}`}
            href={api.downloadUrl(id)}
            download
          >
            Download .zip ↓
          </a>
        </div>
        <p className="text-sm fd-muted">{art.idea}</p>
        <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Files" value={art.files.length} />
          <Stat label="Docs" value={art.docs.length} />
          <Stat label="Setup steps" value={art.setup_instructions.length} />
          <Stat
            label="Cost"
            value={analytics ? `$${(analytics.total_cost_usd ?? 0).toFixed(4)}` : "—"}
          />
        </div>
      </div>

      <div className="fd-card">
        <div className="mb-3 flex items-center gap-3">
          <span className="fd-kicker">setup on your pc</span>
          <span className="fd-rule" />
        </div>
        {art.setup_instructions.length > 0 ? (
          <ol className="space-y-2 text-sm fd-muted">
            {art.setup_instructions.map((s, i) => (
              <li key={i} className="flex gap-3">
                <span className="fd-mono" style={{ color: "var(--accent)" }}>
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span>{s}</span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-sm fd-dim">
            Setup steps will appear once the Backend/DevOps phases run. Meanwhile, download the .zip
            — it includes a generated <span className="fd-mono">README.md</span>.
          </p>
        )}
      </div>

      <details className="fd-card">
        <summary className="cursor-pointer fd-kicker">readme.md preview</summary>
        <pre className="fd-pre mt-3">{art.readme}</pre>
      </details>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="fd-kicker">{label}</div>
      <div className="fd-mono text-lg" style={{ color: "var(--ink)" }}>
        {value}
      </div>
    </div>
  );
}

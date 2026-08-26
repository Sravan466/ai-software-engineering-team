"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { EXAMPLES, PHASES, ROUTING_MODES } from "@/components/shell/phases";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";

/**
 * The home view: describe an idea, choose how it should be routed and whether
 * the pipeline pauses for you, then start the build.
 */
export default function NewBuildPage() {
  const router = useRouter();
  useChrome({ sub: "New build" }, []);

  const [idea, setIdea] = useState("");
  const [mode, setMode] = useState("local");
  const [approvals, setApprovals] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function start() {
    const trimmed = idea.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError("");
    try {
      const backendMode = ROUTING_MODES.find((m) => m.id === mode)?.backend ?? "local_only";
      const project = await api.createProject({
        idea: trimmed,
        routing_mode: backendMode,
        require_approval: approvals,
      });
      // Kick the pipeline off in the background; the build view polls for progress.
      api.run(project.id).catch(() => {});
      router.push(`/projects/${project.id}`);
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  }

  const activeMode = ROUTING_MODES.find((m) => m.id === mode);

  return (
    <div className="composer-page">
      <h1 className="composer-h1">
        What should we <em>build today?</em>
      </h1>
      <p className="prose-lede composer-lede">
        Describe a product idea. Eight specialist agents take it from requirements through
        architecture, code, tests, security and deployment — pausing for your approval at every
        phase.
      </p>

      <div className="composer">
        <label htmlFor="idea" className="sr-only">
          Product idea
        </label>
        <textarea
          id="idea"
          autoFocus
          placeholder="e.g. a habit-tracking app with streaks and smart reminders…"
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) start();
          }}
        />
        <div className="composer-foot">
          <div className="composer-opts">
            <div className="seg" role="group" aria-label="Model routing">
              {ROUTING_MODES.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className="seg-btn"
                  aria-pressed={mode === m.id}
                  onClick={() => setMode(m.id)}
                  disabled={busy}
                  title={m.hint}
                >
                  {m.label}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="switch"
              role="switch"
              aria-checked={approvals}
              onClick={() => setApprovals((a) => !a)}
              disabled={busy}
            >
              <span className="switch-track" aria-hidden="true" />
              Approval gates
            </button>
          </div>
          <div className="composer-submit">
            <span className="composer-hint" aria-hidden="true">
              ⌘↵
            </span>
            <button className="btn btn-primary" disabled={!idea.trim() || busy} onClick={start}>
              {busy && <span className="btn-spinner" aria-hidden="true" />}
              {busy ? "Starting…" : "Start build"}
              {!busy && Icon.arrowRight}
            </button>
          </div>
        </div>
      </div>

      <p className="field-hint" style={{ marginTop: 10 }}>
        {activeMode?.hint}
        {approvals
          ? " The pipeline stops after each phase so you can read the output and approve it."
          : " The pipeline runs all eight phases without stopping."}
      </p>

      {error && (
        <div className="notice notice-bad" role="alert" style={{ marginTop: 14 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t start the build</span>
            <span className="notice-text">{error}</span>
          </div>
        </div>
      )}

      <div className="examples">
        {EXAMPLES.map((ex) => (
          <button key={ex} className="example" onClick={() => setIdea(ex)}>
            {ex}
          </button>
        ))}
      </div>

      <section className="pipeline-legend">
        <h2 className="label">The pipeline · every build</h2>
        <div className="pipeline-legend-row">
          {PHASES.map((ph) => (
            <span key={ph.key} className="pipeline-chip" title={`${ph.name} — ${ph.role}`}>
              <i>{ph.n}</i>
              <b>{ph.label}</b>
            </span>
          ))}
        </div>
      </section>
    </div>
  );
}

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { EXAMPLES, ROUTING_MODES } from "@/components/shell/phases";
import { AGENTS } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";

/**
 * The home view: describe an idea, choose how it runs, meet the crew that will
 * build it. The roster is not decoration — it is the clearest statement of what
 * this product does, which is put eight specialists on your idea in sequence.
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
        What should we
        <br />
        <em>build today?</em>
      </h1>
      <p className="prose-lede composer-lede">
        Describe a product idea. Eight specialists take it from requirements through
        architecture, code, tests, security and deployment — stopping for your approval at every
        handoff.
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

      <p className="field-hint" style={{ marginTop: 10, maxWidth: "64ch" }}>
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

      <section className="crew">
        <div className="sec-head">
          <h2 className="label">The crew · runs in this order</h2>
          <span className="rule" />
        </div>
        <div className="roster">
          {AGENTS.map((a) => (
            <article
              key={a.key}
              className="agent-card"
              style={{ ["--agent" as string]: a.accent }}
            >
              <AgentSprite agent={a} size={46} state="done" />
              <div className="agent-card-body">
                <span className="agent-num">{a.n}</span>
                <h3 className="agent-codename">{a.codename}</h3>
                <span className="agent-role">{a.role}</span>
                <p className="agent-tagline">{a.tagline}</p>
                <span className="agent-trait">{a.trait}</span>
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { EXAMPLES, PHASES, ROUTING_MODES } from "@/components/shell/phases";
import { useChrome } from "@/components/shell/ShellChrome";

/**
 * The home view: the v3 "What should we build today?" composer. Describe an idea,
 * pick a routing mode + whether to gate on approvals, and Build → creates the
 * project, kicks off the run, and drops you into its live build view.
 */
export default function NewBuildPage() {
  const router = useRouter();
  useChrome({ sub: "new build" }, []);

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

  return (
    <div className="newbuild">
      <div className="nb-kicker">
        <span className="fd-kicker" style={{ color: "var(--accent)" }}>
          autonomous engineering org · 8 agents
        </span>
      </div>
      <h1 className="nb-h1">
        What should we
        <br />
        <span className="em">build today?</span>
      </h1>
      <p className="nb-lede">
        Describe a product idea. A team of specialist agents takes it from requirements to a
        deployment plan — pausing for your approval at every phase.
      </p>

      <div className="nb-composer">
        <textarea
          autoFocus
          placeholder="e.g. a habit-tracking app with streaks and smart reminders…"
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) start();
          }}
        />
        <div className="nb-foot">
          <div className="nb-opts">
            <div className="seg">
              <span className="seg-k">routing</span>
              {ROUTING_MODES.map((m) => (
                <button
                  key={m.id}
                  className={"seg-btn" + (mode === m.id ? " on" : "")}
                  onClick={() => setMode(m.id)}
                  disabled={busy}
                >
                  {m.id}
                </button>
              ))}
            </div>
            <button
              className={"gate-toggle" + (approvals ? " on" : "")}
              onClick={() => setApprovals((a) => !a)}
              disabled={busy}
            >
              <span className="gate-box" /> approval gates
            </button>
          </div>
          <button className="fd-btn fd-btn-primary" disabled={!idea.trim() || busy} onClick={start}>
            {busy ? "Starting…" : "Build →"}
          </button>
        </div>
      </div>

      {error && (
        <p className="mt-3 text-sm" style={{ color: "var(--accent)", marginTop: 12 }}>
          {error}
        </p>
      )}

      <div className="nb-examples">
        {EXAMPLES.map((ex) => (
          <button key={ex} className="nb-ex" onClick={() => setIdea(ex)}>
            {ex}
          </button>
        ))}
      </div>

      <div className="nb-phases">
        <span className="fd-kicker">the pipeline · every build</span>
        <div className="nb-phases-row">
          {PHASES.map((ph, i) => (
            <span key={ph.key} style={{ display: "inline-flex", gap: "6px", alignItems: "center" }}>
              <b>{ph.label}</b>
              {i < PHASES.length - 1 && <span className="sep">→</span>}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

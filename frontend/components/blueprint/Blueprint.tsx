"use client";

/**
 * Blueprint.tsx — Direction A: Blueprint. Swiss / technical, wired pipeline.
 * TypeScript/React port of the Claude Design export `fd-blueprint.jsx`
 * (preserved verbatim at design/claude-design-export/).
 */
import type { ReactNode } from "react";
import {
  DEBATE,
  EXAMPLES,
  PHASES,
  fmtCost,
  fmtTok,
  usePipeline,
  type Phase,
  type PhaseState,
  type Result,
} from "./pipeline";
import "./blueprint.css";

type Skin = {
  theme?: "dark" | "light";
  palette?: "rgb" | "cmyk" | "mono";
  font?: "brut" | "editorial" | "mech";
  density?: "compact" | "regular" | "comfy";
  grain?: boolean;
  scan?: boolean;
  cursor?: boolean;
  /** App navigation rendered in the header (replaces the "v4 · blueprint" tag). */
  nav?: ReactNode;
};

function BlueprintNode({
  phase,
  state,
  result,
  onApprove,
  onReject,
}: {
  phase: Phase;
  state: PhaseState;
  result?: Result;
  onApprove: () => void;
  onReject: () => void;
}) {
  return (
    <div className={"bp-node bp-" + state}>
      <div className="bp-node-spine">
        <span className="bp-node-dot" />
      </div>
      <div className="bp-node-body">
        <div className="bp-node-top">
          <span className="bp-node-n">{phase.n}</span>
          <span className="bp-node-name">{phase.name}</span>
          <span className="bp-node-role">{phase.role}</span>
          {phase.debate && <span className="bp-node-badge">debate</span>}
          <span className="bp-node-status">
            {state === "pending" && "queued"}
            {state === "active" && "running…"}
            {state === "redo" && "re-running"}
            {state === "gate" && "awaiting approval"}
            {state === "done" && (result ? result.model : "done")}
          </span>
        </div>
        {(state === "active" || state === "redo") && (
          <div className="bp-node-prog">
            <span />
          </div>
        )}
        {state === "done" && (
          <div className="bp-node-out">
            <span className="bp-node-deliver">{phase.deliver}</span>
            <span className="bp-node-outtext">{phase.out}</span>
          </div>
        )}
        {state === "gate" && (
          <div className="bp-gate">
            {phase.debate && (
              <div className="bp-debate">
                <span className="bp-debate-k">agent debate · {DEBATE.topic}</span>
                <span className="bp-debate-v">→ {DEBATE.verdict}</span>
              </div>
            )}
            <div className="bp-gate-text">{phase.out}</div>
            <div className="bp-gate-actions">
              <button className="bp-btn bp-btn-ok" onClick={onApprove}>
                Approve →
              </button>
              <button className="bp-btn bp-btn-no" onClick={onReject}>
                Reject &amp; regenerate
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function Blueprint({
  theme = "dark",
  palette = "rgb",
  font = "brut",
  density = "comfy",
  grain = true,
  scan = true,
  cursor = false,
  nav,
}: Skin = {}) {
  const p = usePipeline(EXAMPLES[0]);

  const stateFor = (i: number): PhaseState => {
    if (p.results.find((r) => r.idx === i)) return "done";
    if (i === p.current && p.status === "thinking") return "active";
    if (i === p.current && p.status === "redo") return "redo";
    if (i === p.current && p.status === "gate") return "gate";
    return "pending";
  };
  const running = p.status !== "idle";

  return (
    <div
      className="fd-root"
      data-theme={theme}
      data-palette={palette}
      data-font={font}
      data-density={density}
      data-grain={grain ? "on" : "off"}
      data-scan={scan ? "on" : "off"}
      data-cursor={cursor ? "on" : "off"}
    >
      <div className="dir dir-blueprint fx-skin">
        <div className="bp-grid">
          {/* LEFT */}
          <div className="bp-left">
            <header className="bp-head">
              <div className="bp-wm">
                <span className="bp-wm-mark" />
                <span className="bp-wm-text">
                  AI<span className="bp-wm-thin">SWE TEAM</span>
                </span>
              </div>
              <div className="bp-head-meta">
                {nav ?? <span className="mono-k">v4 · blueprint</span>}
                <span className="bp-head-dot" data-on={running} />
                <span className="mono-k">{running ? "pipeline live" : "idle"}</span>
              </div>
            </header>

            <div className="bp-hero">
              <h1 className="bp-h1">
                Ship the whole
                <br />
                engineering team.
                <br />
                <span className="bp-h1-em">Not just a tool.</span>
              </h1>
              <p className="bp-sub">
                Eight specialist agents take one idea to production-ready software — product,
                design, backend, frontend, QA, security, ops, cost — and pause for your approval
                at every phase.
              </p>
            </div>

            <div className="bp-prompt">
              <div className="bp-prompt-head">
                <span className="mono-k">project idea</span>
                <span className="bp-prompt-rule" />
                <button
                  className="bp-shuffle"
                  onClick={() =>
                    p.setIdea(EXAMPLES[Math.floor(Math.random() * EXAMPLES.length)])
                  }
                  disabled={running}
                >
                  shuffle
                </button>
              </div>
              <div className="bp-prompt-field">
                <span className="bp-caret">›</span>
                <textarea
                  value={p.idea}
                  disabled={running}
                  onChange={(e) => p.setIdea(e.target.value)}
                  rows={2}
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>
            </div>

            <div className="bp-controls">
              <div className="bp-seg">
                <span className="bp-seg-k">routing</span>
                {(["auto", "local", "manual"] as const).map((m) => (
                  <button
                    key={m}
                    className={"bp-seg-btn" + (p.mode === m ? " on" : "")}
                    disabled={running}
                    onClick={() => p.setMode(m)}
                  >
                    {m}
                  </button>
                ))}
              </div>
              <button
                className={"bp-approve-toggle" + (p.approvals ? " on" : "")}
                disabled={running}
                onClick={() => p.setApprovals((a) => !a)}
              >
                <span className="bp-toggle-box" />
                approval gates
              </button>
            </div>

            <div className="bp-cta">
              {!running && (
                <button className="bp-run" onClick={p.run}>
                  Run the pipeline <span className="bp-run-arrow">→</span>
                </button>
              )}
              {running && (
                <button className="bp-run bp-run-reset" onClick={p.reset}>
                  Reset
                </button>
              )}
              <div className="bp-meter">
                <div className="bp-meter-row">
                  <span className="mono-k">tokens</span>
                  <span className="bp-meter-v">{fmtTok(p.totals.tok)}</span>
                </div>
                <div className="bp-meter-row">
                  <span className="mono-k">cost</span>
                  <span className="bp-meter-v">{fmtCost(p.totals.cost)}</span>
                </div>
                <div className="bp-meter-row">
                  <span className="mono-k">on local</span>
                  <span className="bp-meter-v">{p.totals.localPct}%</span>
                </div>
              </div>
            </div>
          </div>

          {/* RIGHT — pipeline */}
          <div className="bp-right">
            <div className="bp-right-head">
              <span className="mono-k">pipeline</span>
              <span className="bp-prompt-rule" />
              <span className="mono-k">{p.results.length}/8 phases</span>
            </div>
            <div className="bp-pipe">
              {PHASES.map((ph, i) => (
                <BlueprintNode
                  key={ph.key}
                  phase={ph}
                  state={stateFor(i)}
                  result={p.results.find((r) => r.idx === i)}
                  onApprove={p.approve}
                  onReject={p.reject}
                />
              ))}
              <div className={"bp-end" + (p.status === "done" ? " on" : "")}>
                <span className="bp-node-dot" />
                <span>
                  {p.status === "done"
                    ? "Build complete — deliverables stored, summary written to memory."
                    : "END"}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  api,
  type Artifacts,
  type GateKind,
  type PhaseResult,
  type PreviewState,
  type Project,
} from "@/lib/api";
import { PHASES, PHASE_BY_KEY, PLAN_PHASE_KEYS } from "@/components/shell/phases";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import MockupFrame from "@/components/preview/MockupFrame";
import PhaseArtifact from "./PhaseArtifact";
import FileBrowser from "./FileBrowser";
import { artifactFiles, latestRow, type PayloadFile } from "./payload";

/**
 * The decision the pipeline is waiting on, and everything it is a decision about.
 *
 * There used to be one gate shape, repeated eight times, wherever the pipeline
 * happened to pause. There are now four, chosen by what the run actually stopped for:
 *
 *   plan     — Scope's spec and Atlas's architecture, approved once, while changing
 *              them is still cheap.
 *   ship     — one pass over the finished build: the file tree, the mockup, Warden's
 *              findings and Ledger's numbers, with per-file redo.
 *   security — Warden interrupted an otherwise unattended run over something severe.
 *   phase    — a single handoff, for anyone who kept the every-phase rhythm.
 *
 * Whatever the shape, the rule is the same: the work is above the buttons, in the
 * same panel, and sending it back reaches the agent that produced it.
 */

type Target = { phase: string; path?: string };

const HEAD: Record<GateKind, { title: string; blurb: string; approve: string; after: string }> = {
  plan: {
    title: "Plan review",
    blurb: "The scope and the architecture, before anyone writes code against them.",
    approve: "Approve the plan",
    after: "Five agents then build it without stopping.",
  },
  ship: {
    title: "Ship review",
    blurb: "Everything the crew built, in one pass.",
    approve: "Ship it",
    after: "Approving marks this build complete.",
  },
  cost: {
    title: "Ship review · over budget",
    blurb: "Everything the crew built — and an estimate that came in over your cap.",
    approve: "Ship it anyway",
    after: "Approving accepts the cost and marks this build complete.",
  },
  security: {
    title: "Security stop",
    blurb: "Warden found something serious enough to interrupt the build.",
    approve: "Accept and continue",
    after: "The remaining phases then run without stopping.",
  },
  phase: {
    title: "Handoff",
    blurb: "One agent has finished and is passing its work on.",
    approve: "Approve and continue",
    after: "Approving starts the next agent.",
  },
};

const rowFor = (project: Project, key: string) => latestRow(project.phases, key);

export default function Decision({
  project,
  id,
  busy,
  act,
}: {
  project: Project;
  id: string;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<boolean>;
}) {
  const kind: GateKind = (project.gate_kind as GateKind) || "phase";
  const gatePhase = project.current_phase || "";
  const copy = HEAD[kind] ?? HEAD.phase;

  const [target, setTarget] = useState<Target | null>(null);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);

  // The build under review, fetched only for the pass that needs all of it.
  const wantsBuild = kind === "ship" || kind === "cost";
  const [art, setArt] = useState<Artifacts | null>(null);
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [artError, setArtError] = useState("");
  const [reloads, setReloads] = useState(0);

  useEffect(() => {
    if (!wantsBuild) return;
    setArtError("");
    // A failed fetch is not "still loading". Collapsing the two left the Ship review
    // showing placeholder bars forever above a live Ship it button — approving a
    // build whose files, findings and costs were never actually on screen.
    api
      .getArtifacts(id)
      .then((a) => {
        setArt(a);
        setArtError("");
      })
      .catch((e: any) => setArtError(e.message));
    api.getPreview(id).then(setPreview).catch(() => setPreview(null));
  }, [wantsBuild, id, reloads]);

  // The mockup is drawn alongside the pipeline, so on a fast run it can still be
  // generating when this review opens. Without this the Mockup tab would simply be
  // missing, and the reviewer would approve a build with no picture — which is the
  // thing folding it into the Frontend phase was meant to fix.
  //
  // Bounded: a local model takes 30-60s for this, so past a couple of minutes it did
  // not fail to arrive in time, it failed. Polling a dead generation for as long as
  // someone leaves the tab open buys nothing; the Preview tab can draw one on demand.
  const [waited, setWaited] = useState(0);
  const awaitingMockup = wantsBuild && !preview?.html && waited < 15;
  useEffect(() => {
    if (!awaitingMockup) return;
    const timer = setInterval(() => {
      setWaited((n) => n + 1);
      api.getPreview(id).then(setPreview).catch(() => {});
    }, 8000);
    return () => clearInterval(timer);
  }, [awaitingMockup, id]);

  const files = useMemo(() => (art ? artifactFiles(art.files) : []), [art]);

  // On a whole-build review the gate phase is Ledger, which is the least likely
  // recipient of "the auth flow is wrong". Nothing is assumed: the reviewer aims the
  // note at a file or a panel first.
  const mustAim = wantsBuild && !target;
  const redoPhase = target?.phase || gatePhase;
  const redoAgent = AGENT_BY_KEY[redoPhase];

  // Sending an earlier phase back invalidates everything built on top of it, so the
  // run rebuilds from there. That is a much bigger action than correcting the phase
  // on screen, and the reviewer should know before they press it, not after.
  const order = PHASES.map((p) => p.key);
  const rebuilds =
    order.indexOf(redoPhase) >= 0 &&
    order.indexOf(gatePhase) > order.indexOf(redoPhase);

  /** Point the note at a phase — and at one file within it, when that is the ask. */
  function aim(phase: string, path?: string) {
    setTarget({ phase, path });
    document.getElementById("decision-note")?.focus();
  }

  async function send() {
    const text = note.trim();
    if (!text || !redoPhase || mustAim || sending || busy) return;
    const message = target?.path ? `Rewrite \`${target.path}\` — ${text}` : text;
    setSending(true);
    const sent = await act(() => api.redo(id, redoPhase, message));
    setSending(false);
    // Only on success. A 409 from a second tab used to take the typed note and the
    // chosen file with it, leaving a red banner and nothing to resend.
    if (sent) {
      setNote("");
      setTarget(null);
    }
  }

  return (
    <section className={`decision decision-${kind}`} aria-labelledby="decision-title">
      <header className="decision-head">
        <span className="decision-mark" aria-hidden="true">
          {kind === "security" || kind === "cost" ? Icon.alert : Icon.check}
        </span>
        <div className="decision-headings">
          <h2 id="decision-title">{copy.title}</h2>
          <p>{copy.blurb}</p>
        </div>
        <span className="badge badge-warn">
          <span className="dot dot-warn dot-pulse" aria-hidden="true" />
          Waiting on you
        </span>
      </header>

      {project.gate_note && (
        <p className="decision-alarm" role="status">
          {Icon.alert}
          <span>{project.gate_note}</span>
        </p>
      )}

      <div className="decision-body">
        {kind === "plan" && <PlanReview project={project} onRedo={aim} />}
        {(kind === "security" || kind === "phase") && (
          <SinglePhase project={project} phase={gatePhase} onRedo={aim} />
        )}
        {wantsBuild && (
          <ShipReview
            project={project}
            art={art}
            error={artError}
            onRetry={() => setReloads((n) => n + 1)}
            files={files}
            preview={preview}
            onRedo={aim}
            onRedoFile={(file) => file.phase && aim(file.phase, file.path)}
          />
        )}
      </div>

      <div className="decision-act">
        <div className="decision-approve">
          <button
            className="btn btn-lg btn-accent"
            disabled={busy}
            onClick={() => act(() => api.approve(id))}
          >
            {busy && !sending && <span className="btn-spinner" aria-hidden="true" />}
            {Icon.check} {copy.approve}
          </button>
          <span className="field-hint">{copy.after}</span>
        </div>

        <div className="decision-send">
          <div className="field">
            <label htmlFor="decision-note">
              {target?.path ? (
                <>
                  Send <code className="mono">{target.path}</code> back to{" "}
                  {redoAgent?.codename ?? redoPhase}
                </>
              ) : mustAim ? (
                <>Or send part of it back — pick a file or a panel above first</>
              ) : (
                <>Or send it back to {redoAgent?.codename ?? redoPhase} with a note</>
              )}
            </label>
            <input
              id="decision-note"
              className="input"
              placeholder={
                target?.path
                  ? "e.g. this handler doesn't validate the request body"
                  : "e.g. drop the social feed and focus on the core loop"
              }
              value={note}
              disabled={busy || mustAim}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") send();
                if (e.key === "Escape" && target) setTarget(null);
              }}
            />
          </div>
          {target?.path && (
            <button
              className="btn"
              disabled={busy}
              onClick={() => setTarget({ phase: target.phase })}
            >
              Whole phase instead
            </button>
          )}
          <button
            className="btn btn-danger"
            disabled={busy || mustAim || !note.trim()}
            onClick={send}
          >
            {sending && <span className="btn-spinner" aria-hidden="true" />}
            {Icon.undo} {rebuilds ? "Send back and rebuild" : "Send back"}
          </button>
        </div>
        {rebuilds && (
          <p className="field-hint decision-consequence">
            {Icon.alert}
            <span>
              Every phase after {redoAgent?.codename ?? redoPhase} was built on the
              version you are replacing, so the run rebuilds from there. You come back
              to this review over the new build.
            </span>
          </p>
        )}
      </div>
    </section>
  );
}

/**
 * One agent's contribution to the decision, with the way to send *that* part back.
 *
 * A review that covers several agents needs this: the Plan review shows Scope and
 * Atlas together, and "narrow the scope" has to reach Scope. Addressing the send-back
 * to whichever phase the gate happens to sit on would quietly hand a scope note to
 * the architect.
 */
function PhasePanel({
  phase,
  row,
  maxHeight,
  onRedo,
}: {
  phase: string;
  row: PhaseResult;
  maxHeight: number;
  onRedo: (phase: string) => void;
}) {
  const agent = AGENT_BY_KEY[phase];
  const meta = PHASE_BY_KEY[phase];
  return (
    <div className="plan-part" style={{ ["--agent" as string]: agent?.accent }}>
      <div className="plan-part-head">
        <AgentSprite agent={agent} size={30} state="gate" />
        <b className="agent-line-name">{agent?.codename}</b>
        <span className="phase-deliver">{meta?.deliver}</span>
        <span className="rule" />
        {row.total_tokens > 0 && (
          <span className="phase-tokens mono">{row.total_tokens.toLocaleString()} tok</span>
        )}
        <button
          className="btn btn-sm btn-danger"
          onClick={() => onRedo(phase)}
          title={`Address the note below to ${agent?.codename ?? phase}`}
        >
          {Icon.undo} Send back
        </button>
      </div>
      <PhaseArtifact row={row} maxHeight={maxHeight} />
    </div>
  );
}

// ── plan: two agents, one decision ───────────────────────────────────────────
function PlanReview({
  project,
  onRedo,
}: {
  project: Project;
  onRedo: (phase: string) => void;
}) {
  const rows = PLAN_PHASE_KEYS.map((key) => [key, rowFor(project, key)] as const).filter(
    (entry): entry is readonly [string, PhaseResult] => Boolean(entry[1]),
  );
  if (rows.length === 0) return <Nothing>The plan hasn&apos;t been written yet.</Nothing>;

  return (
    <div className="plan-review">
      {rows.map(([key, row]) => (
        <PhasePanel key={key} phase={key} row={row} maxHeight={380} onRedo={onRedo} />
      ))}
    </div>
  );
}

// ── one handoff, or one interrupt ────────────────────────────────────────────
function SinglePhase({
  project,
  phase,
  onRedo,
}: {
  project: Project;
  phase: string;
  onRedo: (phase: string) => void;
}) {
  const row = phase ? rowFor(project, phase) : undefined;
  if (!row) return <Nothing>There is nothing to read for this phase.</Nothing>;
  return <PhasePanel phase={phase} row={row} maxHeight={480} onRedo={onRedo} />;
}

// ── ship: the whole build in one pass ────────────────────────────────────────
type ShipView = "files" | "mockup" | "security" | "cost";

function ShipReview({
  project,
  art,
  error,
  onRetry,
  files,
  preview,
  onRedo,
  onRedoFile,
}: {
  project: Project;
  art: Artifacts | null;
  error: string;
  onRetry: () => void;
  files: PayloadFile[];
  preview: PreviewState | null;
  onRedo: (phase: string) => void;
  onRedoFile: (file: PayloadFile) => void;
}) {
  const security = rowFor(project, "security_engineer");
  const cost = rowFor(project, "cost_estimation");

  const views: { key: ShipView; label: string; icon: ReactNode; count?: number }[] = [];
  if (files.length) views.push({ key: "files", label: "Files", icon: Icon.file, count: files.length });
  if (preview?.html) views.push({ key: "mockup", label: "Mockup", icon: Icon.sparkle });
  if (security) views.push({ key: "security", label: "Security", icon: Icon.alert });
  if (cost) views.push({ key: "cost", label: "Cost", icon: Icon.list });

  const [view, setView] = useState<ShipView>("files");
  const active = views.find((v) => v.key === view) ?? views[0];

  if (error) {
    return (
      <div className="artifact-pad">
        <div className="notice notice-bad" role="alert">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t load what this build produced</span>
            <span className="notice-text">
              {error} Nothing below is the build — don&apos;t ship it until this loads.
            </span>
            <div className="notice-actions">
              <button className="btn btn-sm btn-primary" onClick={onRetry}>
                {Icon.refresh} Try again
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }
  if (!art) {
    return (
      <div className="artifact-pad">
        <SkeletonLines lines={5} />
      </div>
    );
  }
  if (views.length === 0) {
    return <Nothing>This build produced nothing to review.</Nothing>;
  }

  return (
    <div className="artifact">
      <div className="artifact-bar">
        <div className="switcher" role="group" aria-label="What this build produced">
          {views.map((v) => (
            <button
              key={v.key}
              className="seg-btn seg-btn-icon"
              aria-pressed={active?.key === v.key}
              onClick={() => setView(v.key)}
            >
              {v.icon}
              {v.label}
              {v.count !== undefined && <span className="seg-count">{v.count}</span>}
            </button>
          ))}
        </div>
        <span className="artifact-note mono">
          {art.files.length} files · {art.docs.length} docs
        </span>
      </div>

      {active?.key === "files" && (
        <div className="artifact-view artifact-files" style={{ maxHeight: 520 }}>
          <FileBrowser
            files={files}
            renderAction={(file) =>
              // Only when the file knows which phase wrote it — there is no honest
              // redo for a file we cannot address.
              file.phase ? (
                <button
                  className="btn btn-sm btn-danger"
                  onClick={() => onRedoFile(file)}
                  title={`Send this file back to ${
                    AGENT_BY_KEY[file.phase]?.codename ?? file.phase
                  }`}
                >
                  {Icon.undo} Redo this file
                </button>
              ) : null
            }
          />
        </div>
      )}
      {active?.key === "mockup" && preview?.html && (
        <div className="artifact-view" style={{ maxHeight: 620 }}>
          <div className="artifact-pad">
            <MockupFrame html={preview.html} height={460} />
            <p className="field-hint" style={{ marginTop: 10 }}>
              Drawn by the Frontend phase. Edit it section by section on the Preview tab.
            </p>
          </div>
        </div>
      )}
      {/* Warden and Ledger bring their own reading controls — findings as a table,
          costs as figures — so the phase panel nests here rather than being flattened
          into a wall of prose. */}
      {active?.key === "security" && security && (
        <PhasePanel
          phase="security_engineer"
          row={security}
          maxHeight={440}
          onRedo={onRedo}
        />
      )}
      {active?.key === "cost" && cost && (
        <PhasePanel phase="cost_estimation" row={cost} maxHeight={440} onRedo={onRedo} />
      )}
    </div>
  );
}

function Nothing({ children }: { children: ReactNode }) {
  return <div className="artifact-empty">{children}</div>;
}

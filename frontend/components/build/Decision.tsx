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
import { PHASE_BY_KEY, PLAN_PHASE_KEYS } from "@/components/shell/phases";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import MockupFrame from "@/components/preview/MockupFrame";
import PhaseArtifact from "./PhaseArtifact";
import FileBrowser from "./FileBrowser";
import { artifactFiles, type PayloadFile } from "./payload";

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
    title: "Cost check",
    blurb: "Ledger's estimate came in over the cap you set for this build.",
    approve: "Accept the cost",
    after: "Approving lets the run finish.",
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

// Latest row produced for a phase (phases re-run when sent back).
function latestRow(project: Project, key: string): PhaseResult | undefined {
  const rows = project.phases.filter((p) => p.phase === key);
  if (rows.length === 0) return undefined;
  return [...rows].sort((a, b) => +new Date(a.created_at) - +new Date(b.created_at))[
    rows.length - 1
  ];
}

export default function Decision({
  project,
  id,
  busy,
  act,
}: {
  project: Project;
  id: string;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
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

  useEffect(() => {
    if (!wantsBuild) return;
    api.getArtifacts(id).then(setArt).catch(() => setArt(null));
    api.getPreview(id).then(setPreview).catch(() => setPreview(null));
  }, [wantsBuild, id]);

  const files = useMemo(() => (art ? artifactFiles(art.files) : []), [art]);

  const redoPhase = target?.phase || gatePhase;
  const redoAgent = AGENT_BY_KEY[redoPhase];

  async function send() {
    const text = note.trim();
    if (!text || !redoPhase) return;
    const message = target?.path ? `Rewrite \`${target.path}\` — ${text}` : text;
    setSending(true);
    await act(() => api.redo(id, redoPhase, message));
    setSending(false);
    setNote("");
    setTarget(null);
  }

  return (
    <section className={`decision decision-${kind}`} aria-labelledby="decision-title">
      <header className="decision-head">
        <span className="decision-mark" aria-hidden="true">
          {kind === "security" ? Icon.alert : Icon.check}
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
        {kind === "plan" && <PlanReview project={project} />}
        {kind === "security" && <SinglePhase project={project} phase={gatePhase} />}
        {kind === "phase" && <SinglePhase project={project} phase={gatePhase} />}
        {wantsBuild && (
          <ShipReview
            project={project}
            art={art}
            files={files}
            preview={preview}
            onRedoFile={(file) => {
              setTarget({ phase: file.sourceKey, path: file.path });
              document.getElementById("decision-note")?.focus();
            }}
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
              ) : (
                <>Or send it back with a note on what to change</>
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
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") send();
                if (e.key === "Escape" && target) setTarget(null);
              }}
            />
          </div>
          {target?.path && (
            <button className="btn" disabled={busy} onClick={() => setTarget(null)}>
              Whole phase instead
            </button>
          )}
          <button className="btn btn-danger" disabled={busy || !note.trim()} onClick={send}>
            {sending && <span className="btn-spinner" aria-hidden="true" />}
            {Icon.undo} Send back
          </button>
        </div>
      </div>
    </section>
  );
}

// ── plan: two agents, one decision ───────────────────────────────────────────
function PlanReview({ project }: { project: Project }) {
  const rows = PLAN_PHASE_KEYS.map((key) => [key, latestRow(project, key)] as const).filter(
    ([, row]) => row,
  );
  if (rows.length === 0) return <Nothing>The plan hasn&apos;t been written yet.</Nothing>;

  return (
    <div className="plan-review">
      {rows.map(([key, row]) => {
        const agent = AGENT_BY_KEY[key];
        const meta = PHASE_BY_KEY[key];
        return (
          <div key={key} className="plan-part" style={{ ["--agent" as string]: agent?.accent }}>
            <div className="plan-part-head">
              <AgentSprite agent={agent} size={30} state="gate" />
              <b className="agent-line-name">{agent?.codename}</b>
              <span className="phase-deliver">{meta?.deliver}</span>
              <span className="rule" />
              {row!.total_tokens > 0 && (
                <span className="phase-tokens mono">
                  {row!.total_tokens.toLocaleString()} tok
                </span>
              )}
            </div>
            <PhaseArtifact row={row!} maxHeight={380} />
          </div>
        );
      })}
    </div>
  );
}

// ── one handoff, or one interrupt ────────────────────────────────────────────
function SinglePhase({ project, phase }: { project: Project; phase: string }) {
  const row = phase ? latestRow(project, phase) : undefined;
  if (!row) return <Nothing>There is nothing to read for this phase.</Nothing>;
  const agent = AGENT_BY_KEY[phase];
  const meta = PHASE_BY_KEY[phase];
  return (
    <div className="plan-part" style={{ ["--agent" as string]: agent?.accent }}>
      <div className="plan-part-head">
        <AgentSprite agent={agent} size={30} state="gate" />
        <b className="agent-line-name">{agent?.codename}</b>
        <span className="phase-deliver">{meta?.deliver}</span>
        <span className="rule" />
      </div>
      <PhaseArtifact row={row} maxHeight={480} />
    </div>
  );
}

// ── ship: the whole build in one pass ────────────────────────────────────────
type ShipView = "files" | "mockup" | "security" | "cost";

function ShipReview({
  project,
  art,
  files,
  preview,
  onRedoFile,
}: {
  project: Project;
  art: Artifacts | null;
  files: PayloadFile[];
  preview: PreviewState | null;
  onRedoFile: (file: PayloadFile) => void;
}) {
  const security = latestRow(project, "security_engineer");
  const cost = latestRow(project, "cost_estimation");

  const views: { key: ShipView; label: string; icon: ReactNode; count?: number }[] = [];
  if (files.length) views.push({ key: "files", label: "Files", icon: Icon.file, count: files.length });
  if (preview?.html) views.push({ key: "mockup", label: "Mockup", icon: Icon.sparkle });
  if (security) views.push({ key: "security", label: "Security", icon: Icon.alert });
  if (cost) views.push({ key: "cost", label: "Cost", icon: Icon.list });

  const [view, setView] = useState<ShipView>("files");
  const active = views.find((v) => v.key === view) ?? views[0];

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
            renderAction={(file) => (
              <button
                className="btn btn-sm btn-danger"
                onClick={() => onRedoFile(file)}
                title={`Send this file back to ${
                  AGENT_BY_KEY[file.sourceKey]?.codename ?? file.sourceKey
                }`}
              >
                {Icon.undo} Redo this file
              </button>
            )}
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
        <PhaseArtifact row={security} maxHeight={460} />
      )}
      {active?.key === "cost" && cost && <PhaseArtifact row={cost} maxHeight={460} />}
    </div>
  );
}

function Nothing({ children }: { children: ReactNode }) {
  return <div className="artifact-empty">{children}</div>;
}

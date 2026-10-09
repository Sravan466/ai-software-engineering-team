"use client";

import { useCallback, useEffect, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import Link from "next/link";
import { api, type Artifacts, type DatabaseState, type Project, type RunResponse } from "@/lib/api";
import { listOf } from "@/lib/text";
import { APPROVAL_BY_ID, PHASES } from "@/components/shell/phases";
import { AGENT_BY_KEY, suiteLine, type Persona } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import {
  NODE_STATUS,
  VOICE_FOR,
  crewAtRest,
  effectiveStatus,
  latestRow,
  nodeStateFor,
  spriteFor,
  type NodeState,
} from "@/components/agents/phaseState";
import { useProject } from "@/components/build/useProject";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { Skeleton, SkeletonLines } from "@/components/ui/Skeleton";
import VisualPreview from "@/components/preview/VisualPreview";
import ShipCard, { type ShipIntent } from "@/components/deploy/ShipCard";
import SchemaBadge from "@/components/build/SchemaBadge";
import PhaseArtifact from "@/components/build/PhaseArtifact";
import FileBrowser from "@/components/build/FileBrowser";
import BuildLine from "@/components/build/BuildLine";
import Decision from "@/components/build/Decision";
import { ChangeStrip, Finished } from "@/components/build/Changes";
import Versions from "@/components/deploy/Versions";
import { databaseUnconnected } from "@/lib/database";
import { DatabasePanel } from "@/components/build/DatabaseConnect";
import { IntegrationsPanel } from "@/components/connectors/Integrations";
import { connectorsLabel, connectorsUnconnected } from "@/lib/connectors";
import { FixingPanel } from "@/components/build/AutoFix";
import ReviewPolicy from "@/components/build/ReviewPolicy";
import RunControls from "@/components/build/RunControls";
import PhaseSteps from "@/components/build/PhaseSteps";
import { onOpenFile } from "@/lib/openFile";
import type { FileFocus } from "@/components/build/FileBrowser";

import { artifactFiles } from "@/components/build/payload";
import RelayFloor, { useHeightSwap, useRelayView } from "@/components/crew/RelayFloor";

type Tab = "build" | "preview" | "summary";

// ── status → presentation ────────────────────────────────────────────────────
const STATUS_LABEL: Record<string, string> = {
  created: "Not started",
  running: "Running",
  awaiting_approval: "Waiting for you",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Stopped",
  paused: "Paused",
  stalled: "Stalled",
};

function badgeClass(status: string): string {
  if (status === "completed") return "badge-ok";
  if (status === "awaiting_approval" || status === "paused") return "badge-warn";
  if (status === "running") return "badge-run";
  if (status === "failed" || status === "stalled") return "badge-bad";
  return "";
}

function StatusBadge({ status }: { status: string }) {
  const live = status === "running" || status === "awaiting_approval";
  const dot =
    status === "completed"
      ? "dot-ok"
      : status === "awaiting_approval"
        ? "dot-warn dot-pulse"
        : status === "paused"
          ? "dot-warn"
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

/**
 * What a queued phase is actually waiting for.
 *
 * "Queued" is true and useless: eight steps saying the same word tell you
 * nothing about which of them is next or what is holding the line. This reads
 * the phase in front of it and says so.
 */
function waitingFor(project: Project, index: number): string {
  if (index === 0) {
    return project.status === "created"
      ? "Starts when you run the pipeline"
      : "Queued";
  }
  const prev = PHASES[index - 1];
  const prevName = AGENT_BY_KEY[prev.key].codename;
  switch (nodeStateFor(project, prev.key)) {
    case "running":
      // A stalled run's phase row still says `running`; nothing is driving it.
      // Reading the row alone is how the badge could say "Stalled" while the
      // step beneath it said the phase in front was still working.
      return effectiveStatus(project) === "stalled"
        ? `Queued · ${prevName} stopped responding`
        : `Queued · ${prevName} is still working`;
    case "gate":
      return `Queued · waiting for your review of ${prevName}`;
    case "redo":
      return `Queued · ${prevName} is running again`;
    case "failed":
      return `Queued · ${prevName} stopped mid-phase`;
    case "done":
      return `Queued · next after ${prevName}`;
    default:
      return `Queued · behind ${prevName}`;
  }
}

/**
 * The one-line answer to "who has the work". Used by the compact relay.
 *
 * Reads the project's *effective* status, not the phase row alone. A stalled run
 * is one whose row still says `running` while nothing is driving it, so a summary
 * built only from the row would announce "FORGE is working" directly between a
 * badge reading "Stalled" and a panel offering to resume it.
 */
function relaySummary(project: Project, doneCount: number): string {
  const stalled = effectiveStatus(project) === "stalled";
  const live = PHASES.find((ph) => {
    const ns = nodeStateFor(project, ph.key);
    return ns === "running" || ns === "gate" || ns === "redo";
  });
  if (!live) {
    if (doneCount === PHASES.length) return "All eight approved";
    if (project.status === "created") return "Nobody has the work yet";
    return "Nobody is working right now";
  }
  const name = AGENT_BY_KEY[live.key].codename;
  const ns = nodeStateFor(project, live.key);
  if (ns === "gate") return `${name} is waiting on you`;
  if (ns === "redo") return `${name} is running again`;
  return stalled ? `${name} stopped responding mid-phase` : `${name} is working`;
}

/** "SCOPE", "SCOPE and ATLAS", "SCOPE, ATLAS and FORGE". */
function localPct(project: Project): number | null {
  // Where each phase ran is the backend's answer, recorded per call — never read
  // off a provider's name, which says nothing about where its model runs.
  const known = project.phases.filter((p) => p.provider_used && p.is_local !== null);
  if (known.length === 0) return null;
  const local = known.filter((p) => p.is_local).length;
  return Math.round((local / known.length) * 100);
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
  // The build, kept fresh at the cadence its status calls for (shared with the crew
  // floor, #91).
  const { project, setProject, analytics, error, setError, load, actionFailed, loadedAt } = useProject(id, {
    analytics: true,
  });
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<Tab>("build");
  // "Deploy it" / "Connect to GitHub" from the completion banner: open Deliver on
  // that flow. Cleared once the card has taken it, so a tab switch doesn't re-open it.
  const [shipIntent, setShipIntent] = useState<ShipIntent | null>(null);
  const clearShipIntent = useCallback(() => setShipIntent(null), []);
  // Which phase the relay is sending you to. A pending instruction, not a
  // record of where you last went: PhaseList clears it the moment it acts, or a
  // tab round-trip — which unmounts and remounts that list with the same value
  // still sitting here — would scroll you back to a phase you already left.
  const [jump, setJump] = useState<Jump | null>(null);
  const clearJump = useCallback(() => setJump(null), []);
  // A security finding's `path:line` (#77): the phase that wrote it, on its Files
  // view, at that line — from wherever the finding was on screen.
  useEffect(
    () =>
      onOpenFile((j) => {
        setTab("build");
        setJump({ key: j.phase, file: { path: j.path, line: j.line } });
      }),
    [],
  );
  // Returning from the GitHub OAuth round-trip? Land on Deliver, where the ship
  // card lives (it reads ?github= and ?next= itself).
  // From the crew floor (#91): `?phase=` lands on that phase's row.
  // Either is an instruction, and wins over opening on the preview below.
  const deepLinked = useRef(false);
  useEffect(() => {
    const sp = new URLSearchParams(window.location.search);
    if (sp.get("github") || sp.get("next")) {
      deepLinked.current = true;
      setTab("summary");
    }
    const phase = sp.get("phase");
    if (phase) {
      deepLinked.current = true;
      setTab("build");
      setJump({ key: phase });
    }
  }, []);

  // A finished build is its app: open on Preview, the way Lovable and Bolt do, and
  // go there when a build (or a change to it) finishes while you watch. Only on
  // those two moments: once you pick a tab, the poll never takes you off it.
  const lastStatus = useRef<string | null>(null);
  const currentStatus = project?.status ?? null;
  // Another build in the same page is a first look again.
  useEffect(() => {
    lastStatus.current = null;
  }, [id]);
  useEffect(() => {
    if (!currentStatus) return;
    const was = lastStatus.current;
    lastStatus.current = currentStatus;
    if (currentStatus !== "completed" || was === "completed") return;
    if (was === null ? !deepLinked.current : true) setTab("preview");
  }, [currentStatus]);

  /** Run a control call. Returns whether it landed, so callers can keep the
   *  reviewer's typing when it didn't. */
  const act = useCallback(
    async (fn: () => Promise<RunResponse | unknown>): Promise<boolean> => {
      setBusy(true);
      setError("");
      actionFailed.current = false;
      let ok = false;
      let failure = "";
      try {
        const result = (await fn()) as (RunResponse & { change?: Project["change"] }) | undefined;
        ok = true;
        // Control endpoints return the status they just committed. Applying it
        // before the reload means the badge flips the instant the click lands,
        // instead of reading "Waiting for you" while an agent is generating. A change
        // just started comes back too (#79), so its strip takes the finished slot in
        // the same frame instead of leaving it empty until the reload.
        if (result && typeof result.status === "string") {
          setProject((p) =>
            p ? { ...p, status: result.status, ...(result.change ? { change: result.change } : {}) } : p,
          );
        }
      } catch (e: any) {
        failure = e.message;
      } finally {
        await load();
        // After the reload, and marked as a control's: a successful `load` (and the
        // status poll after it) used to clear the error, which wiped every refusal —
        // a 409 from another tab, a credential in a note — the moment it arrived.
        if (failure) {
          actionFailed.current = true;
          setError(failure);
        }
        setBusy(false);
      }
      return ok;
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

  const atRest = crewAtRest(project);

  const doneCount = PHASES.filter((ph) => nodeStateFor(project, ph.key) === "done").length;
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
    // The Preview tab is a canvas: it takes the width a desktop layout needs.
    <div className={"build-wrap" + (tab === "preview" ? " build-wrap-wide" : "")}>
      <div className="build-head">
        <div style={{ minWidth: 0 }}>
          <h1>{project.name || project.idea}</h1>
          <div className="build-meta">
            <span className="badge">
              {project.routing_mode === "local_only"
                ? "Local models"
                : project.preferred_model || project.routing_mode}
            </span>
            <ReviewPolicy project={project} id={id} onChanged={load} />
            {(project.deploy_url || project.github_repo) && (
              <span className="ship-badges">
                {project.deploy_url && (
                  <a href={project.deploy_url} target="_blank" rel="noreferrer" title={project.deploy_url}>
                    <span className="badge badge-ok">
                      <span className="dot dot-ok" aria-hidden="true" />
                      Live {Icon.external}
                    </span>
                  </a>
                )}
                {project.github_repo && (
                  <a
                    href={`https://github.com/${project.github_repo}`}
                    target="_blank"
                    rel="noreferrer"
                    title={project.github_repo}
                  >
                    <span className="badge badge-mono">
                      {Icon.github} {project.github_repo}
                    </span>
                  </a>
                )}
              </span>
            )}
            {databaseUnconnected(project) && (
              <button
                type="button"
                className="badge badge-warn db-head-badge"
                onClick={() => setTab("summary")}
                title="Connect it on the Deliver tab"
              >
                {Icon.database} Database not connected
              </button>
            )}
            {connectorsUnconnected(project).length > 0 && (
              <button
                type="button"
                className="badge badge-warn db-head-badge"
                onClick={() => setTab("summary")}
                title="Connect them on the Deliver tab"
              >
                {Icon.plug} {connectorsLabel(connectorsUnconnected(project).length)}
              </button>
            )}
          </div>
        </div>
        {/* Actions belong to the view that owns them. The header used to carry a
            Publish button that only switched tabs and a Download that the Deliver
            tab then offered again; both live in Deliver now, in one place each. */}
        <div className="build-actions">
          <RunControls project={project} busy={busy} act={act} />
        </div>
      </div>

      {/* The relay: who has the work, who is next — and a way into their work. */}
      <RelayCard
        project={project}
        status={status!}
        tab={tab}
        atRest={atRest}
        doneCount={doneCount}
        loadedAt={loadedAt}
        onJump={(key) => {
          setTab("build");
          // A fresh object every click, so asking for the same phase twice is two
          // instructions rather than one unchanged value.
          setJump({ key });
        }}
      />

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
          <BuildTab
            project={project}
            analytics={analytics}
            busy={busy}
            act={act}
            id={id}
            jump={jump}
            onJumpDone={clearJump}
            onDeliver={(intent) => {
              setShipIntent(intent ?? null);
              setTab("summary");
            }}
          />
        )}
        {tab === "preview" && <PreviewTab id={id} onOpenBuild={() => setTab("build")} />}
        {tab === "summary" && (
          <SummaryTab
            id={id}
            project={project}
            analytics={analytics}
            onDatabaseChanged={load}
            shipIntent={shipIntent}
            onShipIntentUsed={clearShipIntent}
          />
        )}
      </div>
    </div>
  );
}

// ── Relay card ───────────────────────────────────────────────────────────────
/**
 * The relay: who has the work, who is next, and a way into their work. Every step
 * is a link to its phase in the list below, so clicking the agent you are curious
 * about lands on what they produced.
 *
 * Two views of the same thing, switched in the card's header (#91): the strip of
 * eight, or the crew floor's room for this build, with its bubbles, board and
 * hand-off courier. The choice is per viewer; the card opens or folds to the new
 * view's height rather than jumping.
 */
function RelayCard({
  project,
  status,
  tab,
  atRest,
  doneCount,
  loadedAt,
  onJump,
}: {
  project: Project;
  status: string;
  tab: Tab;
  atRest: boolean;
  doneCount: number;
  loadedAt: number;
  onJump: (phaseKey: string) => void;
}) {
  const [view, setView] = useRelayView();
  const body = useRef<HTMLDivElement>(null);
  useHeightSwap(body, view);
  return (
    <div className="card" style={{ marginTop: 20 }}>
      <div className="sec-head">
        <h2 className="label">Relay</h2>
        <span className="rule" />
        <span className="label mono">{doneCount}/8</span>
        <div className="relay-views" role="group" aria-label="Relay view">
          <button
            type="button"
            className="relay-view-btn"
            aria-pressed={view === "strip"}
            onClick={() => setView("strip")}
          >
            {Icon.list} Strip
          </button>
          <button
            type="button"
            className="relay-view-btn"
            aria-pressed={view === "floor"}
            onClick={() => setView("floor")}
          >
            {Icon.layers} Floor
          </button>
        </div>
        {view === "floor" && (
          <Link
            className="relay-open"
            href={`/crew?project=${project.id}`}
            title="Open the crew floor: inspector, replay and rooms"
            aria-label="Open the crew floor for this build"
          >
            {Icon.expand}
          </Link>
        )}
      </div>
      {/* Under ~600px the eight names don't fit, so the rail goes compact and
          this line carries what the names were there to say. */}
      <p className="relay-active" aria-live="polite">
        {relaySummary(project, doneCount)}
      </p>
      <div ref={body} className="relay-body">
        {view === "floor" ? (
          <div key="floor" className="relay-pane">
            <RelayFloor project={project} loadedAt={loadedAt} onPick={onJump} />
          </div>
        ) : (
          <ol
            key="strip"
            className="relay relay-pane"
            aria-label={`Pipeline progress: ${doneCount} of 8 phases complete`}
          >
            {PHASES.map((ph, i) => {
              const ns = nodeStateFor(project, ph.key);
              const agent = AGENT_BY_KEY[ph.key];
              const live = ns === "running" || ns === "gate";
              // A stalled run's row still says `running`. The summary above and the
              // steps behind it already say otherwise; the step itself has to agree.
              const what =
                ns === "pending"
                  ? waitingFor(project, i)
                  : ns === "running" && status === "stalled"
                    ? "Stopped responding mid-phase"
                    : NODE_STATUS[ns];
              return (
                <li key={ph.key}>
                  <button
                    className={`relay-step ${ns}`}
                    style={{ ["--agent" as string]: agent.accent }}
                    aria-current={live ? "step" : undefined}
                    // The phase rows only exist while the Build tab is mounted, and
                    // an aria-controls pointing at an absent id sends assistive tech
                    // nowhere. The click still works from any tab — it switches first.
                    aria-controls={tab === "build" ? `phase-${ph.key}` : undefined}
                    onClick={() => onJump(ph.key)}
                    title={`${agent.codename} · ${agent.role} — ${what}`}
                  >
                    <AgentSprite agent={agent} size={64} state={spriteFor(project, ns)} asleep={atRest} />
                    <span className="relay-name">{agent.codename}</span>
                    <span className="relay-bar" />
                    <span className="sr-only">{`${agent.role} — ${what}. Go to this phase.`}</span>
                  </button>
                </li>
              );
            })}
          </ol>
        )}
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
  act: (fn: () => Promise<unknown>) => Promise<boolean>;
  id: string;
}) {
  const state = effectiveStatus(project);
  const copy: Record<string, { title: string; text: string; action: string }> = {
    stalled: {
      title: "This build stopped responding",
      text:
        "No progress in a while. The backend probably restarted mid-phase. Resuming " +
        "re-runs that phase from the last checkpoint and keeps everything approved.",
      action: "Resume from checkpoint",
    },
    paused: {
      title: "Waiting for your computer",
      text:
        "This build's model is on your computer, which isn't connected. Nothing is lost. " +
        "The build picks up from the last finished phase when the connector is back.",
      action: "Resume now",
    },
    cancelled: {
      title: "You stopped this build",
      text:
        "Everything made before the stop is kept. Resuming starts from the last approved phase.",
      action: "Resume",
    },
    failed: {
      title: "This build stopped after a model error",
      text:
        "Usually the local runtime is down. Check in Settings that it's running and has the " +
        "model, then resume.",
      action: "Resume from checkpoint",
    },
  };
  // A cloud provider refused the key (#63): its own words and its own fix, never
  // the local-runtime advice — which is wrong for every part of that case.
  // Only a refusal the person must act on: a rate limit or an outage that outlasted
  // the retries keeps the ordinary failed copy (and its "Check runtime").
  const help = state === "failed" && project.last_error_help?.blocking ? project.last_error_help : null;
  if (help) {
    copy.failed = {
      title: help.title,
      text: `${help.body} Or pick another model in Settings and resume. Approved work is kept.`,
      action: "Resume from checkpoint",
    };
  }
  const { title, text, action } = copy[state] ?? copy.failed;
  const fix = help && help.blocking && help.action_url ? help : null;

  return (
    <div
      className={"notice " + (state === "cancelled" || state === "paused" ? "notice-warn" : "notice-bad")}
      role={state === "paused" ? "status" : undefined}
    >
      {Icon.alert}
      <div className="notice-body">
        <span className="notice-title">{title}</span>
        <span className="notice-text">{text}</span>
        {project.last_error && state !== "cancelled" && state !== "failed" && (
          <span className="notice-detail mono">{project.last_error}</span>
        )}
        {project.last_error && state === "failed" && (
          // The raw error is for whoever reports a bug, not the headline.
          <details className="notice-tech">
            <summary>Technical details</summary>
            <span className="notice-detail mono">{project.last_error}</span>
          </details>
        )}
        <div className="notice-actions">
          {fix && (
            <a className="btn btn-sm btn-primary" href={fix.action_url} target="_blank" rel="noreferrer">
              {fix.action_label} {Icon.external}
            </a>
          )}
          {help && (
            <a className="btn btn-sm" href="/settings#agent-models">
              Change model
            </a>
          )}
          <button
            className={"btn btn-sm" + (fix ? "" : " btn-primary")}
            disabled={busy}
            onClick={() => act(() => api.resume(id))}
          >
            {busy && <span className="btn-spinner" aria-hidden="true" />}
            {Icon.play} {action}
          </button>
          {state === "paused" && (
            <a className="btn btn-sm" href="/setup#computers">
              My computers
            </a>
          )}
          {state === "failed" && !help && (
            <a className="btn btn-sm" href="/settings">
              Check runtime
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

/** Where the page is being sent: a phase, and — from a finding — a file in it (#77). */
type Jump = { key: string; file?: { path: string; line?: number | null } };

/** The gates whose review covers the whole build — a change's review is one of these. */
const WHOLE_BUILD_GATES = new Set(["ship", "cost", "build"]);

function BuildTab({
  project,
  analytics,
  busy,
  act,
  id,
  jump,
  onJumpDone,
  onDeliver,
}: {
  project: Project;
  analytics: any;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<boolean>;
  id: string;
  /** A phase the relay is asking us to go to, if any. */
  jump: Jump | null;
  onJumpDone: () => void;
  onDeliver: (intent?: ShipIntent) => void;
}) {
  const pct = localPct(project);
  const doneCount = PHASES.filter((ph) => nodeStateFor(project, ph.key) === "done").length;
  const state = effectiveStatus(project);
  const interrupted = state === "failed" || state === "cancelled" || state === "stalled" || state === "paused";

  if (state === "created") {
    return (
      <div className="card empty">
        <h3>Ready when you are</h3>
        <p>
          Eight agents take this idea from requirements to a deployment plan.{" "}
          {APPROVAL_BY_ID[project.approval_mode]?.running}
          {project.approval_mode !== "unattended" && " When it stops, you see the files, diagram and data before you decide."}
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
      {/* A change in flight (#79): what was asked and who is on it. At its review the
          whole-build review is the change's own surface, so the strip steps aside;
          at any other stop (the plan, a security stop, the database) it stays, with
          the way back to the version before. */}
      {project.change &&
        !(project.change.status === "awaiting_approval" && WHOLE_BUILD_GATES.has(project.gate_kind ?? "")) && (
          <ChangeStrip project={project} id={id} busy={busy} act={act} />
        )}
      {interrupted && <RunInterrupted project={project} busy={busy} act={act} id={id} />}

      {/* The crew fixing its own serious problems: progress, not a question. */}
      {state === "running" && <FixingPanel project={project} />}

      {/* One decision surface, always in the same place, whatever stopped the run.
          The gate used to be buried inside whichever of eight phase rows happened
          to be open. */}
      {state === "awaiting_approval" && (
        <Decision project={project} id={id} busy={busy} act={act} />
      )}

      {/* The same slot, for the one ending that isn't a dead end — and the start of
          the next turn (#79): what to change, and what was changed before. Counted,
          not assumed: a run can reach `completed` with a phase that produced nothing. */}
      {state === "completed" && (
        <Finished
          project={project}
          id={id}
          busy={busy}
          act={act}
          doneCount={doneCount}
          total={PHASES.length}
          onDeliver={onDeliver}
        />
      )}


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
        <PhaseList
          project={project}
          jump={jump}
          onJumpDone={onJumpDone}
          atRest={crewAtRest(project)}
        />
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
            {Number(analytics.builds) > 0 && (
              // The sandbox's time (#75): installing, building and starting the code.
              <Stat v={`${Math.round(Number(analytics.build_seconds) || 0)}s`} l="Building it" />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The history: every phase, in order, each a disclosure over the agent's full
 * deliverable — the files, the diagram, the structured data, not just the prose.
 *
 * The decision itself is no longer here. It used to render inside whichever of these
 * eight rows the pipeline happened to stop on, which is exactly why it was easy to
 * miss; it now has one home at the top of the tab. This list is for reading back.
 *
 * It is also where the relay lands. `jump` is the step you clicked up there: the
 * row opens if it has anything to show, scrolls itself into view, and marks itself
 * for a moment so you can see which of eight rows just answered you.
 */
function PhaseList({
  project,
  jump,
  onJumpDone,
  atRest,
}: {
  project: Project;
  jump: Jump | null;
  onJumpDone: () => void;
  /** crewAtRest(project) — the same rule as the relay, so they never disagree. */
  atRest: boolean;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [landed, setLanded] = useState<string | null>(null);
  // The file a finding asked for, and in which phase (#77).
  const [focus, setFocus] = useState<(FileFocus & { key: string }) | null>(null);
  const timers = useRef<{ raf?: number; fade?: ReturnType<typeof setTimeout> }>({});
  // A stopped run still finishing its call is live; one whose process died since is
  // as stalled as a running one would be.
  const state = project.status === "cancelled" && project.stalled ? "stalled" : effectiveStatus(project);
  // The steps every running row has shown (#86). If nothing is running as the list
  // first renders, whatever runs next is news and opens; otherwise the running row
  // fills this in itself, and what was already on screen at load renders still.
  const [seenSteps] = useState<MutableRefObject<Set<string> | null>>(() => ({
    current: PHASES.some((ph) => nodeStateFor(project, ph.key) === "running") ? null : new Set(),
  }));

  // The scroll and the highlight outlive the instruction that started them, so
  // they are torn down on unmount rather than by the effect below — which
  // re-runs the instant `jump` is cleared.
  useEffect(
    () => () => {
      if (timers.current.raf) cancelAnimationFrame(timers.current.raf);
      if (timers.current.fade) clearTimeout(timers.current.fade);
    },
    [],
  );

  useEffect(() => {
    if (!jump) return;
    const { key } = jump;
    const row = latestRow(project, key);
    const hasDoc = Boolean(row && row.status !== "running" && (row.content_md || row.output));
    if (hasDoc) setOpen((o) => ({ ...o, [key]: true }));
    if (jump.file) setFocus({ key, path: jump.file.path, line: jump.file.line, n: Date.now() });
    setLanded(key);
    // The tab panel it lives in mounts in this same commit, so wait a frame for
    // layout before asking the browser to scroll to it.
    timers.current.raf = requestAnimationFrame(() => {
      document
        .getElementById(`phase-${key}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    timers.current.fade = setTimeout(() => setLanded(null), 1600);
    // Acted on, so it stops being pending. Without this the value would still be
    // here on the next mount of this list and scroll the page again.
    onJumpDone();
    // `project` is deliberately absent: a 2.5s poll landing mid-read must never
    // re-scroll the page under someone.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump, onJumpDone]);

  return (
    <div className="phases">
      {PHASES.map((ph, i) => {
        const ns = nodeStateFor(project, ph.key);
        const row = latestRow(project, ph.key);
        const isGate = ns === "gate";
        const hasDoc = Boolean(row && row.status !== "running" && (row.content_md || row.output));
        // Closed by default now: the phase under review is already open, in full,
        // in the decision panel above. Opening it here would say it twice. The one
        // exception is "The crew needs a hand": that panel shows the fix rounds, not
        // the phase's work, so the work — and what it worked from — opens here.
        const isOpen =
          open[ph.key] ?? (isGate && project.gate_kind === "needs_help");
        const agent = AGENT_BY_KEY[ph.key];

        return (
          <div
            key={ph.key}
            id={`phase-${ph.key}`}

            className={
              `phase ${ns}` +
              (isOpen && hasDoc ? " open" : "") +
              (landed === ph.key ? " landed" : "")
            }
            style={{ ["--agent" as string]: agent.accent }}
          >
            <button
              className="phase-sum"
              disabled={!hasDoc}
              aria-expanded={hasDoc ? isOpen : undefined}
              onClick={() => hasDoc && setOpen((o) => ({ ...o, [ph.key]: !isOpen }))}
            >
              <AgentSprite
                agent={agent}
                size={40}
                state={spriteFor(project, ns)}
                asleep={atRest}
              />

              <span className="phase-main">
                <span className="phase-line agent-line">
                  <span className="phase-n">{ph.n}</span>
                  <span className="agent-line-name">{agent.codename}</span>
                  <span className="phase-role">{agent.role}</span>
                  {ph.debate && <span className="badge badge-run">Debated</span>}
                  {isGate && <span className="badge badge-warn">Under review above</span>}
                  {ns === "failed" && <span className="badge badge-bad">Interrupted</span>}
                  {row && <SchemaBadge row={row} />}
                </span>
                {/* The agent's own status line, in their voice — and, for a phase
                    that hasn't started, the plain reason it hasn't. The running row
                    tells its steps beneath instead (#86). */}
                {ns !== "running" && (
                  <span className="agent-say">
                    {ns === "pending"
                      ? waitingFor(project, i)
                      : (VOICE_FOR[ns] === "done" && ph.key === "qa_engineer" && suiteLine(row?.test_run)) ||
                        agent.lines[VOICE_FOR[ns]]}
                    {hasDoc && (
                      <span className="phase-deliver" style={{ marginLeft: 8 }}>
                        {ph.deliver}
                      </span>
                    )}
                  </span>
                )}
              </span>

              <span className="phase-side">
                {ns !== "running" && row?.total_tokens ? (
                  <span className="phase-tokens mono">
                    {row.total_tokens.toLocaleString()} tok
                  </span>
                ) : null}
                {row?.provider_used && row?.model_used && (
                  <span className="phase-model" title={row.fallback_note ?? undefined}>
                    {row.provider_used}/{row.model_used}
                    {row.fallback_note && (
                      // A refused key made this phase run elsewhere: say so, and why.
                      <span className="phase-fallback">
                        {" "}· fell back<span className="sr-only">: {row.fallback_note}</span>
                      </span>
                    )}
                  </span>
                )}
                {hasDoc && (
                  <span className="phase-chev" aria-hidden="true">
                    {Icon.chevron}
                  </span>
                )}
              </span>
            </button>

            {/* What the agent has done and is doing, step by step, as it happens. */}
            <PhaseSteps
              running={ns === "running"}
              finished={ns === "done" || ns === "gate"}
              project={project}
              phaseKey={ph.key}
              state={state}
              seen={seenSteps}
            />

            {/* Any phase that produced something can be read in full, whenever —
                including the one under review, which the decision above also shows. */}

            {hasDoc && isOpen && row && (
              <div className="phase-body">
                <PhaseArtifact
                  row={row}
                  maxHeight={420}
                  focus={focus?.key === ph.key ? focus : undefined}
                  onFocused={() => setFocus(null)}
                />
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
/**
 * The mockup, and the loop for changing it.
 *
 * This used to be half a file browser: generated source lived under Preview → Files
 * while Deliver reported a *count* of source files you could not open. The files now
 * sit where decisions about them are made — at the review, and in Deliver — and
 * Preview is what its name says.
 */
function PreviewTab({ id, onOpenBuild }: { id: string; onOpenBuild: () => void }) {
  return <VisualPreview id={id} onOpenBuild={onOpenBuild} />;
}

// ── Deliver tab ──────────────────────────────────────────────────────────────
/**
 * What each phase actually put on the table.
 *
 * This row used to print all eight deliverable names as chips no matter what the
 * run produced, so a build that stopped after two phases still advertised a
 * security review and a Dockerfile. Files carry the phase that wrote them and
 * every doc is `docs/<phase>.md`, so the row can simply be read off the artifacts.
 *
 * The count is files *surviving in the assembled project*, which is not always
 * the number a phase wrote: `assemble` dedupes by path and lets a later phase
 * win, stamping the survivor with the last writer. That makes it the right
 * number for "what is in the archive" and the wrong one for "what this agent
 * produced", so the chip says which it means. Membership comes from the docs,
 * which are appended per phase and never collapse.
 */
function producedByPhase(art: Artifacts): Map<string, number> {
  const byPhase = new Map<string, number>();
  for (const f of art.files) {
    byPhase.set(f.phase, (byPhase.get(f.phase) ?? 0) + 1);
  }
  for (const d of art.docs) {
    const key = d.path.replace(/^docs\//, "").replace(/\.md$/, "");
    if (!byPhase.has(key)) byPhase.set(key, 0);
  }
  return byPhase;
}

/** A run that can still produce something hasn't finished failing to. */
function stillRunning(status: string): boolean {
  return status === "created" || status === "running" || status === "awaiting_approval";
}

function SummaryTab({
  id,
  project,
  analytics,
  onDatabaseChanged,
  shipIntent,
  onShipIntentUsed,
}: {
  id: string;
  project: Project;
  analytics: any;
  onDatabaseChanged: () => void;
  shipIntent: ShipIntent | null;
  onShipIntentUsed: () => void;
}) {
  const [art, setArt] = useState<Artifacts | null>(null);
  const [error, setError] = useState("");
  // Whether a database is saved, and whether this download should carry it.
  const [dbSaved, setDbSaved] = useState(false);
  const [withEnv, setWithEnv] = useState(false);
  const onDb = useCallback(
    (s: DatabaseState) => {
      const saved = Boolean(s.saved?.length);
      setDbSaved(saved);
      if (!saved) setWithEnv(false);
      onDatabaseChanged();
    },
    [onDatabaseChanged],
  );

  // What ships (#79): refetched when the version does — a change kept, a version
  // restored — so this tab never describes a build that has moved on.
  const versionKey = `${project.current_version?.number ?? 0}:${project.status === "running" ? "r" : "s"}`;
  useEffect(() => {
    api.getArtifacts(id).then(setArt).catch((e) => setError(e.message));
  }, [id, versionKey]);

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
  const produced = producedByPhase(art);
  const shipped = PHASES.filter((ph) => produced.has(ph.key));
  const silent = PHASES.filter((ph) => !produced.has(ph.key));

  return (
    /* Shipping first: once a build is finished, putting it online (or into the
       person's GitHub) is what they came here for. Then what is here, how to run
       it, and a copy to take away. */
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <ShipCard
        // Reloaded when the version does (#79): it says which version Deploy sends.
        key={versionKey}
        id={id}
        defaultName={art.name || art.idea}
        intent={shipIntent}
        onIntentUsed={onShipIntentUsed}
        onShipped={onDatabaseChanged}
      />
      <div className="card">
        <div className="sec-head">
          <h2 className="label">Your project</h2>
          <span className="rule" />
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

        {/* Read off the artifacts, so this row can only ever claim what exists. */}
        <div className="deliver-made">
          <span className="label">What the agents produced</span>
          {shipped.length > 0 ? (
            <div className="deliverables">
              {shipped.map((ph) => {
                const files = produced.get(ph.key) ?? 0;
                return (
                  <span
                    key={ph.key}
                    className="badge badge-mono"
                    title={
                      `${AGENT_BY_KEY[ph.key].codename} · ${ph.name}` +
                      (files > 0
                        ? ` · ${files} ${files === 1 ? "file" : "files"} in the archive`
                        : "")
                    }
                  >
                    {ph.deliver}
                    {files > 0 && <b className="deliver-n">{files}</b>}
                  </span>
                );
              })}
            </div>
          ) : (
            <p className="field-hint" style={{ margin: 0 }}>
              No files or documents yet.
            </p>
          )}
          {/* "Produced nothing" is a verdict, and a phase that hasn't had its turn
              has not earned one. While the run can still reach them, they simply
              haven't got there yet. */}
          {shipped.length > 0 && silent.length > 0 && (
            <p className="field-hint" style={{ margin: 0 }}>
              {listOf(silent.map((ph) => AGENT_BY_KEY[ph.key].codename))}{" "}
              {stillRunning(art.status)
                ? `${silent.length === 1 ? "hasn’t" : "haven’t"} produced anything yet.`
                : `produced nothing in this run.`}
            </p>
          )}
        </div>
      </div>

      {/* What you are actually taking away. Deliver used to report `12` under
          "Source files" and offer no way to open one of them. */}
      {art.files.length > 0 && (
        <div className="card card-flush">
          <div className="card-head">
            <h2 className="label">The code</h2>
            <span className="rule" />
            <span className="mono dim" style={{ fontSize: "var(--t-xs)" }}>
              {art.files.length} files
            </span>
          </div>
          <div className="artifact-view artifact-files" style={{ height: 560 }}>
            <BuildLine
              state={art.build?.status ?? null}
              problems={art.build?.problems.length ?? 0}
              commands={art.scaffold?.commands ?? []}
              runs={art.build?.runs}
            />
            <FileBrowser files={artifactFiles(art.files)} />
          </div>
        </div>
      )}

      <div className="card">
        <div className="sec-head">
          <h2 className="label">Run it on your machine</h2>
          <span className="rule" />
        </div>
        {/* The platform's commands first: they are written against the manifests
            the platform itself put in the archive, so they are the ones that work.
            The team's own notes follow, as notes. */}
        {art.scaffold?.commands.length ? (
          <>
            <pre className="run-cmds mono">{art.scaffold.commands.join("\n")}</pre>
            {art.scaffold.notes.map((n, i) => (
              <p key={i} className="field-hint" style={{ margin: "8px 0 0" }}>
                {n}
              </p>
            ))}
          </>
        ) : null}
        {art.setup_instructions.length > 0 ? (
          <>
            {art.scaffold?.commands.length ? (
              <p className="label" style={{ margin: "18px 0 8px" }}>
                Notes from the team
              </p>
            ) : null}
            <div className="setup-list">
              {art.setup_instructions.map((s, i) => (
                <div key={i} className="setup-step">
                  <span className="n">{String(i + 1).padStart(2, "0")}</span>
                  <span>{s}</span>
                </div>
              ))}
            </div>
          </>
        ) : art.scaffold?.commands.length ? null : (
          <p className="muted" style={{ margin: 0, fontSize: "var(--t-base)", lineHeight: 1.6 }}>
            Setup steps appear once the Backend and DevOps phases have run. The .zip always includes
            a generated <code>README.md</code>.
          </p>
        )}
      </div>

      <DatabasePanel id={id} onChange={onDb} />

      <IntegrationsPanel id={id} />

      {/* The one download control in the app. The header carried a second copy
          of this button, which is how the same archive came to be offered twice. */}
      <div className="card">
        <div className="sec-head">
          <h2 className="label">Take a copy</h2>
          <span className="rule" />
        </div>
        <div className="deliver-take">
          <p className="muted" style={{ margin: 0, fontSize: "var(--t-base)", lineHeight: 1.6 }}>
            {hasOutput ? (
              <>
                A .zip of everything above: {art.files.length} source{" "}
                {art.files.length === 1 ? "file" : "files"}, {art.docs.length}{" "}
                {art.docs.length === 1 ? "document" : "documents"} and a generated{" "}
                <code>README.md</code>. Nothing leaves your machine.
                {art.version && project.change
                  ? ` This is v${art.version}: the change in progress isn't in it until you keep it.`
                  : ""}
              </>
            ) : (
              <>Nothing to download yet. No phase has produced a file or document.</>
            )}
          </p>
          {hasOutput && (
            <a className="btn btn-primary" href={api.downloadUrl(id, withEnv)} download>
              {Icon.download} {art.version ? `Download v${art.version}` : "Download .zip"}
            </a>
          )}
        </div>
        {hasOutput && dbSaved && (
          <div className="db-include">
            <label className="db-check">
              <input
                type="checkbox"
                checked={withEnv}
                onChange={(e) => setWithEnv(e.target.checked)}
                aria-describedby="db-include-hint"
              />
              <span>Include my database credentials in the download</span>
            </label>
            <p className="field-hint" id="db-include-hint">
              {withEnv
                ? "This download has a real backend/.env with your credentials. Never commit it. It's already in .gitignore."
                : "This download has backend/.env.example with placeholders only. GitHub always gets placeholders."}
            </p>
          </div>
        )}
        {/* Every version, in the card that takes a copy of one (#79). */}
        <Versions
          id={id}
          status={project.status}
          changeOpen={Boolean(project.change)}
          currentKey={versionKey}
          onRestored={onDatabaseChanged}
        />
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

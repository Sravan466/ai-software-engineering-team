"use client";

import { useState } from "react";
import {
  api,
  type AutoFixProblem,
  type AutoFixRound,
  type AutoFixTrack,
  type Project,
  type WaiveKind,
} from "@/lib/api";
import { PHASES } from "@/components/shell/phases";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { Icon } from "@/components/shell/icons";
import SecurityFindings from "./SecurityFindings";
import { ReasonKinds } from "./ReasonKinds";

/**
 * The crew fixing its own serious problems (#50).
 *
 * A leaked credential, code that doesn't compile, a phase built on the wrong
 * database: each has one right answer, so the crew fixes it, re-checks, and goes
 * again — and the person watching sees progress, not a question. Two surfaces:
 *
 *   FixingPanel — while the loop runs: which round, who is on which file, and where
 *                 each problem is on its way from queued to fixed.
 *   NeedsHelp   — when the loop stopped with something still wrong: what every round
 *                 tried, what is left, Keep trying first. Waiving is the exception,
 *                 behind More, with a reason kind on the record.
 */

const ORDER = PHASES.map((p) => p.key);

const STRATEGY: Record<AutoFixRound["strategy"], string> = {
  guided: "The findings, plus advice from the skills library",
  with_code: "The code, plus a note that the last try failed",
  stronger_model: "A full rewrite on the strongest model available",
};

/** A code round starts from the errors, not from a reviewer's advice. */
const CODE_STRATEGY: Record<AutoFixRound["strategy"], string> = {
  guided: "The errors, named file by file",
  with_code: "The broken code, plus a note that the last try failed",
  stronger_model: "A full rewrite on the strongest model available",
};

/**
 * A tests round (#76) is about who is asked: the engineer whose code a test checks,
 * with QA's suite kept and run again on the fix; or QA, once the engineer had a turn,
 * to decide whether the test or the code is wrong.
 */
function testApproach(strategy: AutoFixRound["strategy"], round?: AutoFixRound): string {
  const toQa = round?.phases.includes("qa_engineer");
  const who = toQa
    ? (round?.phases.length ?? 0) > 1
      ? "The owners fix the code; QA decides on the rest"
      : "QA decides whether the test or the code is wrong"
    : "The owner fixes the code; the same tests run again";
  return strategy === "stronger_model" ? `${who}, on the strongest model available` : who;
}

export function approach(track: string, strategy: AutoFixRound["strategy"], round?: AutoFixRound): string {
  if (track === "tests") return testApproach(strategy, round);
  return (track === "security" ? STRATEGY : CODE_STRATEGY)[strategy];
}


type Step = "queued" | "fixing" | "rechecking" | "fixed";
const STEPS: { key: Step; label: string }[] = [
  { key: "queued", label: "Queued" },
  { key: "fixing", label: "Fixing" },
  { key: "rechecking", label: "Re-checking" },
  { key: "fixed", label: "Fixed" },
];

export function trackEntries(project: Project): [string, AutoFixTrack][] {
  return Object.entries(project.auto_fix?.tracks ?? {});
}

function openRound(t: AutoFixTrack): AutoFixRound | null {
  const last = t.rounds[t.rounds.length - 1];
  return last && last.fixed === null ? last : null;
}

/** What a build problem broke, by the step that found it (#75). */
const BROKE: Record<string, string> = {
  install: "doesn't install",
  build: "fails the build",
  boot: "crashes on start",
  vercel: "failed on Vercel",
  test: "can't run as a test",
};

/** Where a real build found a problem, as a tag beside it. */
const FOUND_BY: Record<string, string> = {
  install: "Install",
  build: "Build",
  boot: "Start-up",
  vercel: "Vercel",
  test: "Test run",
};

/** A problem's first line as its title; a pasted log under it, as a terminal. */
function ProblemTitle({ title }: { title: string }) {
  const [head, ...rest] = title.split("\n");
  return (
    <>
      <b className="fix-item-title">{head}</b>
      {rest.length > 0 && <pre className="fix-item-tail mono">{rest.join("\n")}</pre>}
    </>
  );
}

function trackLabel(name: string): string {
  if (name === "security") return "security findings";
  if (name === "tests") return "the failing tests";
  const phase = name.replace(/^build:/, "");
  return `${AGENT_BY_KEY[phase]?.codename ?? phase}'s code`;
}

/** Where one problem is, read from where the run is. */
function stepFor(problem: AutoFixProblem, track: string, current: string | null): Step {
  const at = ORDER.indexOf(current ?? "");
  const owner = ORDER.indexOf(problem.phase);
  if (at < 0 || owner < 0) return "queued";
  if (at < owner) return "queued";
  if (at === owner) return "fixing";
  // A code fix is re-checked inside its own phase; a finding by the next audit; a
  // failing test by running the suite again, at QA.
  return track === "security" || track === "tests" ? "rechecking" : "fixed";
}

/** The rail every problem runs along. The label says the state; colour only agrees. */
function StepRail({ step }: { step: Step }) {
  const reached = STEPS.findIndex((s) => s.key === step);
  return (
    <span className="fix-rail" data-step={step}>
      <span className="fix-rail-track" aria-hidden="true">
        {STEPS.map((s, i) => (
          <span key={s.key} className="fix-rail-seg" data-on={i <= reached || undefined} />
        ))}
      </span>
      <span className="fix-rail-label">{STEPS[reached]?.label}</span>
    </span>
  );
}

function Owner({ phase }: { phase: string }) {
  const agent = AGENT_BY_KEY[phase];
  if (!agent) return <span className="finding-owner">{phase}</span>;
  return (
    <span className="finding-owner" style={{ ["--agent" as string]: agent.accent }}>
      <AgentSprite agent={agent} size={18} state="queued" />
      {agent.codename}
    </span>
  );
}

// ── while it runs ────────────────────────────────────────────────────────────
export function FixingPanel({ project }: { project: Project }) {
  if (project.status !== "running") return null;
  const live = trackEntries(project)
    .map(([name, t]) => [name, t, openRound(t)] as const)
    .filter((entry): entry is readonly [string, AutoFixTrack, AutoFixRound] => Boolean(entry[2]));
  if (live.length === 0) return null;

  return (
    <section className="fixing" aria-labelledby="fixing-title" aria-live="polite">
      {live.map(([name, t, round]) => {
        const count = round.problems.length;
        const fromVercel = round.source === "vercel";
        const onIt = round.phases.includes(project.current_phase ?? "")
          ? project.current_phase
          : null;
        const onFile = onIt
          ? round.problems.find((p) => p.phase === onIt && p.where)?.where
          : null;
        return (
          <div key={name} className="fixing-track">
            <header className="fixing-head">
              <span className="fixing-mark" aria-hidden="true">{Icon.rotate}</span>
              <div className="fixing-headings">
                <h2 id="fixing-title">
                  {fromVercel
                    ? `Fixing what Vercel rejected: ${count} problem${count === 1 ? "" : "s"}`
                    : name === "tests"
                      ? `Fixing ${count} failing test${count === 1 ? "" : "s"}`
                      : `Fixing ${count} serious ${name === "security" ? "issue" : "problem"}${count === 1 ? "" : "s"}`}
                </h2>
                <p>
                  {/* Counted within this episode: a fresh problem gets a fresh budget. */}
                  Round {round.n - (t.episode_start ?? 0)} of up to {t.allowed - (t.episode_start ?? 0)}
                  {onIt && (
                    <>
                      {" · "}
                      <b>{AGENT_BY_KEY[onIt]?.codename ?? onIt}</b> is on{" "}
                      {onFile ? <code className="mono">{onFile}</code> : "it"}
                    </>
                  )}
                  {!onIt && name === "security" && " · Warden is re-checking the rebuilt code"}
                  {!onIt && name === "tests" && " · SIEVE's suite runs again on the rebuilt code"}
                </p>
              </div>
              <span className="badge badge-run">
                <span className="dot dot-run dot-pulse" aria-hidden="true" />
                No decision needed
              </span>
            </header>
            <p className="fixing-approach">
              <span className="fixing-approach-label">This round</span>
              {approach(name, round.strategy, round)}
            </p>
            <ul className="fix-list">
              {round.problems.map((p) => (
                <li key={p.key} className="fix-item">
                  <div className="fix-item-main">
                    {p.severity && <span className="badge badge-bad">{p.severity}</span>}
                    <ProblemTitle title={p.title} />
                  </div>
                  <div className="fix-item-meta">
                    {p.kind === "test" && <span className="fix-item-step">Test</span>}
                    {p.step && <span className="fix-item-step">{FOUND_BY[p.step] ?? p.step}</span>}
                    {p.where && <code className="mono">{p.where}</code>}
                    <Owner phase={p.phase} />
                    <StepRail step={stepFor(p, name, project.current_phase)} />
                  </div>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}

// ── what every round tried ───────────────────────────────────────────────────
function RoundLedger({ name, track }: { name: string; track: AutoFixTrack }) {
  if (track.rounds.length === 0) {
    return (
      <p className="field-hint ledger-empty">
        Automatic fixing is switched off, so no round ran.
      </p>
    );
  }
  return (
    <ol className="ledger">
      {track.rounds.map((r) => {
        const sent = r.problems.length;
        const fixed = r.fixed?.length ?? 0;
        const who = r.phases.map((p) => AGENT_BY_KEY[p]?.codename ?? p).join(", ");
        return (
          <li key={r.n} className="ledger-row" data-progress={fixed > 0 || undefined}>
            <span className="ledger-n mono">R{r.n}</span>
            <div className="ledger-body">
              <span className="ledger-what">
                Sent {sent} to {who}
              </span>
              <span className="ledger-how">{approach(name, r.strategy, r)}</span>
            </div>
            <span className={`badge ${fixed > 0 ? "badge-ok" : "badge"}`}>
              {r.fixed === null ? "Not re-checked" : `Fixed ${fixed} of ${sent}`}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

// ── when the loop stopped ────────────────────────────────────────────────────
export function NeedsHelp({
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
  const stuck = trackEntries(project).filter(([, t]) => t.stopped && !t.accepted);
  const security = stuck.find(([name]) => name === "security");
  const code = stuck.filter(([name]) => name !== "security");
  const [more, setMore] = useState(false);
  const [kind, setKind] = useState<WaiveKind | null>(null);
  const [reason, setReason] = useState("");
  const [tick, setTick] = useState(0);
  const [securityLeft, setSecurityLeft] = useState<number | null>(null);

  // After every serious finding has been waived there is nothing left to try, so the
  // main action becomes carrying on — same request, honest label.
  const nothingToFix = code.length === 0 && securityLeft === 0;

  return (
    <div className="help">
      {stuck.map(([name, t]) => (
        <div key={name} className="help-track">
          <h3 className="help-title">
            What the crew tried on {trackLabel(name)}
            <span className="field-hint">
              {t.stopped?.reason === "no_progress"
                ? "Stopped early. The last round fixed nothing."
                : `Stopped after ${t.rounds.length - (t.episode_start ?? 0)} round${t.rounds.length - (t.episode_start ?? 0) === 1 ? "" : "s"}.`}
            </span>
          </h3>
          <RoundLedger name={name} track={t} />
          {name !== "security" && <CodeLeft track={t} />}
        </div>
      ))}

      {security && (
        <div className="help-track">
          <h3 className="help-title">Still open</h3>
          <SecurityFindings
            key={tick}
            id={id}
            busy={busy}
            act={act}
            scope="serious"
            allowWaive={more}
            onChange={() => setTick((n) => n + 1)}
            onCount={setSecurityLeft}
          />
        </div>
      )}

      <div className="decision-act">
        <div className="decision-approve">
          <button
            className="btn btn-lg btn-accent"
            disabled={busy}
            onClick={() => act(() => api.keepTrying(id))}
          >
            {busy && <span className="btn-spinner" aria-hidden="true" />}
            {nothingToFix ? Icon.arrowRight : Icon.rotate}
            {nothingToFix ? "Continue the build" : "Keep trying"}
          </button>
          <button
            className="btn btn-lg btn-danger"
            disabled={busy}
            onClick={() => act(() => api.stop(id, "Stopped after the crew asked for help."))}
          >
            {Icon.stop} Stop the build
          </button>
          <span className="field-hint">
            {nothingToFix
              ? "Every serious finding is waived, so the build can go on."
              : "Keep trying gives the crew up to two more rounds."}
          </span>
        </div>

        <div className="help-more">
          <button
            className="btn btn-sm btn-ghost"
            aria-expanded={more}
            aria-controls="help-more-body"
            onClick={() => setMore((v) => !v)}
          >
            <span className="help-more-chev" data-open={more || undefined} aria-hidden="true">
              {Icon.chevron}
            </span>
            More
          </button>
          {more && (
            <div id="help-more-body" className="help-more-body">
              {security && (
                <p className="field-hint">
                  Each finding above now has a Waive button. Waiving a serious one needs a
                  reason and a reason kind, so there&apos;s a record of why it shipped.
                </p>
              )}
              {code.length > 0 && (
                <AcceptForm
                  kind={kind}
                  setKind={setKind}
                  reason={reason}
                  setReason={setReason}
                  busy={busy}
                  onAccept={() =>
                    kind && act(() => api.acceptProblems(id, kind, reason.trim()))
                  }
                />
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CodeLeft({ track }: { track: AutoFixTrack }) {
  const last = track.rounds[track.rounds.length - 1];
  const left = last
    ? last.problems.filter((p) => (last.remaining ?? []).includes(p.key))
    : [];
  if (left.length === 0) return null;
  return (
    <ul className="fix-list">
      {left.map((p) => (
        <li key={p.key} className="fix-item" data-left>
          <div className="fix-item-main">
            <span className="badge badge-bad">
              {p.kind === "stack"
                ? "contradicts the stack"
                : p.kind === "test"
                  ? "still fails"
                  : BROKE[p.step ?? ""] ?? "doesn't compile"}
            </span>
            <ProblemTitle title={p.title} />
          </div>
          {p.where && (
            <div className="fix-item-meta">
              <code className="mono">{p.where}</code>
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function AcceptForm({
  kind,
  setKind,
  reason,
  setReason,
  busy,
  onAccept,
}: {
  kind: WaiveKind | null;
  setKind: (k: WaiveKind) => void;
  reason: string;
  setReason: (r: string) => void;
  busy: boolean;
  onAccept: () => void;
}) {
  const ready = Boolean(kind) && reason.trim().length >= 3;
  return (
    <div className="accept-form">
      <b className="accept-title">Continue without these fixed</b>
      <ReasonKinds name="accept-kind" kind={kind} setKind={setKind} />
      <div className="field">
        <label htmlFor="accept-reason">What should someone reading this build later know?</label>
        <input
          id="accept-reason"
          className="input"
          placeholder="e.g. the failing file is a generated stub we replace by hand"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </div>
      <button className="btn btn-sm btn-danger" disabled={busy || !ready} onClick={onAccept}>
        Continue and record the waiver
      </button>
    </div>
  );
}

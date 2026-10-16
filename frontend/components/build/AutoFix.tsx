"use client";

import { useState } from "react";
import Link from "next/link";
import {
  api,
  type AutoFixProblem,
  type AutoFixRound,
  type AutoFixTrack,
  type KeptBest,
  type Project,
  type TrendPoint,
  type WaiveKind,
} from "@/lib/api";
import { PHASES } from "@/components/shell/phases";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { Icon } from "@/components/shell/icons";
import SecurityFindings, { ruleName } from "./SecurityFindings";
import { ReasonKinds } from "./ReasonKinds";
import { CrewTalk } from "./CrewTalk";

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
 *                 tried, what is left, Try again first. Waiving is the exception,
 *                 behind More, with a reason kind on the record.
 */

const ORDER = PHASES.map((p) => p.key);

const STRATEGY: Record<AutoFixRound["strategy"], string> = {
  guided: "The findings, plus advice from the skills library",
  with_code: "The code, plus a note that the last try failed",
  stronger_model: "The code, on the strongest model it can reach",
};

/** A code round starts from the errors, not from a reviewer's advice. */
const CODE_STRATEGY: Record<AutoFixRound["strategy"], string> = {
  guided: "The errors, named file by file",
  with_code: "The broken code, plus a note that the last try failed",
  stronger_model: "The broken code, on the strongest model it can reach",
};

/** `anthropic:claude-x` → `claude-x`: the model, without where it runs. */
function modelName(model: string): string {
  return model.includes(":") ? model.slice(model.indexOf(":") + 1) : model;
}

/** Who a cloud model's provider is, in a sentence. */
const PROVIDERS: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google" };

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
  return strategy === "stronger_model" ? `${who}, on the strongest model it can reach` : who;
}

/**
 * What a round tried, by what ran (#103): a round the person sent to a model of their
 * choosing says so, and a stronger round names the model it ran on.
 */
export function approach(track: string, strategy: AutoFixRound["strategy"], round?: AutoFixRound): string {
  const base = track === "tests" ? testApproach(strategy, round) : (track === "security" ? STRATEGY : CODE_STRATEGY)[strategy];
  if (round?.basis === "asked") return `${base.split(",")[0]}, on the model you chose for this fix`;
  if (round?.basis === "asked_unreached") return `${base.split(",")[0]}. The model you chose couldn't be reached`;
  if (round?.basis === "escalated_unreached") return `${base.split(",")[0]}. The stronger model couldn't be reached`;
  if (strategy === "stronger_model" && track.startsWith("build:") && round && round.fixed != null && !("basis" in round)) {
    // A code round closed before rounds said what they ran on (#103): it was sent for
    // a stronger model, and on a one-model account it ran on the same one. Say what
    // the loop asked for, not what it can't know ran.
    return `${base.split(",")[0]}, sent for a stronger model (which one ran wasn't recorded)`;
  }
  return base;
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

/**
 * A code track's problem counts (#103), one point per attempt — worked out by the
 * backend as the page reads the build (`autofix.view`), older builds included.
 */
export function trendOf(name: string, t: AutoFixTrack): TrendPoint[] {
  return name.startsWith("build:") && t.trend && t.trend.length > 1 ? t.trend : [];
}

/** Which attempt the build was put back on, in words. */
function keptWhich(kept: KeptBest): string {
  return kept.round > 0 ? `what round ${kept.round} made` : "the attempt before any round";
}

/**
 * The trend as a run of numbers (#103): fewer than the step before reads as better,
 * more as worse — in words for a screen reader, in colour only as agreement. A step the
 * loop put back reads "back to", and the attempt the build is on now is ringed.
 */
function Trend({ points, kept }: { points: TrendPoint[]; kept?: (KeptBest & { from?: string }) | null }) {
  if (points.length < 2) return null;
  const keptAt = kept?.from ? points.findIndex((p) => p.attempt === kept.from) : -1;
  const said =
    "Problems: " + points.map((p) => (p.back ? `back to ${p.n}` : String(p.n))).join(", then ") + "." +
    (kept ? ` The build is back on ${keptWhich(kept)}, with ${kept.problems}.` : "");
  return (
    <p className="fix-trend">
      <span className="sr-only">{said}</span>
      <span className="fix-trend-label" aria-hidden="true">Problems</span>
      <span className="fix-trend-steps" aria-hidden="true">
        {points.map((p, i) => {
          const prev = points[i - 1];
          const dir = !prev || p.back ? undefined : p.n < prev.n ? "down" : p.n > prev.n ? "up" : "same";
          return (
            <span key={i} className="fix-trend-step" style={{ ["--i" as string]: i }}>
              {i > 0 && <span className="fix-trend-arrow">{p.back ? Icon.undo : Icon.arrowRight}</span>}
              <span className="fix-trend-n" data-dir={dir} data-back={p.back || undefined} data-kept={keptAt === i || undefined}>
                {p.n}
              </span>
            </span>
          );
        })}
      </span>
    </p>
  );
}

/** A round's result, net (#103): what it fixed and what it broke, not "fixed 3 of 3". */
function roundResult(r: AutoFixRound): { text: string; tone: "ok" | "bad" | "warn" | "plain" } {
  const sent = r.problems.length;
  const fixed = r.fixed?.length ?? 0;
  if (r.fixed === null) return { text: "Not re-checked", tone: "plain" };
  if (r.unjudged && fixed === 0) return { text: "Couldn't re-check", tone: "warn" };
  if (r.nothing_sent) return { text: "Too long to show", tone: "warn" };
  // Security and test rounds, and code rounds nobody can know the result of, as before.
  if (r.after === undefined) return { text: `Fixed ${fixed} of ${sent}`, tone: fixed > 0 ? "ok" : "plain" };
  const broke = r.broke ?? 0;
  const text = broke > 0 ? `Fixed ${fixed}, broke ${broke}` : `Fixed ${fixed} of ${sent}`;
  return { text, tone: r.after < sent ? "ok" : r.after > sent ? "bad" : "plain" };
}

/** What a build problem broke, by the step that found it (#75). */
const BROKE: Record<string, string> = {
  install: "doesn't install",
  build: "fails the build",
  boot: "crashes on start",
  vercel: "failed on Vercel",
  test: "can't run as a test",
};

/** Code that parses and isn't finished (#101), said as that rather than "doesn't compile". */
const UNFINISHED: Record<NonNullable<AutoFixProblem["problem"]>, string> = {
  placeholder: "unfinished",
  weak_test: "tests nothing",
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

/** What the platform fixed itself across the episode, without a model (#103). */
function platformFixed(t: AutoFixTrack): { paths: number; imports: number } {
  const out = { paths: 0, imports: 0 };
  // Only what the build is on now: after a put-back, the rounds after the kept attempt
  // were set aside, their mends with them.
  const kept = t.stopped?.kept_best?.round;
  t.rounds.slice(t.episode_start ?? 0).forEach((r, i) => {
    const counted = i === 0 ? [r.platform_before] : [];
    if (kept === undefined || kept === null || i + 1 <= kept) counted.push(r.platform);
    for (const p of counted) {
      out.paths += p?.paths ?? 0;
      out.imports += p?.imports ?? 0;
    }
  });
  return out;
}

/** "The platform fixed 3 import paths and 1 import itself, without a model." */
function PlatformLine({ track }: { track: AutoFixTrack }) {
  const { paths, imports } = platformFixed(track);
  if (paths + imports === 0) return null;
  const parts = [
    paths ? `${paths} import path${paths === 1 ? "" : "s"}` : "",
    imports ? `${imports} import${imports === 1 ? "" : "s"}` : "",
  ].filter(Boolean);
  return (
    <p className="fix-kept fix-platform">
      {Icon.check}
      <span>The platform fixed {parts.join(" and ")} itself, without a model.</span>
    </p>
  );
}

/** The trend so far, under "This round" while a fix runs (#103). */
function FixingTrend({ name, track }: { name: string; track: AutoFixTrack }) {
  const points = trendOf(name, track);
  const { paths, imports } = platformFixed(track);
  if (points.length < 2 && paths + imports === 0) return null;
  return (
    <div className="fixing-trend">
      <Trend points={points} />
      <PlatformLine track={track} />
    </div>
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
                      <b>{AGENT_BY_KEY[onIt]?.codename ?? onIt}</b>{" "}
                      {name === "tests" && onIt !== "qa_engineer" ? (
                        // The engineer fixes the code a test checks, never the test.
                        <>
                          is fixing what {onFile ? <code className="mono">{onFile}</code> : "the failing test"} checks
                        </>
                      ) : (
                        <>is on {onFile ? <code className="mono">{onFile}</code> : "it"}</>
                      )}
                    </>
                  )}
                  {!onIt && name === "security" && " · The scanners run again on the rebuilt code"}
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
            <FixingTrend name={name} track={t} />
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
                    {/* The rule the rescan runs again (#77). */}
                    {p.tool && <span className="fix-item-step">{p.tool}</span>}
                    {p.rule_id && (
                      <code className="mono" title={p.rule_id}>
                        {ruleName(p.rule_id, p.tool)}
                      </code>
                    )}
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
        const who = r.phases.map((p) => AGENT_BY_KEY[p]?.codename ?? p).join(", ");
        const result = roundResult(r);
        return (
          <li key={r.n} className="ledger-row" data-progress={result.tone === "ok" || undefined} data-worse={result.tone === "bad" || undefined}>
            <span className="ledger-n mono">R{r.n}</span>
            <div className="ledger-body">
              <span className="ledger-what">
                Sent {sent} to {who}
                {/* The model's own name as it answered — tags like `:7b` are part of it. */}
                {r.model && <span className="ledger-model mono"> on {r.model}</span>}
              </span>
              <span className="ledger-how">{approach(name, r.strategy, r)}</span>
              {r.note && (
                <span className="ledger-note">
                  You said: “{r.note}”
                  {r.decided && r.note_sent !== false && " Kept as a decision, so this round could drop what it leaves out."}
                  {r.note_sent === false && " The model didn't see it: there was no room beside the code."}
                  {r.note_sent === "partly" && " Only some of this round's calls had room for it."}
                </span>
              )}
            </div>
            <span
              className={`badge${result.tone === "plain" ? "" : ` badge-${result.tone}`}`}
              title={r.unjudged}
            >
              {result.text}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

// ── when the loop stopped ────────────────────────────────────────────────────
/** Who a track's problems belong to, in a person's words. */
type Side = { name: string; plural?: boolean };
const SIDE: Record<string, Side> = {
  backend_engineer: { name: "the backend" },
  frontend_engineer: { name: "the website" },
  qa_engineer: { name: "the tests", plural: true },
};

function sideOf(name: string): Side {
  const phase = name.replace(/^build:/, "");
  return SIDE[phase] ?? { name: `${AGENT_BY_KEY[phase]?.codename ?? phase}'s work` };
}

/** "the backend's", "the tests'". */
function whose(side: Side): string {
  return side.name.endsWith("s") ? `${side.name}'` : `${side.name}'s`;
}

/** What is left on a stopped track (#103), new problems included; older records only
 * kept what the last round was sent, less what it fixed. */
function leftOf(track: AutoFixTrack): AutoFixProblem[] {
  const last = track.rounds[track.rounds.length - 1];
  return track.stopped?.problems ?? (last ? last.problems.filter((p) => (last.remaining ?? []).includes(p.key)) : []);
}

function capital(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Wiring: one file disagreeing with another about a name, a default, or a path. */
const WIRING = /no exported member|no default export|has no exported|imports `?\.|no file in this build|never imported or declared|declares .* locally/i;

/**
 * The headline a person reads first (#103): what is wrong, in product terms, from
 * the kinds of problem left. The compiler's own words are under "Details for
 * developers".
 */
function headline(name: string, t: AutoFixTrack): string {
  if (name === "security") return "A security problem is still open.";
  if (name === "tests") return "Some of the app's tests fail.";
  const side = sideOf(name);
  const { plural } = side;
  const kinds = leftOf(t).map((p): string => {
    if (p.kind === "stack") return "stack";
    if (p.problem === "weak_test") return "weak";
    if (p.problem === "placeholder") return "unfinished";
    if (p.step === "vercel") return "vercel";
    if (p.step === "install") return "install";
    if (p.step === "build" || p.step === "boot") return "start";
    if (/does not parse/i.test(p.title)) return "parse";
    if (/not a package the platform can install|is not a package on npm/i.test(p.title)) return "install";
    if (WIRING.test(p.title)) return "wiring";
    return "other";
  });
  const counts = new Map<string, number>();
  for (const k of kinds) counts.set(k, (counts.get(k) ?? 0) + 1);
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "other";
  switch (top) {
    case "wiring":
      return `${capital(whose(side))} files don't connect to each other yet.`;
    case "parse":
      return `Some of ${whose(side)} code can't run as written.`;
    case "unfinished":
      return plural ? `Some of ${side.name} aren't finished.` : `Part of ${side.name} isn't finished.`;
    case "weak":
      return "Some tests don't check anything yet.";
    case "install":
      return `${capital(side.name)} ${plural ? "use" : "uses"} a package that can't be installed.`;
    case "stack":
      return plural
        ? `Some of ${side.name} use a different setup from the plan.`
        : `Part of ${side.name} uses a different setup from the plan.`;
    case "start":
      return plural ? `${capital(side.name)} don't run yet.` : `${capital(side.name)} doesn't start yet.`;
    case "vercel":
      return "The website didn't build on Vercel.";
    default:
      return `${capital(side.name)} ${plural ? "have" : "has"} problems the crew couldn't fix.`;
  }
}

/** The compiler's own words, for whoever wants them (#103). */
function Details({ track }: { track: AutoFixTrack }) {
  const [open, setOpen] = useState(false);
  if (leftOf(track).length === 0) return null;
  return (
    <div className="help-details">
      <button
        className="btn btn-sm btn-ghost"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="help-more-chev" data-open={open || undefined} aria-hidden="true">
          {Icon.chevron}
        </span>
        Details for developers
      </button>
      {open && (
        <div className="help-details-body">
          <CodeLeft track={track} />
        </div>
      )}
    </div>
  );
}

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
  const [advanced, setAdvanced] = useState(false);
  const [note, setNote] = useState("");
  const [kind, setKind] = useState<WaiveKind | null>(null);
  const [reason, setReason] = useState("");
  const [tick, setTick] = useState(0);
  const [securityLeft, setSecurityLeft] = useState<number | null>(null);

  // After every serious finding has been waived there is nothing left to try, so the
  // main action becomes carrying on — same request, honest label.
  const nothingToFix = code.length === 0 && securityLeft === 0;
  // What would help (#103): a cloud model for one fix, with the person's yes; or, with
  // nothing stronger to reach, a bigger model added in Settings.
  const codeTracks = code.filter(([name]) => name.startsWith("build:"));
  const cloud = codeTracks.map(([, t]) => t.stopped?.cloud).find((m): m is string => Boolean(m)) ?? null;
  // Whose code goes to the cloud: every stuck side it was offered for.
  const cloudSides = codeTracks.filter(([, t]) => cloud && t.stopped?.cloud === cloud).map(([name]) => sideOf(name));
  // Only when the record says adding a model is what would help: nothing stronger to
  // reach and no cloud key at all. A build stopped before this was recorded says nothing.
  const nothingStronger = !cloud && codeTracks.some(([, t]) => t.stopped?.add_model === true);
  // Try again takes the box's draft along as the next round's note — pressing it is the
  // person saying so, and its label says it ("Try again with your note"). It goes as a
  // message, so the conversation shows it; a cloud round takes it along as before.
  const draftNote = !nothingToFix && note.trim() ? note.trim() : undefined;
  const retry = async (model?: string) => {
    const ok = await act(() =>
      // With a draft, through the conversation — the cloud button too — so it's kept there
      // and read as the person meant it (a decision about the app, #102).
      draftNote ? api.sendMessage(id, draftNote, { keepTrying: true, model }) : api.keepTrying(id, { model }),
    );
    if (ok && draftNote) setNote("");
  };

  return (
    <div className="help">
      {stuck.map(([name, t]) => (
        <div key={name} className="help-track">
          <h3 className="help-title">
            {headline(name, t)}
            <span className="field-hint">{stoppedHint(t)}</span>
          </h3>
          <Trend points={trendOf(name, t)} kept={t.stopped?.kept_best} />
          <PlatformLine track={t} />
          {t.stopped?.kept_best && (
            <p className="fix-kept">
              {Icon.undo}
              <span>
                Back on {keptWhich(t.stopped.kept_best)}: {t.stopped.kept_best.problems} problem
                {t.stopped.kept_best.problems === 1 ? "" : "s"}, not {t.stopped.kept_best.instead_of}. The rounds after
                it made things worse.
              </span>
            </p>
          )}
          <RoundLedger name={name} track={t} />
          {name !== "security" && <Details track={t} />}
        </div>
      ))}

      {security && (
        <div className="help-track">
          <h3 className="help-title">
            Still open
            <span className="field-hint">What the last rescan still reports, by rule and line.</span>
          </h3>
          <SecurityFindings
            key={tick}
            id={id}
            busy={busy}
            act={act}
            scope="serious"
            allowWaive={advanced}
            onChange={() => setTick((n) => n + 1)}
            onCount={setSecurityLeft}
          />
        </div>
      )}

      {nothingStronger && (
        <p className="fix-kept help-model">
          {Icon.info}
          <span>
            Bigger models fix this kind of problem more often. Add a cloud key or a bigger local model
            in <Link href="/settings">Settings</Link>.
          </span>
        </p>
      )}

      <div className="decision-act">
        {/* The build page's one box (#102): a note here starts the next round with it,
            a question is answered straight away. The cloud button sends the draft too. */}
        <CrewTalk
          project={project}
          id={id}
          busy={busy}
          act={act}
          place={nothingToFix ? "gate" : "stuck"}
          embedded
          text={note}
          setText={setNote}
        />
        <div className="decision-approve">
          <button className="btn btn-lg btn-accent" disabled={busy} onClick={() => retry()}>
            {busy && <span className="btn-spinner" aria-hidden="true" />}
            {nothingToFix ? Icon.arrowRight : Icon.rotate}
            {nothingToFix ? "Continue the build" : draftNote ? "Try again with your note" : "Try again"}
          </button>
          {cloud && (
            <button className="btn btn-lg btn-ghost" disabled={busy} onClick={() => retry(cloud)}>
              {Icon.sparkle} Use {modelName(cloud)} for this fix
            </button>
          )}
          {nothingStronger && (
            <Link className="btn btn-lg btn-ghost" href="/settings">
              {Icon.plus} Add a model
            </Link>
          )}
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
              : "Try again gives the crew up to 2 more rounds."}
            {cloud && cloudSides.length > 0 && (
              <>
                {" "}Using {modelName(cloud)} sends {cloudSides.map(whose).join(" and ")} code to{" "}
                {PROVIDERS[cloud.split(":")[0]] ?? cloud.split(":")[0]}, for this round only.
              </>
            )}
          </span>
        </div>

        <div className="help-more">
          <button
            className="btn btn-sm btn-ghost"
            aria-expanded={advanced}
            aria-controls="help-more-body"
            onClick={() => setAdvanced((v) => !v)}
          >
            <span className="help-more-chev" data-open={advanced || undefined} aria-hidden="true">
              {Icon.chevron}
            </span>
            Advanced: continue with these problems
          </button>
          {advanced && (
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

/** Why the loop stopped, in a line. */
function stoppedHint(t: AutoFixTrack): string {
  const last = t.rounds[t.rounds.length - 1];
  if (t.stopped?.reason === "unchecked") return "Stopped. The scanners couldn't run again to check the fix.";
  if (t.stopped?.reason === "no_progress") {
    // Net (#103): it fixed some, and left as many or more.
    if (last?.after !== undefined && (last.fixed?.length ?? 0) > 0) {
      return `Stopped early. The last round left ${last.after} where it started with ${last.problems.length}.`;
    }
    return "Stopped early. The last round fixed nothing.";
  }
  const n = t.rounds.length - (t.episode_start ?? 0);
  return `Stopped after ${n} round${n === 1 ? "" : "s"}.`;
}

function CodeLeft({ track }: { track: AutoFixTrack }) {
  const left = leftOf(track);
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
                  : p.problem
                    ? UNFINISHED[p.problem]
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

"use client";

import { useEffect, useRef, useState, type CSSProperties, type MutableRefObject, type ReactNode } from "react";
import type { Activity, ActivityStep, Project } from "@/lib/api";
import { AGENT_BY_KEY, type Persona } from "@/components/agents/personas";
import { Icon } from "@/components/shell/icons";
import { FilePath, FileProgress, activityFor } from "@/components/build/CodeWriting";
import { plural } from "@/lib/text";

/**
 * The running phase's row, told as the steps it is taking (#86).
 *
 * The row used to carry a blinking voice line, a sweeping bar and a ticking clock —
 * proof that something was happening, and nothing about what. It now reads the way
 * an agent chat does: finished steps settle into quiet past-tense lines ("Planned 2
 * files", "Wrote frontend/lib/sum.ts", "Ran npm install"), the plan says in a
 * sentence what it's for, the step in hand shimmers in the agent's colour, and three
 * dots say more is coming.
 *
 * All of it comes from the poll: `activity.trail` is kept by the server, so a command
 * that ran between two polls — or before a reload — is still here. A phase that
 * reports no steps is one line until it's done; nothing is invented to fill the gap.
 */

/** `held`: the step in hand when the run stopped under it — kept, but not moving. */
type LineState = "done" | "live" | "held" | "stopped" | "waiting";

type Line = {
  /** Stable across polls, so a live line that finishes *becomes* its done line. */
  id: string;
  state: LineState;
  /** The words that shimmer while the step runs. */
  title: string;
  /** What a live step's words become once it's done, if they change. */
  past?: string;
  /** Quiet words after it: a file. */
  object?: ReactNode;
  /** A command, in mono. */
  chip?: string;
  count?: string;
  /** Something wrong with a finished step, said beside it. */
  flag?: string;
  /** A sentence under the line, typed in when it first appears. */
  message?: string;
  /** What the chevron opens. */
  panel?: ReactNode;
};

// Why the step in hand stopped, when nobody is generating any more. `cancelled` is
// deliberately absent: Stop can't interrupt the model call in flight, so a cancelled
// run is still finishing its step, and says so beneath it.
const STOPPED_VERB: Record<string, string> = {
  stalled: "Stopped responding mid-phase",
  failed: "The run failed mid-phase",
};

/** How many finished files show by name; the rest are counted. */
const FILES_SHOWN = 8;

const FILE_DONE: Record<string, string> = { ok: "Wrote", failed: "Wrote", missing: "Couldn't write" };

/** A step the phase has finished, as it reads once it's done. */
function finishedStep(t: ActivityStep): Pick<Line, "title" | "chip"> {
  switch (t.stage) {
    case "planning":
      return { title: t.total > 0 ? `Planned ${plural(t.total, "file")}` : "The plan listed no files" };
    case "writing":
      return { title: t.total > 0 ? `Wrote ${plural(t.done, "file")}` : "Wrote it in one reply" };
    case "fixing":
      return { title: "Fixed the files the build flagged" };
    case "changing":
      return { title: "Made the change asked for on the preview" };
    case "checking":
      return { title: "Checked the build" };
    case "building":
    case "testing":
    case "scanning":
      return t.detail
        ? { title: "Ran", chip: t.detail }
        : { title: t.stage === "testing" ? "Ran the tests" : t.stage === "scanning" ? "Ran the scanners" : "Ran the build" };
    case "reviewing":
      return { title: "Reviewed what the scanners can't see" };
    default:
      return { title: t.stage };
  }
}

/** The step in hand. */
function currentStep(a: Activity): Pick<Line, "title" | "past" | "object" | "chip" | "count"> {
  switch (a.stage) {
    case "":
      return { title: "Setting up", past: "Set up" };
    case "planning":
      return { title: "Planning the files", past: "Planned the files" };
    case "changing":
      // "Ask the crew" on the app preview (#78): one file, changed in place.
      return a.detail
        ? { title: "Changing", past: "Changed", object: <FilePath path={a.detail} /> }
        : { title: "Changing the file", past: "Changed the file" };
    case "writing":
    case "fixing":
      if (!a.detail) {
        return a.stage === "fixing" ? { title: "Fixing", past: "Fixed" } : { title: "Writing it in one reply", past: "Wrote it in one reply" };
      }
      return {
        title: a.stage === "fixing" ? "Fixing" : "Writing",
        past: a.stage === "fixing" ? "Fixed" : "Wrote",
        object: <FilePath path={a.detail} />,
        count: a.total > 0 ? `${Math.min(a.done + 1, a.total)} of ${a.total}` : undefined,
      };
    case "checking":
      return { title: "Checking the build", past: "Checked the build" };
    case "building":
    case "testing":
    case "scanning":
      return a.detail ? { title: "Running", past: "Ran", chip: a.detail } : { title: a.label || "Running it", past: "Ran it" };
    case "reviewing":
      // Warden's model call, once the scanners have run (#77).
      return { title: "Reviewing what the scanners can't see", past: "Reviewed what the scanners can't see" };
    default:
      return { title: a.label || "Working" };
  }
}

/**
 * The step in hand as plain words, for places with no room for the feed: the crew
 * floor's bubble and nameplate (#91). The same step the feed's live line shows.
 */
export function stepWords(a: Activity): { title: string; path?: string; chip?: string; count?: string } {
  if (a.ended) return { title: "Wrapping up" };
  const s = currentStep(a);
  const onFile = a.stage === "writing" || a.stage === "fixing" || a.stage === "changing";
  return { title: s.title, path: onFile && a.detail ? a.detail : undefined, chip: s.chip, count: s.count };
}

/**
 * The files that have landed, one line each, in plan order — the latest few by name.
 * While the phase is still writing, the file in hand is the live line instead; once
 * it has moved on (`settled`), a file being fixed is still one that was written.
 */
function fileLines(a: Activity, id: (part: string) => string, settled: boolean): Line[] {
  const landed = a.files.filter((f) => f.state in FILE_DONE || (settled && (f.state === "writing" || f.state === "fixing")));
  const shown = landed.length > FILES_SHOWN ? landed.slice(-(FILES_SHOWN - 1)) : landed;
  const lines: Line[] = [];
  if (shown.length < landed.length) {
    lines.push({ id: id("files-earlier"), state: "done", title: `Wrote ${plural(landed.length - shown.length, "earlier file")}` });
  }
  for (const f of shown) {
    lines.push({
      id: id(`file:${f.path}`),
      state: "done",
      title: FILE_DONE[f.state] ?? "Wrote",
      object: <FilePath path={f.path} />,
      flag: f.state === "failed" ? "still has problems" : undefined,
    });
  }
  return lines;
}

/** The phase's lines: what it has done, then what it's doing. Pure. */
export function stepsFor(project: Project, key: string, agent: Persona, state: string): Line[] {
  // Prefixed with the project, so a page reused for another build never mistakes
  // that build's lines for ones it has already shown.
  const id = (part: string | number) => `${project.id}:${key}:${part}`;
  const a = activityFor(project, key);
  const lines: Line[] = [];

  if (!a) {
    lines.push({ id: id(0), state: "live", title: agent.steps.doing, past: agent.steps.done });
  } else {
    const trail = a.trail ?? [];
    const dropped = a.dropped ?? 0;
    // A code phase plans first, and keeps its file list even after the planning step
    // has fallen off the front of the trail. A phase that reports only its last part
    // (QA's test run) did its writing first, and says so.
    const planned = a.files.length > 0 || a.stage === "planning" || trail.some((t) => t.stage === "planning");
    const wroteInTrail = trail.some((t) => t.stage === "writing");
    // Writing comes straight after planning, so once planning has fallen off the front
    // of the trail, writing is over too even if its own entry went with it.
    const settled = wroteInTrail || (dropped > 0 && !trail.some((t) => t.stage === "planning"));
    let n = 0;
    let filed = false;
    const files = () => {
      if (!filed && a.files.length) lines.push(...fileLines(a, id, settled));
      filed = true;
    };

    if (!planned) lines.push({ id: id(n++), state: "done", title: agent.steps.done });
    // Numbered from the phase's first step, not the trail's, so a step falling off the
    // front doesn't shift every id after it.
    if (dropped > 0) lines.push({ id: id("earlier"), state: "done", title: plural(dropped, "earlier step") });
    n += dropped;
    if (settled && !wroteInTrail) files();

    for (const t of trail) {
      const at = n++;
      if (t.stage === "writing" && a.files.length) {
        files();
        continue;
      }
      const isPlan = t.stage === "planning";
      lines.push({
        id: id(at),
        state: "done",
        ...finishedStep(t),
        message: isPlan && a.note ? a.note : undefined,
        panel: isPlan && a.files.length ? <FileProgress activity={a} /> : undefined,
      });
    }

    if (a.ended) {
      // Stopped reporting, row not saved yet: every step is done, the phase is closing.
      lines.push({ id: id(n), state: "live", title: "Wrapping up", past: "Wrapped up" });
    } else if ((a.stage === "writing" || a.stage === "fixing") && !settled) {
      files();
      // The file in hand has the id its finished line will have, so it settles in place
      // — unless it has already landed and has that line (the poll caught the moment
      // between its check and the next call).
      const own = a.detail ? id(`file:${a.detail}`) : null;
      const landed = own !== null && lines.some((l) => l.id === own);
      lines.push({ id: own && !landed ? own : id(n), state: "live", ...currentStep(a) });
    } else {
      lines.push({ id: id(n), state: "live", ...currentStep(a) });
    }
  }

  // The step in hand keeps its words in every ending, so you can still read which
  // command it was on; what happened to the run is said on its own line beneath.
  const tail = lines[lines.length - 1];
  if (STOPPED_VERB[state]) {
    tail.state = "held";
    lines.push({ id: id("stopped"), state: "stopped", title: STOPPED_VERB[state] });
  } else if (state === "cancelled") {
    // Stopped after the phase's last step had already finished: the run stops before
    // the next phase, not after "this step".
    const done = !!a?.ended;
    lines.push({ id: id("stopping"), state: "waiting", title: done ? "Stopping before the next phase" : "Stopping once this step finishes" });
  }
  return lines;
}

// ── motion helpers ──────────────────────────────────────────────────────────

function reducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** How much of `text` is showing: a new sentence types itself in, an old one is there. */
function useTyped(text: string, animate: boolean): number {
  const [shown, setShown] = useState(animate ? 0 : text.length);
  useEffect(() => {
    if (!animate || reducedMotion()) {
      setShown(text.length);
      return;
    }
    // Quick for a short sentence, never more than about a second and a half.
    const duration = Math.min(1500, Math.max(450, text.length * 14));
    const start = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const p = Math.min((now - start) / duration, 1);
      setShown(Math.round(p * text.length));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [text, animate]);
  return Math.min(shown, text.length);
}

/** An element that grows in when it is new, and is plain once it has. */
function Enter({
  fresh,
  className,
  delay = 0,
  as: Tag = "li",
  children,
}: {
  fresh: boolean;
  className: string;
  delay?: number;
  as?: "li" | "div";
  children: ReactNode;
}) {
  const [growing, setGrowing] = useState(fresh);
  return (
    <Tag
      className={className + (growing ? " is-new" : "")}
      style={growing && delay ? ({ ["--ap-delay" as string]: `${delay}ms` } as CSSProperties) : undefined}
      onAnimationEnd={(e) => {
        if (e.target === e.currentTarget) setGrowing(false);
      }}
    >
      {children}
    </Tag>
  );
}

/** The step's words: lit while it runs, with a sheen passing over them. */
function Title({ text }: { text: string }) {
  return (
    <span className="ap-title">
      <span className="ap-title-ink">{text}</span>
      <span className="ap-sheen" aria-hidden="true">
        {text}
      </span>
    </span>
  );
}

function Typed({ text, fresh }: { text: string; fresh: boolean }) {
  // Decided once, as it mounts: `fresh` is only true on the render it first appears.
  const [animate] = useState(fresh);
  const n = useTyped(text, animate);
  // The last few letters arrive soft, so the text reads as written rather than stamped.
  const edge = Math.max(0, n - 6);
  return (
    <p className="ap-msg">
      <span className="sr-only">{text}</span>
      <span aria-hidden="true">
        {text.slice(0, edge)}
        {n < text.length ? <span className="ap-edge">{text.slice(edge, n)}</span> : text.slice(edge, n)}
        {/* The rest is laid out but invisible: the paragraph is its full height from
            the first frame, so typing never pushes anything down a line at a time. */}
        <span className="ap-ghost">{text.slice(n)}</span>
      </span>
    </p>
  );
}

/** A sentence is news when its words are, not just its line. */
function messageId(line: Line): string {
  return `${line.id}#${line.message}`;
}

const STATE_ICON: Partial<Record<LineState, ReactNode>> = {
  stopped: Icon.alert,
  waiting: Icon.clock,
};

function Row({ line, isNew, delay }: { line: Line; isNew: (id: string) => boolean; delay: number }) {
  const [open, setOpen] = useState(false);
  const panel = `ap-more-${line.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;

  return (
    <Enter fresh={isNew(line.id)} delay={delay} className={`ap-line is-${line.state}`}>
      <div className="ap-line-in">
        {/* One element for the life of the line. The toggle is a button stretched over
            the row rather than the row itself, so a live line that finishes keeps its
            words where they are and crossfades into its done colour, instead of being
            swapped for a different element. */}
        <div className={"ap-head" + (line.panel ? " has-more" : "")}>
          {STATE_ICON[line.state] && <span className="ap-icon">{STATE_ICON[line.state]}</span>}
          <Title text={line.title} />
          {line.chip && <code className="ap-chip">{line.chip}</code>}
          {line.object && <span className="ap-obj">{line.object}</span>}
          {line.count && <span className="ap-count">{line.count}</span>}
          {line.flag && <span className="ap-flag">{line.flag}</span>}
          {line.panel && (
            <button
              type="button"
              className="ap-toggle"
              aria-expanded={open}
              aria-controls={panel}
              aria-label={`${line.title}: ${open ? "hide" : "show"} the plan`}
              onClick={() => setOpen((o) => !o)}
            >
              <span className="ap-chev">{Icon.chevron}</span>
            </button>
          )}
        </div>

        {/* Keyed on its words, so a sentence that changes types the new one from the start. */}
        {line.message && (
          <Enter key={line.message} as="div" fresh={isNew(messageId(line))} className="ap-line">
            <div className="ap-line-in">
              <Typed text={line.message} fresh={isNew(messageId(line))} />
            </div>
          </Enter>
        )}

        {line.panel && (
          <div id={panel} className="ap-more" data-open={open || undefined}>
            <div className="ap-more-in">{line.panel}</div>
          </div>
        )}
      </div>
    </Enter>
  );
}

type StepsProps = {
  project: Project;
  phaseKey: string;
  /** The run's effective status (`stalled` folded in), as the page computes it. */
  state: string;
  /**
   * Every line the list has already shown, shared by the rows so one phase handing
   * over to the next is news while a reload is not. Null until something is on screen:
   * whatever is there first is history, and renders still.
   */
  seen: MutableRefObject<Set<string> | null>;
};

/** How long a finished phase's steps take to fold away. Exits are quicker than entrances. */
const FOLD_MS = 380;

/**
 * A phase row's steps while it runs — and for a moment after, so that when the phase
 * hands over they fold away (the last step settling as they go) rather than vanish
 * and drop every row below by their height in one frame.
 */
export default function RowSteps({
  running,
  finished,
  ...props
}: StepsProps & {
  running: boolean;
  /** The phase finished (rather than failing or being stopped): its last step is done. */
  finished: boolean;
}) {
  const [shown, setShown] = useState(running);
  useEffect(() => {
    if (running) {
      setShown(true);
      return;
    }
    const t = setTimeout(() => setShown(false), FOLD_MS);
    return () => clearTimeout(t);
  }, [running]);
  if (!running && !shown) return null;
  return (
    <div className="phase-writing">
      <PhaseSteps {...props} closing={!running} finished={finished} />
    </div>
  );
}

function PhaseSteps({
  project,
  phaseKey,
  state,
  seen,
  closing,
  finished,
}: StepsProps & { closing: boolean; finished: boolean }) {
  const agent = AGENT_BY_KEY[phaseKey];
  // Folding away, the row is no longer this phase's to describe — the poll has moved
  // on — so it shows what it last said. The step in hand settles into its past tense
  // only if the phase finished; one that failed or was stopped is held as it was.
  const said = useRef<Line[]>([]);
  const settle = (l: Line): Line =>
    finished ? { ...l, state: "done", title: l.past ?? l.title, count: undefined } : { ...l, state: "held" };
  const lines = closing
    ? said.current.map((l) => (l.state === "live" ? settle(l) : l))
    : agent
      ? stepsFor(project, phaseKey, agent, state)
      : [];
  if (!closing) said.current = lines;

  const ids = lines.flatMap((l) => (l.message ? [l.id, messageId(l)] : [l.id]));
  // A block that mounts into a list that has already shown something is a phase
  // starting: it opens. One that mounts first (a load mid-run) is simply there.
  const [fresh] = useState(() => seen.current !== null);
  if (seen.current === null) seen.current = new Set(ids);
  const isNew = (id: string) => seen.current !== null && !seen.current.has(id);
  useEffect(() => {
    ids.forEach((id) => seen.current?.add(id));
  });

  if (!agent) return null;
  const generating = !closing && lines.some((l) => l.state === "live") && (state === "running" || state === "cancelled");
  const last = lines[lines.length - 1];
  let arriving = 0;

  return (
    <Enter as="div" fresh={fresh} className={"ap-wrap" + (closing ? " is-closing" : "")}>
      <div className="ap" style={{ ["--agent-lit" as string]: agent.accentLit }}>
        <div className="ap-in">
          <ol className="ap-lines" aria-label={`What ${agent.codename} is doing`}>
            {lines.map((line) => {
              // Steps that land together arrive one after another, not as a block.
              const delay = isNew(line.id) ? Math.min(arriving++, 3) * 70 : 0;
              return <Row key={line.id} line={line} isNew={isNew} delay={delay} />;
            })}
            {generating && (
              <li className="ap-dots" aria-hidden="true">
                <i />
                <i />
                <i />
              </li>
            )}
          </ol>
          {/* The newest line, once, as it changes — title and command only: the file and
              the count change every few seconds while a phase writes, and would chatter. */}
          <p className="sr-only" aria-live="polite">
            {last ? `${agent.codename}: ${[last.title, last.chip].filter(Boolean).join(" ")}` : ""}
          </p>
        </div>
      </div>
    </Enter>
  );
}

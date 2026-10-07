"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { Activity, ActivityStep, PhaseResult, Project } from "@/lib/api";
import { AGENT_BY_KEY, suiteLine, type Persona } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { PHASES } from "@/components/shell/phases";
import { Icon } from "@/components/shell/icons";
import { latestRow } from "@/components/build/payload";
import { FilePath, activityFor } from "@/components/build/CodeWriting";

/**
 * The run as an account of what the crew did and is doing, not a clock (#86).
 *
 * This replaced a banner that said "PRISM is building it", a loading bar and a
 * ticking timer — true, and no help: it said *that* something was happening, never
 * what had been done or what was being done now. The feed reads the way an agent
 * chat does. Finished steps settle into quiet past-tense lines with a chevron; the
 * latest one says in a sentence what it decided; the step in hand shimmers; three
 * dots say more is coming.
 *
 * Everything here is derived from the poll: finished phases from their rows (each
 * output already carries a summary), and the running phase's own steps from
 * `activity.trail`, which the server keeps so a command that ran between two polls,
 * or before a reload, is still there. Nothing is invented to fill a gap — a phase
 * that reports no steps is one line until it's done.
 */

type LineState = "done" | "live" | "stopped" | "waiting";

type Line = {
  /** Stable across polls, so a live line that finishes *becomes* its done line. */
  id: string;
  state: LineState;
  /** The words that shimmer while the step runs. */
  title: string;
  /** Quiet words after it: a file, a count, a tally. */
  object?: ReactNode;
  /** A command, in mono. */
  chip?: string;
  count?: string;
  /** A sentence under the line, typed in when it first appears. */
  message?: string;
  /** What the chevron opens. */
  more?: { text: string | null; phase: string; deliver: string };
};

type Group = { key: string; agent: Persona; live: boolean; lines: Line[] };

// Why a phase that a row still calls `running` isn't. Keyed by the run's effective
// status, so each ending is named the way the notice above names it. `cancelled`
// is deliberately absent: Stop can't interrupt the model call in flight, so a
// cancelled run with a running row still has someone generating, and says so.
const STOPPED_VERB: Record<string, string> = {
  stalled: "Was mid-phase when the build stopped responding",
  failed: "Was mid-phase when the run failed",
};

/** Which field of each phase's output is its own one-paragraph account. */
const SUMMARY_FIELD: Record<string, string> = {
  product_manager: "problem_statement",
  system_design: "architecture_overview",
};

const FINISHED = new Set(["approved", "pending_approval"]);

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function squash(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function summaryOf(row: PhaseResult): string | null {
  const value = row.output?.[SUMMARY_FIELD[row.phase] ?? "summary"];
  return typeof value === "string" && value.trim() ? squash(value) : null;
}

/** The first sentence, or a clean cut at a word if the first sentence runs long. */
function firstSentence(text: string, max = 220): string {
  const end = text.search(/[.!?](\s|$)/);
  if (end >= 0 && end < max) return text.slice(0, end + 1);
  if (text.length <= max) return text;
  const cut = text.slice(0, max - 1);
  return cut.slice(0, cut.lastIndexOf(" ") > 0 ? cut.lastIndexOf(" ") : cut.length).replace(/[,;:]$/, "") + "…";
}

/** The quiet fact a finished phase carries beside its title. */
function factOf(row: PhaseResult): string | undefined {
  if (row.phase === "qa_engineer") return suiteLine(row.test_run) ?? undefined;
  const files = row.output?.files;
  if ((row.phase === "backend_engineer" || row.phase === "frontend_engineer") && Array.isArray(files) && files.length) {
    return plural(files.length, "file");
  }
  return undefined;
}

/** A step the running phase has finished, as the feed says it. */
function finishedStep(t: ActivityStep): Pick<Line, "title" | "chip"> {
  switch (t.stage) {
    case "planning":
      return { title: t.total > 0 ? `Planned ${plural(t.total, "file")}` : "Planned the files" };
    case "writing":
      if (t.total <= 0) return { title: "Wrote the files" };
      return { title: t.done < t.total ? `Wrote ${t.done} of ${plural(t.total, "file")}` : `Wrote ${plural(t.total, "file")}` };
    case "fixing":
      return { title: "Fixed the files the build flagged" };
    case "checking":
      return { title: "Checked the build" };
    case "building":
    case "testing":
      return t.detail ? { title: "Ran", chip: t.detail } : { title: t.stage === "testing" ? "Ran the tests" : "Ran the build" };
    default:
      return { title: t.stage };
  }
}

/** The step in hand. */
function currentStep(a: Activity): Pick<Line, "title" | "object" | "chip" | "count"> {
  const count = a.total > 0 ? `${Math.min(a.done + 1, a.total)} of ${a.total}` : undefined;
  switch (a.stage) {
    case "planning":
      return { title: "Planning the files" };
    case "writing":
    case "fixing":
      return {
        title: a.stage === "fixing" ? "Fixing" : "Writing",
        object: a.detail ? <FilePath path={a.detail} /> : undefined,
        count,
      };
    case "checking":
      return { title: "Checking the build" };
    case "building":
    case "testing":
      return a.detail ? { title: "Running", chip: a.detail } : { title: a.label || "Running it" };
    default:
      return { title: a.label || "Working" };
  }
}

/** The running phase's lines: what it has done, then what it's doing. */
function liveLines(project: Project, key: string, agent: Persona, state: string): Line[] {
  const id = (i: number) => `${key}:${i}`;
  if (STOPPED_VERB[state]) return [{ id: id(0), state: "stopped", title: STOPPED_VERB[state] }];

  const a = activityFor(project, key);
  const lines: Line[] = [];
  if (a) {
    const trail = a.trail ?? [];
    // A phase that reports only its last part (QA's test run) did its writing first.
    if ((trail[0]?.stage ?? a.stage) !== "planning") {
      lines.push({ id: id(0), state: "done", title: agent.steps.done });
    }
    for (const t of trail) {
      lines.push({
        id: id(lines.length),
        state: "done",
        ...finishedStep(t),
        message: t.stage === "planning" && a.note ? a.note : undefined,
      });
    }
    lines.push({ id: id(lines.length), state: "live", ...currentStep(a) });
  } else {
    lines.push({ id: id(0), state: "live", title: agent.steps.doing });
  }

  const tail = lines[lines.length - 1];
  if (state === "cancelled") Object.assign(tail, { title: "Finishing this phase, then stopping", object: undefined, chip: undefined, count: undefined });
  if (state === "paused") Object.assign(tail, { state: "waiting", title: "Paused until your computer reconnects", object: undefined, chip: undefined, count: undefined });
  return lines;
}

/** The whole feed, from one poll. Pure: the same project always reads the same. */
export function feedFor(project: Project, state: string): Group[] {
  const groups: Group[] = [];
  let live: Group | null = null;

  for (const ph of PHASES) {
    const agent = AGENT_BY_KEY[ph.key];
    const row = latestRow(project.phases, ph.key);
    if (!agent || !row) continue;
    if (row.status === "running") {
      live = { key: ph.key, agent, live: true, lines: liveLines(project, ph.key, agent, state) };
    } else if (FINISHED.has(row.status)) {
      const summary = summaryOf(row);
      groups.push({
        key: ph.key,
        agent,
        live: false,
        lines: [{
          // The same id as the phase's first live line: the line that shimmered while
          // the agent worked is the one that settles into "done", not a new one.
          id: `${ph.key}:0`,
          state: "done",
          title: agent.steps.done,
          object: factOf(row),
          more: { text: summary, phase: ph.key, deliver: ph.deliver },
        }],
      });
    }
  }

  // Between two phases nobody holds a row yet, but the run is moving: the next
  // agent's line is already theirs, and it is the same line once their row starts.
  if (!live && state === "running") {
    const next = PHASES.find((ph) => !groups.some((g) => g.key === ph.key));
    const agent = next && AGENT_BY_KEY[next.key];
    if (next && agent) live = { key: next.key, agent, live: true, lines: [{ id: `${next.key}:0`, state: "live", title: agent.steps.doing }] };
  }

  // The latest finished phase says what it decided, in a sentence.
  const latest = groups[groups.length - 1];
  if (latest) {
    const line = latest.lines[0];
    if (line.more?.text) line.message = firstSentence(line.more.text);
  }

  if (live) groups.push(live);

  // A gate is the crew waiting on you; the decision itself is the card above.
  if (state === "awaiting_approval") {
    const last = groups[groups.length - 1];
    if (last) last.lines.push({ id: `${last.key}:gate`, state: "waiting", title: "Waiting on your decision" });
  }
  if (state === "paused" && !live) {
    const last = groups[groups.length - 1];
    if (last) last.lines.push({ id: `${last.key}:paused`, state: "waiting", title: "Paused until your computer reconnects" });
  }
  return groups;
}

/** Whether the feed has anything to say for this run. */
function shows(project: Project, state: string): boolean {
  if (state === "running" || state === "awaiting_approval" || state === "paused") return true;
  // Stopped, failed or stalled with an agent still holding a row: who had it matters.
  const key = project.current_phase;
  return !!key && latestRow(project.phases, key)?.status === "running";
}

// ── motion helpers ──────────────────────────────────────────────────────────

function reducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/**
 * How much of `text` is showing. A new message types itself in; one that was
 * already there when the feed appeared is simply there — a reload or a tab switch
 * never replays it.
 */
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

/**
 * The card's height follows its content on a transition, so a step arriving or a
 * row opening moves everything below it smoothly instead of in a jump. A callback
 * ref, because the content mounts and unmounts as the feed shows and hides.
 */
function useFollowHeight(): [(el: HTMLDivElement | null) => void, number | null] {
  const [height, setHeight] = useState<number | null>(null);
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: HTMLDivElement | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el || typeof ResizeObserver === "undefined") {
      setHeight(null);
      return;
    }
    const ro = new ResizeObserver(() => setHeight(el.offsetHeight));
    ro.observe(el);
    observer.current = ro;
  }, []);
  useEffect(() => () => observer.current?.disconnect(), []);
  return [ref, height];
}

/** A list item that grows in when it is new, and is plain once it has. */
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
        if (e.target === e.currentTarget && e.animationName === "ap-grow") setGrowing(false);
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
  const n = useTyped(text, fresh);
  // The last few letters arrive soft, so the text reads as written rather than stamped.
  const edge = Math.max(0, n - 6);
  return (
    <p className="ap-msg">
      <span className="sr-only">{text}</span>
      <span aria-hidden="true">
        {text.slice(0, edge)}
        {n < text.length && <span className="ap-edge">{text.slice(edge, n)}</span>}
        {n >= text.length && text.slice(edge, n)}
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

function Row({
  line,
  lead,
  isNew,
  delay,
  onJump,
}: {
  line: Line;
  lead?: ReactNode;
  isNew: (id: string) => boolean;
  delay: number;
  onJump: (phase: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const panel = `ap-more-${line.id}`;
  const more = line.more;
  // The panel repeats the summary only when the line isn't already showing all of it.
  const fullText = more?.text && more.text !== line.message ? more.text : null;

  return (
    <Enter fresh={isNew(line.id)} delay={delay} className={`ap-line is-${line.state}`}>
      <div className="ap-line-in">
        {/* One element for the life of the line. The toggle is a button stretched over
            the row rather than the row itself, so a live line that finishes keeps its
            words where they are and crossfades into its done colour, instead of being
            swapped for a different element. */}
        <div className={"ap-head" + (more ? " has-more" : "")}>
          {lead}
          {STATE_ICON[line.state] && <span className="ap-icon">{STATE_ICON[line.state]}</span>}
          <Title text={line.title} />
          {line.chip && <code className="ap-chip">{line.chip}</code>}
          {line.object && <span className="ap-obj">{line.object}</span>}
          {line.count && <span className="ap-count">{line.count}</span>}
          {more && (
            <button
              type="button"
              className="ap-toggle"
              aria-expanded={open}
              aria-controls={panel}
              aria-label={`${line.title}: ${open ? "hide" : "show"} details`}
              onClick={() => setOpen((o) => !o)}
            >
              <span className="ap-chev">{Icon.chevron}</span>
            </button>
          )}
        </div>

        {/* Keyed on its words: a line whose sentence changes (the plan's note, then the
            phase's own summary once it's done) types the new one in from the start. */}
        {line.message && (
          <Enter key={line.message} as="div" fresh={isNew(messageId(line))} className="ap-line">
            <div className="ap-line-in">
              <Typed text={line.message} fresh={isNew(messageId(line))} />
            </div>
          </Enter>
        )}

        {more && (
          <div id={panel} className="ap-more" data-open={open || undefined}>
            <div className="ap-more-in">
              {fullText && <p className="ap-more-text">{fullText}</p>}
              <button type="button" className="ap-see" onClick={() => onJump(more.phase)}>
                See {more.deliver} {Icon.arrowRight}
              </button>
            </div>
          </div>
        )}
      </div>
    </Enter>
  );
}

/** What a screen reader hears: the newest line, once, as it changes. */
function announcement(groups: Group[]): string {
  const g = groups[groups.length - 1];
  const line = g?.lines[g.lines.length - 1];
  if (!g || !line) return "";
  // Title and command only: not the file or the count, which change every few
  // seconds while a phase writes and would make the region chatter.
  return `${g.agent.codename}: ${[line.title, line.chip].filter(Boolean).join(" ")}`;
}

export default function AgentProgress({
  project,
  state,
  onJump,
}: {
  project: Project;
  /** The run's effective status (`stalled` folded in), as the page computes it. */
  state: string;
  onJump: (phase: string) => void;
}) {
  const visible = shows(project, state);
  const groups = visible ? feedFor(project, state) : [];
  const [measure, height] = useFollowHeight();

  // Everything on screen the first time the feed appears is history, not news: it
  // renders still. Only what arrives after that grows, rises and types in.
  const seen = useRef<Set<string> | null>(null);
  if (visible && groups.length && seen.current === null) {
    seen.current = new Set(groups.flatMap((g) => g.lines.flatMap((l) => (l.message ? [l.id, messageId(l)] : [l.id]))));
  }
  const isNew = (id: string) => seen.current !== null && !seen.current.has(id);

  if (!visible || groups.length === 0) return null;

  const live = groups.find((g) => g.live);
  const tail = live?.lines[live.lines.length - 1];
  const generating = !!tail && tail.state === "live" && (state === "running" || state === "cancelled");
  const accent = (live ?? groups[groups.length - 1]).agent;
  let arriving = 0;

  return (
    <section
      className="ap"
      aria-label="Build progress"
      data-sized={height !== null || undefined}
      style={{ height: height ?? undefined, ["--agent" as string]: accent.accent }}
    >
      <div className="ap-in" ref={measure}>
        <ol className="ap-groups">
          {groups.map((g) => (
            <li
              key={g.key}
              className={"ap-group" + (g.live ? " is-live" : "")}
              style={{ ["--agent" as string]: g.agent.accent, ["--agent-lit" as string]: g.agent.accentLit }}
            >
              {/* Keyed on liveness, so the working sprite and the still swap with a fade
                  rather than one frame to the next. */}
              <span className="ap-av" key={g.live ? "live" : "still"}>
                <AgentSprite agent={g.agent} size={g.live ? 32 : 20} state={g.live && generating ? "working" : "queued"} />
              </span>
              <ol className="ap-lines">
                {g.lines.map((line, i) => {
                  // Steps that land together arrive one after another, not as a block.
                  const delay = isNew(line.id) ? Math.min(arriving++, 3) * 70 : 0;
                  return (
                    <Row
                      key={line.id}
                      line={line}
                      lead={i === 0 ? <b className="agent-line-name ap-who">{g.agent.codename}</b> : undefined}
                      isNew={isNew}
                      delay={delay}
                      onJump={onJump}
                    />
                  );
                })}
                {g.live && generating && (
                  <li className="ap-dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </li>
                )}
              </ol>
            </li>
          ))}
        </ol>
        <p className="sr-only" aria-live="polite">
          {announcement(groups)}
        </p>
      </div>
    </section>
  );
}

import type { PhaseResult, Project } from "@/lib/api";
import { AGENTS, AGENT_BY_KEY, suiteLine, type Persona } from "@/components/agents/personas";
import type { SpriteState } from "@/components/agents/AgentSprite";
import {
  HANDOFF_STATE,
  SPRITE_STATE,
  crewAtRest,
  effectiveStatus,
  firstLine,
  latestRow,
  nodeStateFor,
  sentBack,
  spriteFor,
  type NodeState,
} from "@/components/agents/phaseState";
import { stepWords } from "@/components/build/PhaseSteps";

/**
 * What the crew floor shows (#91), as data: each station's sprite, bubble and
 * nameplate, and the board on the back wall. Three sources produce it, and the room
 * draws all three the same way:
 *
 *   • a build as it is now (`liveFloor`), from the same poll the build page reads;
 *   • a finished build at a moment of its run (`replayFloor`), from its phase rows;
 *   • the tour (`tourFloor`): the scripted scenarios, kept for an account with no
 *     builds, and labelled as made up.
 */

export type Bubble = {
  /** What they're doing, in the progress feed's words. */
  head: string;
  /** A file the step is on: shown after `head`, folder quiet. */
  path?: string;
  /** A second line: the reviewer's note, or why the build stopped here. */
  note?: string;
  /** What kind of note it is: `Sent back`, `Stopped here`. */
  noteTag?: string;
};

export type Station = {
  agent: Persona;
  state: SpriteState;
  /** The phase's own state, when there is a build behind it. */
  ns: NodeState | null;
  bubble: Bubble | null;
  /** The status line under the nameplate. */
  plate: string;
  /** The nameplate's colour, which is the sprite's state except for a stalled run. */
  tone: SpriteState;
};

export type Board = {
  title: string;
  status: string;
  done: number;
  /** Who has the work, if anyone. */
  active: number;
  /** "PRISM ON DECK", "ALL APPROVED", or nothing. */
  deck: string;
  /** The live step, or how the build stands: "WRITING · 2/9 FILES · 0:41". */
  foot: string;
};

export type Floor = {
  stations: Station[];
  /** Nobody holds the work, so whoever waits dozes. */
  asleep: boolean;
  board: Board;
};

// ── formatting ───────────────────────────────────────────────────────────────

/** 41 → "0:41", 3725 → "1:02:05". */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** Server times arrive without a zone on older rows; they are UTC. */
export function ms(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(t) ? null : t;
}

/** How long a finished attempt took, from its own row. */
export function took(row: PhaseResult | undefined): number | null {
  const a = ms(row?.started_at);
  const b = ms(row?.completed_at);
  return a !== null && b !== null && b >= a ? (b - a) / 1000 : null;
}

/** `frontend/app/page.tsx` → `app/page.tsx`: the end of a path is the part you read. */
export function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.length > 2 ? parts.slice(-2).join("/") : path;
}

/** "WRITING · 2/9 FILES" from a step; the feed's words, set for the board. */
function stageLine(project: Project): string {
  const a = project.activity;
  if (!a) return "WORKING";
  if (a.ended) return "WRAPPING UP";
  const stage = (a.stage || "setting up").toUpperCase();
  const fileStage = a.stage === "planning" || a.stage === "writing" || a.stage === "fixing";
  return fileStage && a.total > 0 ? `${stage} · ${Math.min(a.done, a.total)}/${a.total} FILES` : stage;
}

const STATUS_WORD: Record<string, string> = {
  created: "NOT STARTED",
  running: "RUNNING",
  awaiting_approval: "WAITING ON YOU",
  completed: "FINISHED",
  failed: "STOPPED",
  cancelled: "STOPPED",
  paused: "PAUSED",
  stalled: "NOT RESPONDING",
};

/** Which question the build is stopped on, for the board's fourth row. */
const GATE_WORD: Record<string, string> = {
  phase: "PHASE REVIEW",
  plan: "PLAN REVIEW",
  ship: "SHIP REVIEW",
  security: "SECURITY STOP",
  cost: "COST CHECK",
  build: "BUILD REVIEW",
  database: "DATABASE QUESTION",
  integrations: "SERVICES QUESTION",
  needs_help: "THE CREW NEEDS A HAND",
};

/** Long names are cut on the board, which is a fixed width of pixel type. */
function boardName(name: string): string {
  const n = name.trim().toUpperCase();
  return n.length > 26 ? `${n.slice(0, 25).trimEnd()}…` : n;
}

// ── a build, now ─────────────────────────────────────────────────────────────

/** The step the live phase is on, as the bubble and nameplate say it. */
function liveStep(project: Project, key: string): { head: string; path?: string } | null {
  const a = project.activity;
  if (!a || a.phase !== key) return null;
  const w = stepWords(a);
  return { head: w.title, path: w.path ? shortPath(w.path) : w.chip };
}

/**
 * `liveFloor(project, tick)`: `tick` is seconds since the poll that brought this
 * build, so the board's clock moves between polls instead of in 2.5s jumps.
 */
export function liveFloor(project: Project, tick = 0): Floor {
  const state = effectiveStatus(project);
  const stalled = state === "stalled";
  const asleep = crewAtRest(project);
  const stations: Station[] = AGENTS.map((agent) => {
    const ns = nodeStateFor(project, agent.key);
    const row = latestRow(project, agent.key);
    const sprite = spriteFor(project, ns);
    const back = ns === "running" || ns === "redo" ? sentBack(project, agent.key) : null;
    const backNote = back ? firstLine(back.feedback) : "";
    let bubble: Bubble | null = null;
    let plate = "";
    switch (ns) {
      case "running": {
        if (stalled) {
          bubble = { head: "Stopped responding" };
          plate = "Not responding";
          break;
        }
        const step = liveStep(project, agent.key);
        bubble = step ?? { head: agent.lines.working };
        plate = step ? (step.path ? `${step.head} ${step.path}` : step.head) : agent.lines.working;
        if (backNote) Object.assign(bubble, { note: backNote, noteTag: "Sent back" });
        break;
      }
      case "gate":
        bubble = { head: "Needs you", note: firstLine(project.gate_note) || undefined };
        plate = "Needs you";
        break;
      case "redo":
        bubble = { head: "Sent back", note: backNote || undefined };
        plate = "Sent back";
        break;
      case "failed":
        plate = "Stopped";
        break;
      case "done": {
        const t = took(row);
        plate = t === null ? "Done" : `Done ${clock(t)}`;
        break;
      }
      default:
        plate = asleep ? "Idle" : "Queued";
    }
    return { agent, state: sprite, ns, bubble, plate, tone: stalled && ns === "running" ? "queued" : sprite };
  });

  const done = stations.filter((s) => s.ns === "done").length;
  const active = stations.findIndex((s) => s.ns === "running" || s.ns === "gate" || s.ns === "redo");
  let foot = STATUS_WORD[state] ?? state.toUpperCase();
  if (state === "running" && active >= 0 && stations[active].ns === "running") {
    const base = project.activity?.elapsed_s ?? project.elapsed_seconds ?? null;
    foot = stageLine(project) + (base !== null ? ` · ${clock(base + tick)}` : "");
  } else if (state === "awaiting_approval") {
    foot = GATE_WORD[project.gate_kind ?? ""] ?? "YOUR TURN";
  } else if (state === "paused") {
    foot = "PAUSED · COMPUTER OFFLINE";
  }
  return {
    stations,
    asleep,
    board: {
      title: boardName(project.name || project.idea),
      status: STATUS_WORD[state] ?? state.toUpperCase(),
      done,
      active,
      deck:
        active >= 0
          ? `${AGENTS[active].codename} ON DECK`
          : done === AGENTS.length
            ? "ALL APPROVED"
            : "",
      foot,
    },
  };
}

/** What the phase that just took the work was given, in the hand-off note's words. */
export function handoffLine(project: Project, from: number, to: number): string {
  const a = AGENTS[from].codename;
  const b = AGENTS[to];
  const row = latestRow(project, b.key);
  const deps =
    row?.handoff?.deps ?? (project.given?.phase === b.key ? project.given.deps : null) ?? [];
  const given = deps
    .map((d) => `${AGENT_BY_KEY[d.phase]?.codename ?? d.phase} ${(HANDOFF_STATE[d.full] ?? HANDOFF_STATE.whole).label.toLowerCase()}`)
    .join(" · ");
  return given ? `${a} → ${b.codename}: ${given}` : `${a} → ${b.codename}`;
}

// ── a finished build, replayed ───────────────────────────────────────────────

export type Attempt = {
  index: number;
  row: PhaseResult;
  start: number;
  end: number;
};

/**
 * A finished run laid out for replay: every attempt in the order it ran, and a
 * clock that squeezes the dead time out.
 *
 * Real time is the wrong axis for a ~8s replay. A build that waited two hours at a
 * gate would spend the whole replay on the wait, and a phase that took four seconds
 * would never be on screen. So time between attempts is cut to a beat, and every
 * attempt gets at least a minimum share; inside an attempt, time runs straight. The
 * mapping is monotonic both ways, so a point on the scrubber is a real moment.
 */
export type Timeline = {
  attempts: Attempt[];
  first: number;
  /** Real time at replay position u ∈ [0, 1]. */
  at: (u: number) => number;
  /** Replay position of a real time. */
  pos: (t: number) => number;
};

const MIN_SHARE = 0.035;
const GAP_SHARE = 0.012;

export function timeline(project: Project): Timeline | null {
  const index = new Map(AGENTS.map((a, i) => [a.key, i]));
  const attempts: Attempt[] = [];
  const rows = project.phases
    .filter((r) => index.has(r.phase))
    .map((r) => ({ r, s: ms(r.started_at) ?? ms(r.created_at) }))
    .filter((x): x is { r: PhaseResult; s: number } => x.s !== null)
    .sort((a, b) => a.s - b.s);
  rows.forEach(({ r, s }, i) => {
    const next = rows[i + 1]?.s ?? null;
    const done = ms(r.completed_at);
    // An attempt with no end (a row that was abandoned) lasts until the next one starts.
    const end = done !== null && done >= s ? done : Math.max(s + (r.latency_ms || 1000), next ?? s);
    attempts.push({ index: index.get(r.phase)!, row: r, start: s, end });
  });
  if (!attempts.length) return null;

  const points = Array.from(new Set(attempts.flatMap((a) => [a.start, a.end]))).sort((a, b) => a - b);
  const spans: { t0: number; t1: number; w: number }[] = [];
  let active = 0;
  for (let k = 0; k < points.length - 1; k++) {
    const t0 = points[k];
    const t1 = points[k + 1];
    const busy = attempts.some((a) => a.start < t1 && a.end > t0);
    spans.push({ t0, t1, w: busy ? t1 - t0 : -1 });
    if (busy) active += t1 - t0;
  }
  const floor = Math.max(active, 1) * MIN_SHARE;
  const gap = Math.max(active, 1) * GAP_SHARE;
  // Each attempt's floor goes on its own spans, in proportion, so a short phase is
  // seen without stretching the long one beside it.
  for (const a of attempts) {
    const mine = spans.filter((s) => s.w >= 0 && s.t0 >= a.start && s.t1 <= a.end);
    const real = mine.reduce((n, s) => n + (s.t1 - s.t0), 0);
    if (real > 0 && real < floor) mine.forEach((s) => (s.w = ((s.t1 - s.t0) / real) * floor));
  }
  spans.forEach((s) => s.w < 0 && (s.w = gap));
  if (!spans.length) spans.push({ t0: points[0], t1: points[0] + 1, w: 1 });
  const total = spans.reduce((n, s) => n + s.w, 0) || 1;
  const cum: number[] = [];
  spans.reduce((n, s) => (cum.push(n), n + s.w), 0);

  return {
    attempts,
    first: points[0],
    at(u) {
      const w = Math.min(Math.max(u, 0), 1) * total;
      let k = spans.length - 1;
      while (k > 0 && cum[k] > w) k--;
      const s = spans[k];
      const f = s.w ? Math.min(Math.max((w - cum[k]) / s.w, 0), 1) : 1;
      return s.t0 + f * (s.t1 - s.t0);
    },
    pos(t) {
      if (t <= spans[0].t0) return 0;
      for (let k = 0; k < spans.length; k++) {
        const s = spans[k];
        if (t <= s.t1) return (cum[k] + (s.t1 > s.t0 ? (t - s.t0) / (s.t1 - s.t0) : 1) * s.w) / total;
      }
      return 1;
    },
  };
}

/** The floor at real time `t` of a finished run. */
export function replayFloor(project: Project, line: Timeline, t: number): Floor {
  const stations: Station[] = AGENTS.map((agent, i) => {
    const mine = line.attempts.filter((a) => a.index === i && a.start <= t);
    const now = mine[mine.length - 1];
    let ns: NodeState = "pending";
    if (now) {
      if (t < now.end) ns = "running";
      else if (now.row.status === "rejected") ns = "redo";
      else if (now.row.status === "failed") ns = "failed";
      else if (now.row.status === "running") ns = "running";
      else ns = "done";
    }
    // The attempt before this one was sent back: say so while it's redone.
    const before = mine[mine.length - 2];
    const back = ns === "redo" ? now.row : ns === "running" && before?.row.status === "rejected" ? before.row : null;
    const backNote = back ? firstLine(back.feedback) : "";
    let bubble: Bubble | null = null;
    let plate = "Queued";
    if (ns === "running") {
      bubble = { head: agent.steps.doing, ...(backNote ? { note: backNote, noteTag: "Sent back" } : {}) };
      plate = "Working";
    } else if (ns === "redo") {
      bubble = { head: "Sent back", note: backNote || undefined };
      plate = "Sent back";
    } else if (ns === "failed") {
      plate = "Stopped";
    } else if (ns === "done") {
      const s = took(now.row);
      plate = s === null ? "Done" : `Done ${clock(s)}`;
    }
    const state = SPRITE_STATE[ns];
    return { agent, state, ns, bubble, plate, tone: state };
  });
  const done = stations.filter((s) => s.ns === "done").length;
  const active = stations.findIndex((s) => s.ns === "running" || s.ns === "redo");
  return {
    stations,
    asleep: active < 0,
    board: {
      title: boardName(project.name || project.idea),
      status: "REPLAY",
      done,
      active,
      deck: active >= 0 ? `${AGENTS[active].codename} ON DECK` : done === AGENTS.length ? "ALL APPROVED" : "",
      foot: `REPLAY · ${clock((t - line.first) / 1000)} IN`,
    },
  };
}

// ── the tour ─────────────────────────────────────────────────────────────────

export type Scenario = {
  id: string;
  label: string;
  hint: string;
  /** Resolves each agent's state by index. */
  state: (i: number) => SpriteState;
};

/** The made-up office, for an account with no builds yet. */
export const SCENARIOS: Scenario[] = [
  { id: "idle", label: "Before the build", hint: "Nobody has been given anything yet.", state: () => "queued" },
  {
    id: "mid",
    label: "Mid build",
    hint: "Two phases approved. FORGE is working.",
    state: (i) => (i < 2 ? "done" : i === 2 ? "working" : "queued"),
  },
  {
    id: "gate",
    label: "Waiting on you",
    hint: "PRISM is done and waiting for your approval.",
    state: (i) => (i < 3 ? "done" : i === 3 ? "gate" : "queued"),
  },
  {
    id: "reject",
    label: "Sent back",
    hint: "You sent SIEVE's tests back. It's running again.",
    state: (i) => (i < 4 ? "done" : i === 4 ? "rejected" : "queued"),
  },
  { id: "shipped", label: "Shipped", hint: "All eight phases approved.", state: () => "done" },
];

const TOUR_PLATE: Record<SpriteState, string> = {
  queued: "Idle",
  working: "Working",
  gate: "Needs you",
  done: "Done",
  rejected: "Sent back",
};

const TOUR_VOICE: Record<SpriteState, keyof Persona["lines"]> = {
  queued: "queued",
  working: "working",
  gate: "done",
  done: "done",
  rejected: "rejected",
};

export function tourFloor(states: SpriteState[], label: string, relay: boolean): Floor {
  const stations: Station[] = AGENTS.map((agent, i) => {
    const st = states[i];
    const live = st === "working" || st === "gate" || st === "rejected";
    return {
      agent,
      state: st,
      ns: null,
      bubble: live ? { head: st === "gate" ? "Needs you" : agent.lines[TOUR_VOICE[st]] } : null,
      plate: TOUR_PLATE[st],
      tone: st,
    };
  });
  const done = states.filter((s) => s === "done").length;
  const active = states.findIndex((s) => s === "working" || s === "gate" || s === "rejected");
  return {
    stations,
    asleep: !relay && states.every((s) => s === "queued" || s === "done"),
    board: {
      title: "THE TOUR",
      status: label.toUpperCase(),
      done,
      active,
      deck: active >= 0 ? `${AGENTS[active].codename} ON DECK` : done === AGENTS.length ? "ALL APPROVED" : "",
      foot: "MADE-UP OFFICE",
    },
  };
}

/** The empty floor: no builds, the crew asleep. */
export function emptyFloor(): Floor {
  return {
    stations: AGENTS.map((agent) => ({
      agent,
      state: "queued" as SpriteState,
      ns: null,
      bubble: null,
      plate: "Idle",
      tone: "queued" as SpriteState,
    })),
    asleep: true,
    board: { title: "AI SWE TEAM", status: "", done: 0, active: -1, deck: "", foot: "NO BUILDS YET" },
  };
}

// ── what changed between two looks at the floor ──────────────────────────────

export type Change = {
  /** For the screen reader log: "PRISM finished. SIEVE is working." */
  said: string[];
  /** A phase that finished on this look, or -1. */
  finished: number;
  /** A phase that took up work on this look (not a re-run), or -1. */
  started: number;
};

/**
 * Compare two floors of the same build. Only what moved is news; a reload or a
 * build switch is not, so the caller passes `prev` only for the same build. A
 * hand-off can land across two looks (one phase finished, the next not started
 * yet), so pairing `finished` with a later `started` is the caller's job.
 */
export function diff(prev: Floor, next: Floor): Change {
  const said: string[] = [];
  let finished = -1;
  let started = -1;
  next.stations.forEach((s, i) => {
    const was = prev.stations[i];
    if (!was || was.ns === s.ns) return;
    const name = s.agent.codename;
    if (s.ns === "done" && (was.ns === "running" || was.ns === "gate")) {
      finished = i;
      said.push(`${name} finished.`);
    } else if (s.ns === "running" && was.ns !== "running") {
      if (was.ns === "redo" || was.ns === "failed") {
        said.push(`${name} is working again.`);
      } else {
        started = i;
        said.push(`${name} is working.`);
      }
    } else if (s.ns === "gate") {
      said.push(`${name} needs you.`);
    } else if (s.ns === "redo") {
      said.push(`${name} was sent back.`);
    } else if (s.ns === "failed") {
      said.push(`${name} stopped mid-phase.`);
    }
  });
  if (next.board.status !== prev.board.status && next.board.status === "FINISHED") said.push("All eight finished.");
  return { said, finished, started };
}

/** SIEVE's line once it's done, from what running the suite actually did. */
export function doneLine(agent: Persona, row: PhaseResult | undefined): string {
  return (agent.key === "qa_engineer" && suiteLine(row?.test_run)) || agent.steps.done;
}

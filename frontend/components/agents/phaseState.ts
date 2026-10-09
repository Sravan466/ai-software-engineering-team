import type { HandoffDep, PhaseResult, Project } from "@/lib/api";
import { latestRow as rowFor } from "@/components/build/payload";
import type { SpriteState } from "./AgentSprite";
import type { Persona } from "./personas";

/**
 * What an agent is doing on a build: one answer, read by the build page's relay and
 * phase list and by the crew floor (#91). Two copies of this used to disagree about
 * the same build depending on which page you were on.
 */
export type NodeState = "done" | "running" | "gate" | "redo" | "failed" | "pending";

/** A stalled run says `running` in the database and is not running. Say the truth. */
export function effectiveStatus(project: Project): string {
  return project.status === "running" && project.stalled ? "stalled" : project.status;
}

// Latest row produced for a phase (phases re-run when sent back). One definition,
// shared with the decision panel: two answers to "which attempt is current" would
// let the badge on a phase and the artifact under review disagree.
export const latestRow = (project: Project, key: string): PhaseResult | undefined =>
  rowFor(project.phases, key);

/**
 * What a phase is doing, read from the phase's own row first.
 *
 * This used to be inferred from the *project* status plus `current_phase`, and both
 * of those only moved once an agent had finished, so a phase mid-generation was
 * indistinguishable from one that had never started. A row now exists from the moment
 * generation begins, and it carries its own status, which makes this a lookup instead
 * of a guess.
 */
export function nodeStateFor(project: Project, key: string): NodeState {
  const row = latestRow(project, key);
  if (!row) return "pending";
  if (row.status === "running") return "running";
  if (row.status === "approved") return "done";
  if (row.status === "rejected") return "redo";
  if (row.status === "failed") return "failed";
  if (row.status === "pending_approval") {
    const waiting = project.status === "awaiting_approval" && project.current_phase === key;
    return waiting ? "gate" : "done";
  }
  return "done";
}

// Which of an agent's voice lines fits the state it's in. A phase waiting at a
// gate has finished its work, so it speaks its "done" line.
export const VOICE_FOR: Record<NodeState, keyof Persona["lines"]> = {
  pending: "queued",
  running: "working",
  gate: "done",
  done: "done",
  redo: "rejected",
  failed: "rejected",
};

// The build view and the sprite share one idea of what an agent is doing.
export const SPRITE_STATE: Record<NodeState, SpriteState> = {
  done: "done",
  running: "working",
  gate: "gate",
  redo: "rejected",
  failed: "rejected",
  pending: "queued",
};

export const NODE_STATUS: Record<NodeState, string> = {
  pending: "Queued",
  running: "Running",
  redo: "Rejected, re-running",
  failed: "Stopped mid-phase",
  gate: "Needs your approval",
  done: "Done",
};

/**
 * Whether the crew is off duty: nobody holds the work, so whoever waits dozes.
 * Only a running build or one waiting on your approval is on duty. A build that
 * hasn't started is at rest, and so is a stalled one: it stopped responding, and
 * the crew says so.
 */
export function crewAtRest(project: Project): boolean {
  const s = effectiveStatus(project);
  return s !== "running" && s !== "awaiting_approval";
}

/**
 * The sprite for a phase. On a stalled run the phase that stopped responding
 * still reads `running`; it dozes with everyone else instead of looping its
 * work beside a sleeping crew.
 */
export function spriteFor(project: Project, ns: NodeState): SpriteState {
  return ns === "running" && effectiveStatus(project) === "stalled" ? "queued" : SPRITE_STATE[ns];
}

/**
 * The attempt before the current one, when the reviewer (or the crew's own fix
 * loop) sent it back: what the agent is redoing, and why. Null on a first attempt.
 */
export function sentBack(project: Project, key: string): PhaseResult | null {
  const rows = project.phases
    .filter((r) => r.phase === key)
    .sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.id < b.id ? -1 : 1));
  const last = rows[rows.length - 1];
  if (!last) return null;
  if (last.status === "rejected") return last;
  const before = rows[rows.length - 2];
  return last.status === "running" && before?.status === "rejected" ? before : null;
}

/** The first line of a reviewer's note, for a bubble or a nameplate. */
export function firstLine(text: string | null | undefined): string {
  return (text ?? "").split(/\r?\n/).map((l) => l.trim()).find(Boolean) ?? "";
}

/** How much of an earlier phase's work a hand-off carried (#80). */
export const HANDOFF_STATE: Record<HandoffDep["full"], { label: string; tone: string }> = {
  whole: { label: "Everything", tone: "ok" },
  cut: { label: "Cut to fit", tone: "warn" },
  digest_only: { label: "Summary only", tone: "warn" },
};

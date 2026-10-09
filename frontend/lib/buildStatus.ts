import { useEffect, useState } from "react";
import { SIGNED_OUT_EVENT, type Project } from "@/lib/api";

/**
 * A build's status as a dot and a phrase. One mapping for every list of builds: the
 * sidebar's recent builds and the crew floor's build switcher (#91).
 */

// Status → the leading dot on each build row.
export const STATUS_DOT: Record<string, string> = {
  completed: "dot-ok",
  awaiting_approval: "dot-warn dot-pulse",
  running: "dot-run dot-pulse",
  failed: "dot-bad",
  stalled: "dot-bad",
  cancelled: "dot-warn",
  paused: "dot-warn",
};

export const STATUS_TEXT: Record<string, string> = {
  completed: "Completed",
  awaiting_approval: "Waiting for your approval",
  running: "Running",
  failed: "Failed",
  cancelled: "Stopped",
  paused: "Paused, waiting for your computer",
  stalled: "Stalled, not responding",
  created: "Not started",
};

// A build that says `running` with no live runner behind it is stalled, and a list
// should say so rather than pulsing at a corpse.
export function statusOf(p: Project): string {
  return p.status === "running" && p.stalled ? "stalled" : p.status;
}

/** Whether a build is in someone's hands right now: working, or waiting on you. */
export function isLive(p: Project): boolean {
  const s = statusOf(p);
  return s === "running" || s === "awaiting_approval";
}

/** Most recently touched first. */
export const byUpdate = (a: Project, b: Project) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0);

/**
 * The build the crew floor opens on: the live one, most recently touched first, or
 * else the most recently touched build of all. Null with no builds.
 */
export function floorBuild(projects: Project[] | null | undefined): Project | null {
  if (!projects?.length) return null;
  const sorted = [...projects].sort(byUpdate);
  return sorted.find(isLive) ?? sorted[0];
}

/** `/crew`, opened on a build (and an agent), when there is one to open on. */
export function crewHref(project: Project | null | undefined, agentKey?: string): string {
  const q = new URLSearchParams();
  if (project) q.set("project", project.id);
  if (agentKey) q.set("agent", agentKey);
  const qs = q.toString();
  return qs ? `/crew?${qs}` : "/crew";
}

// ── the sidebar's list, shared ───────────────────────────────────────────────
// The sidebar already fetches the builds on every page; the home page's crew links
// read that list instead of asking again, so both links always open the same build.
let shared: Project[] | null = null;
const listeners = new Set<(list: Project[] | null) => void>();
// Bumped on sign-out: a list fetched before it belongs to the account that left.
let epoch = 0;

/** Call before fetching the list, and hand the value back with it. */
export function projectsEpoch(): number {
  return epoch;
}

export function publishProjects(list: Project[], fetchedIn: number): void {
  if (fetchedIn !== epoch) return;
  shared = list;
  listeners.forEach((fn) => fn(list));
}

/** Signed out (by hand or by an expired session): the next account sees none of these. */
export function forgetProjects(): void {
  epoch += 1;
  shared = null;
  listeners.forEach((fn) => fn(null));
}

if (typeof window !== "undefined") window.addEventListener(SIGNED_OUT_EVENT, forgetProjects);

export function useSharedProjects(): Project[] | null {
  const [list, setList] = useState<Project[] | null>(shared);
  useEffect(() => {
    listeners.add(setList);
    if (shared) setList(shared);
    return () => {
      listeners.delete(setList);
    };
  }, []);
  return list;
}

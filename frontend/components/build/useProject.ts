"use client";

import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import { api, type Project } from "@/lib/api";
import { latestRow } from "@/components/agents/phaseState";

/**
 * How often to ask again, matched to how fast this build can change.
 *
 * A live `running` build is the only thing worth a tight loop. A *stalled* one is
 * not running at all, and hammering it every 2.5s forever is precisely the old
 * behaviour this used to have. `awaiting_approval` looks static but isn't: another
 * tab can approve, stop or reject it, and a tab showing a gate that no longer exists
 * is how one click's worth of intent used to advance two phases. Stopped, a run
 * still has someone finishing the call Stop can't interrupt (its row says `running`
 * until that call returns, and the row's steps say "stopping once this step
 * finishes", #86); keep watching until it has. A stopped run whose process died
 * before its call returned never will: `stalled`.
 */
export function pollMsFor(project: Project | null): number {
  if (!project) return 0;
  const finishing =
    project.status === "cancelled" &&
    !project.stalled &&
    !!project.current_phase &&
    latestRow(project, project.current_phase)?.status === "running";
  if ((project.status === "running" && !project.stalled) || finishing) return 2500;
  if (project.status === "running" || project.status === "awaiting_approval") return 10000;
  // Paused for the user's computer: it resumes by itself when the connector is
  // back, so keep watching, slowly, to show that happening.
  if (project.status === "paused") return 5000;
  return 0;
}

export type ProjectPoll = {
  project: Project | null;
  setProject: React.Dispatch<React.SetStateAction<Project | null>>;
  /** The build's usage summary, when asked for with `analytics: true`. */
  analytics: any;
  error: string;
  setError: (e: string) => void;
  load: () => Promise<void>;
  /**
   * Whether the error on screen is a control's refusal, which the poll must leave
   * alone: it is cleared by the next action, not by the next reload.
   */
  actionFailed: MutableRefObject<boolean>;
  /** When the build on screen was last read, for clocks that tick between polls. */
  loadedAt: number;
};

/**
 * One build, kept fresh at `pollMsFor`'s cadence. Shared by the build page and the
 * crew floor (#91), which used to have one poll between them and no way to share it.
 *
 * `id` may change under it (the crew floor switches builds): the old build is
 * dropped at once, and a reply for it that lands late is ignored rather than shown
 * under the new one.
 */
export function useProject(id: string | null, opts: { analytics?: boolean } = {}): ProjectPoll {
  const withAnalytics = !!opts.analytics;
  const [project, setProject] = useState<Project | null>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [error, setError] = useState("");
  const [loadedAt, setLoadedAt] = useState(0);
  const actionFailed = useRef(false);
  const current = useRef(id);
  current.current = id;

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const p = await api.getProject(id);
      if (current.current !== id) return;
      setProject(p);
      setLoadedAt(Date.now());
      if (withAnalytics) {
        const a = await api.analytics(id);
        if (current.current !== id) return;
        setAnalytics(a);
      }
      if (!actionFailed.current) setError("");
    } catch (e: any) {
      if (current.current === id) setError(e.message);
    }
  }, [id, withAnalytics]);

  // A different build starts from nothing, not from the last one's numbers.
  useEffect(() => {
    setProject(null);
    setAnalytics(null);
    setError("");
    actionFailed.current = false;
    load();
  }, [load]);

  const pollMs = pollMsFor(project);
  useEffect(() => {
    if (pollMs <= 0) return;
    const t = setInterval(load, pollMs);
    return () => clearInterval(t);
  }, [pollMs, load]);

  return { project, setProject, analytics, error, setError, load, actionFailed, loadedAt };
}

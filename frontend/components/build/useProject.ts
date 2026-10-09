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
  /** The last failure loading *this* build: never one left over from another. */
  error: string;
  /** That failure was a 404: there is no such build (or it isn't yours). */
  notFound: boolean;
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
 * under the new one. Replies for the same build are kept in order too: polls can
 * overlap on a slow backend, and one older than what is already on screen (a poll,
 * its error, its analytics, or a control's own answer) is dropped, or the build would
 * seem to go backwards, which the crew floor would read as work changing hands.
 */
export function useProject(id: string | null, opts: { analytics?: boolean } = {}): ProjectPoll {
  const withAnalytics = !!opts.analytics;
  const [project, setProjectState] = useState<Project | null>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [failure, setFailure] = useState<{ id: string | null; message: string; status?: number }>({ id: null, message: "" });
  const [loadedAt, setLoadedAt] = useState(0);
  const actionFailed = useRef(false);
  const current = useRef(id);
  current.current = id;
  const asked = useRef(0);
  const shown = useRef(0);

  const setError = useCallback((message: string) => setFailure({ id: current.current, message }), []);
  // A control's answer is newer than any poll already in flight.
  const setProject = useCallback<React.Dispatch<React.SetStateAction<Project | null>>>((v) => {
    shown.current = ++asked.current;
    setProjectState(v);
  }, []);

  const load = useCallback(async () => {
    if (!id) return;
    const n = ++asked.current;
    const fresh = () => current.current === id && n >= shown.current;
    try {
      const p = await api.getProject(id);
      if (!fresh()) return;
      shown.current = n;
      setProjectState(p);
      setLoadedAt(Date.now());
      if (withAnalytics) {
        const a = await api.analytics(id);
        if (!fresh()) return;
        setAnalytics(a);
      }
      if (!actionFailed.current) setFailure({ id, message: "" });
    } catch (e: any) {
      if (fresh()) setFailure({ id, message: e.message, status: e.status });
    }
  }, [id, withAnalytics]);

  // A different build starts from nothing, not from the last one's numbers.
  useEffect(() => {
    setProjectState(null);
    setAnalytics(null);
    setFailure({ id: null, message: "" });
    actionFailed.current = false;
    load();
  }, [load]);

  const pollMs = pollMsFor(project);
  useEffect(() => {
    if (pollMs <= 0) return;
    const t = setInterval(load, pollMs);
    return () => clearInterval(t);
  }, [pollMs, load]);

  const mine = failure.id === id;
  return {
    project,
    setProject,
    analytics,
    error: mine ? failure.message : "",
    notFound: mine && failure.status === 404,
    setError,
    load,
    actionFailed,
    loadedAt,
  };
}

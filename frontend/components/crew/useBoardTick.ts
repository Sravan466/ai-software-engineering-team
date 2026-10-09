"use client";

import { useEffect, useState } from "react";
import type { Project } from "@/lib/api";
import { statusOf } from "@/lib/buildStatus";

/**
 * Seconds since the poll that brought this build, while it runs: the board's clock
 * moves every second instead of in poll-sized jumps. Zero for anything not running
 * (a stalled run included).
 */
export function useBoardTick(project: Project | null, loadedAt: number): number {
  const running = !!project && statusOf(project) === "running";
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  return running && loadedAt ? Math.max(0, (now - loadedAt) / 1000) : 0;
}

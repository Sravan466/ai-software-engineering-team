"use client";

import { useEffect, useState } from "react";

/** `0:07`, `4:31`, `1:02:33` — a duration you can read at a glance. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/**
 * Seconds since `startIso`, ticking once a second while `live`.
 *
 * A phase on a local 7B model runs for minutes. Without this the UI offers no
 * evidence that anything is happening at all — which is exactly how a healthy
 * slow run and a dead one came to look identical.
 *
 * The interval only exists while the phase is live, so a page full of finished
 * phases schedules no timers.
 */
export function useElapsed(startIso: string | null | undefined, live: boolean): number | null {
  const [seconds, setSeconds] = useState<number | null>(null);

  useEffect(() => {
    if (!startIso || !live) {
      setSeconds(null);
      return;
    }
    const started = Date.parse(startIso);
    if (Number.isNaN(started)) {
      setSeconds(null);
      return;
    }
    const tick = () => setSeconds(Math.max(0, (Date.now() - started) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startIso, live]);

  return seconds;
}

/** The ticking clock, wherever a phase is live. */
export function Elapsed({ startIso, live }: { startIso: string | null; live: boolean }) {
  const seconds = useElapsed(startIso, live);
  if (seconds === null) return null;
  return (
    <span className="elapsed mono" role="timer" aria-label="Time this phase has been running">
      {formatDuration(seconds)}
    </span>
  );
}

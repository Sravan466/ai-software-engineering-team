"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { diff, type Floor } from "./floor";

/** Lines kept for the screen reader log. */
const LOG_MAX = 6;

type Options = {
  /** Don't read changes as news (the tour plays its own script). */
  quiet?: boolean;
  /** A new view (another build, live ↔ replay): what's on screen is the starting point. */
  onFresh?: (floor: Floor) => void;
  /** Someone took up the work, or the work moved to another agent. */
  onActive?: (index: number) => void;
};

/**
 * What changed on the floor between two looks at the same build (#91): the log line
 * ("PRISM finished. SIEVE is working.") and the bow the finishing agent takes as the
 * next one starts. Shared by the crew floor and the build page's Relay card, so both
 * play a hand-off the same way.
 *
 * `viewKey` names what is on screen (a build, live or replay). When it changes, the
 * floor is a fresh starting point and nothing is played.
 */
export function useFloorEvents(floor: Floor, viewKey: string, opts: Options = {}) {
  const [log, setLog] = useState<string[]>([]);
  const [poke, setPoke] = useState<{ i: number; n: number } | null>(null);
  // Never reset: a sprite skips a value it has already played.
  const pokeSeq = useRef(0);
  const seen = useRef<{ key: string; floor: Floor } | null>(null);
  // A hand-off can land across two looks: one phase finished, the next not started yet.
  const pendingFrom = useRef(-1);
  const latest = useRef({ floor, opts });
  latest.current = { floor, opts };

  const pokeAgent = useCallback((i: number) => setPoke({ i, n: ++pokeSeq.current }), []);
  const clearPoke = useCallback(() => setPoke(null), []);
  const pokeOf = (i: number) => (poke?.i === i ? poke.n : undefined);

  const signature = floor.stations.map((s) => `${s.ns}:${s.state}`).join(",") + "|" + floor.board.status;

  useEffect(() => {
    const { floor: next, opts: o } = latest.current;
    const prev = seen.current;
    seen.current = { key: viewKey, floor: next };
    if (!prev || prev.key !== viewKey) {
      pendingFrom.current = -1;
      o.onFresh?.(next);
      return;
    }
    if (o.quiet) return;
    const ch = diff(prev.floor, next);
    if (ch.said.length) setLog((l) => [...l, ch.said.join(" ")].slice(-LOG_MAX));
    if (ch.finished >= 0) pendingFrom.current = ch.finished;
    if (ch.started >= 0) {
      const from = pendingFrom.current;
      pendingFrom.current = -1;
      if (from >= 0 && ch.started > from) {
        // The one who finished takes a bow as the next one starts.
        pokeAgent(from);
      }
      o.onActive?.(ch.started);
    } else if (next.board.active >= 0 && next.board.active !== prev.floor.board.active) {
      o.onActive?.(next.board.active);
    }
  }, [signature, viewKey, pokeAgent]);

  return { log, setLog, pokeAgent, pokeOf, clearPoke };
}

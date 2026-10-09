"use client";

import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import type { Project } from "@/lib/api";
import { AGENTS } from "@/components/agents/personas";
import { useFloorTheme } from "@/components/agents/themes";
import Room from "./Room";
import { handoffLine, liveFloor } from "./floor";
import { useFloorEvents } from "./useFloorEvents";

export type RelayView = "strip" | "floor";

const VIEW_KEY = "aiteam.relay-view";

/** The Relay card's view, per viewer. The strip until chosen, or when storage is blocked. */
export function useRelayView(): [RelayView, (v: RelayView) => void] {
  const [view, setView] = useState<RelayView>("strip");
  useEffect(() => {
    try {
      if (window.localStorage.getItem(VIEW_KEY) === "floor") setView("floor");
    } catch {
      // Storage blocked: the strip, quietly.
    }
  }, []);
  const choose = (v: RelayView) => {
    setView(v);
    try {
      window.localStorage.setItem(VIEW_KEY, v);
    } catch {
      // Not kept past this visit; the card still changes.
    }
  };
  return [view, choose];
}

/**
 * Animate a box between two contents' heights: it opens or folds to the new height
 * instead of jumping, then goes back to `auto` so it follows its content again.
 * `key` is what was swapped. Skipped under reduced motion.
 */
export function useHeightSwap(box: RefObject<HTMLElement>, key: string) {
  const last = useRef<{ key: string; height: number } | null>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const prev = last.current;
    const to = el.getBoundingClientRect().height;
    last.current = { key, height: to };
    if (!prev || prev.key === key) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    el.style.height = `${prev.height}px`;
    void el.offsetHeight; // the start height, laid out before the transition
    el.classList.add("is-swapping");
    el.style.height = `${to}px`;
    const done = () => {
      el.classList.remove("is-swapping");
      el.style.height = "";
      last.current = { key, height: el.getBoundingClientRect().height };
    };
    const t = setTimeout(done, 340);
    el.addEventListener("transitionend", done, { once: true });
    return () => {
      clearTimeout(t);
      el.removeEventListener("transitionend", done);
    };
  }, [box, key]);
  // The content's height moves between swaps too (a line of text, a poll): keep the
  // starting point current, except mid-swap.
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (!el.classList.contains("is-swapping") && last.current) {
        last.current = { ...last.current, height: el.getBoundingClientRect().height };
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [box]);
}

/**
 * The build page's Relay card as a room (#91): the crew floor for this build, in
 * place of the strip of eight. It plays the same hand-offs as the floor page, and
 * clicking an agent goes to their phase, as the strip does. The inspector, replay
 * and room picker stay on the floor page.
 */
export default function RelayFloor({
  project,
  loadedAt,
  onPick,
}: {
  project: Project;
  /** When the poll last read the build, so the board's clock ticks between polls. */
  loadedAt: number;
  onPick: (phaseKey: string) => void;
}) {
  const look = useFloorTheme();
  const running = project.status === "running" && !project.stalled;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  const tick = running && loadedAt ? Math.max(0, (now - loadedAt) / 1000) : 0;
  const floor = liveFloor(project, tick);
  const { log, flight, pokeAgent, pokeOf } = useFloorEvents(floor, project.id);
  const courier = flight
    ? { ...flight, label: handoffLine(project, flight.from, flight.to, floor.stations[flight.to]?.row) }
    : null;
  return (
    <Room
      compact
      floor={floor}
      selected={-1}
      onSelect={(i) => {
        pokeAgent(i);
        onPick(AGENTS[i].key);
      }}
      pokeOf={pokeOf}
      look={look}
      courier={courier}
      hint={log.length ? log[log.length - 1] : "Click an agent to go to their work"}
    />
  );
}

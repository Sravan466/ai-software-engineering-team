"use client";

import { useEffect, useRef, useState } from "react";
import { artFor, type Persona } from "./personas";

export type SpriteState = "queued" | "working" | "done" | "rejected" | "gate";

/**
 * Below this a 4x6 sheet cell is a few pixels of noise, so the still is drawn
 * instead — one small pre-scaled frame that holds its silhouette at 18px.
 */
const STILL_MAX = 28;

/**
 * Renders an agent from their sprite sheet.
 *
 * Which row plays, and how, is all in agents.css, keyed on `is-<state>`:
 * working and gate loop (someone is busy; someone needs you), done plays once
 * and settles, rejected plays into its slump and holds, queued stands still.
 * Eight characters breathing on a loop everywhere is noise; motion is kept for
 * the states that are news — with one exception, `asleep`: a queued agent
 * while no build is running dozes on a slow loop. Waiting a turn inside a live
 * build stays awake and still, so nobody looks stuck mid-run.
 *
 * Two things layer on top and are deliberately not in the sheet:
 *
 *   • The motion signature. Each persona carries a `motion` that agents.css
 *     applies to the whole figure, so a character moves the same way wherever
 *     it appears, and every keyframe has a reduced-motion path.
 *   • The contact shadow. A figure with nothing under it floats; `ground` opts
 *     into an ellipse tinted with the agent's own colour, which is what makes
 *     the crew read as standing on the floor rather than pasted over it.
 */
export default function AgentSprite({
  agent,
  size = 48,
  state = "queued",
  ground = false,
  asleep = false,
  poke,
  className = "",
}: {
  agent: Persona;
  size?: number;
  state?: SpriteState;
  /** Draw a contact shadow beneath the figure. On for anyone standing in a room. */
  ground?: boolean;
  /** Nothing is running, so a queued agent sleeps. Ignored in any other state. */
  asleep?: boolean;
  /**
   * Bump to poke: one pass of their job (working row) and their signature,
   * played on an overlay while their own animation runs on, untouched,
   * underneath — so when it ends nothing restarts. A new value restarts it.
   */
  poke?: number;
  className?: string;
}) {
  const art = artFor(agent);
  const sheetOnly = size > STILL_MAX;
  const [acting, setActing] = useState<number | null>(null);
  const overlay = useRef<HTMLSpanElement>(null);
  // The last poke this sprite played. A sprite that comes back round to an
  // agent (the inspector following the relay) must not replay an old click.
  const played = useRef<number | undefined>(undefined);

  // A different agent in the same sprite (the inspector) starts clean. Declared
  // before the poke effect: when a click changes both at once, the reset runs
  // first and the poke then starts.
  useEffect(() => setActing(null), [agent.key]);

  useEffect(() => {
    if (poke === undefined) {
      setActing(null); // the page called it off (relay, scenario)
    } else if (poke !== played.current && sheetOnly) {
      played.current = poke;
      setActing(poke);
    }
  }, [poke, sheetOnly]);

  // The act ends when its animations do — re-read each time, so one added
  // mid-act (a state change) is waited for too, and a cancelled one simply
  // drops out instead of hanging the wait. A backstop timer ends it whatever
  // happens. Under reduced motion there are none; the job frame holds briefly.
  useEffect(() => {
    const el = overlay.current;
    if (acting === null || !el) return;
    let live = true;
    const done = () => live && setActing(null);
    const backstop = setTimeout(done, 2500);
    (async () => {
      if (!el.getAnimations().length) return void setTimeout(done, 900);
      for (;;) {
        const running = el.getAnimations().filter((a) => a.playState !== "finished");
        if (!running.length || !live) break;
        await Promise.allSettled(running.map((a) => a.finished));
      }
      done();
    })();
    return () => {
      live = false;
      clearTimeout(backstop);
    };
  }, [acting]);
  return (
    <span
      className={`sprite motion-${agent.motion} is-${state}${asleep && state === "queued" ? " is-asleep" : ""}${acting !== null && sheetOnly ? " is-poked" : ""}${ground ? " grounded" : ""} ${className}`}
      style={{
        ["--sprite-size" as string]: `${size}px`,
        ["--agent" as string]: agent.accent,
        ["--sheet" as string]: `url(${art.sheet})`,
      }}
      data-agent={agent.codename}
    >
      {ground && <span className="sprite-ground" aria-hidden="true" />}
      {!sheetOnly ? (
        // A fixed 96px asset drawn at icon size; next/image would add nothing.
        // eslint-disable-next-line @next/next/no-img-element
        <img className="sprite-still" src={art.still} alt="" width={size} height={size} draggable={false} />
      ) : (
        <span className="sprite-sheet" aria-hidden="true" />
      )}
      {acting !== null && sheetOnly && (
        <span key={acting} ref={overlay} className="sprite-poke" aria-hidden="true" />
      )}
    </span>
  );
}

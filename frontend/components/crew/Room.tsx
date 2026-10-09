"use client";

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { AGENTS, deskPaletteFor } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import PixelArt from "@/components/agents/PixelArt";
import { BOARD_PROP, COOLER, CRATE, MONITORS, MUG, PLANT, PROP_PALETTE, RACK, SPOOL } from "@/components/agents/props";
import { ART_H, ART_W, type FloorThemeState, type PiecePlace, type PieceSheet, type PropId } from "@/components/agents/themes";
import type { Floor } from "./floor";

/**
 * The room, built back to front (#91 wires it to real builds; the room is #17's):
 *
 *   sky → back wall → floor plane → back set → the crew → foreground set
 *
 * Each tier further from the camera is dimmer and less saturated, each nearer tier
 * is larger and darker in outline, and the crew get contact shadows so they stand on
 * the floor instead of hovering over it. What the crew are doing comes in as a
 * `Floor`: from a build now, from a moment of a finished one, or from the tour.
 */

/**
 * Where each agent actually stands. `x` is across the room, `depth` is how far back
 * (0 = the near edge of the floor, 1 = against the wall). Two loose ranks that take
 * turns: a neighbour is always in the other rank, so neighbours stand one behind
 * the other rather than elbow to elbow, and the four in a rank are two places apart
 * with room between their desks. Each depth is a little off its rank's, so it still
 * reads as a room people work in rather than a lineup.
 */
const FLOOR_PLAN: { x: number; depth: number }[] = [
  { x: 9, depth: 0.68 }, // SCOPE, back
  { x: 20.7, depth: 0.12 }, // ATLAS, front
  { x: 32.4, depth: 0.62 }, // FORGE, back
  { x: 44.1, depth: 0.04 }, // PRISM, front
  { x: 55.9, depth: 0.72 }, // SIEVE, back
  { x: 67.6, depth: 0.14 }, // WARDEN, front
  { x: 79.3, depth: 0.64 }, // RELAY, back
  { x: 90.5, depth: 0.06 }, // LEDGER, front
];

type Props = {
  floor: Floor;
  selected: number;
  onSelect: (i: number) => void;
  pokeOf: (i: number) => number | undefined;
  look: FloorThemeState;
  /** The line under the room: the latest thing that happened, or how to use it. */
  hint: string;
  /** Inside another card (the build page's Relay): shorter, and no frame of its own. */
  compact?: boolean;
  /** Stations go to their phase rather than select an agent: links, not toggles. */
  jump?: boolean;
};

function Prop({ id, has, children }: { id: PropId; has: Set<string>; children: ReactNode }) {
  return has.has(id) ? <span className={"prop p-" + id}>{children}</span> : null;
}

/** An animated set piece from the room's art: its frames played the way a sprite's are.
 *  A second copy of the same piece starts part-way through the loop, so two panes
 *  of rain don't fall in step. */
function Piece({ name, sheet, at, copy = 0 }: { name: string; sheet: PieceSheet; at: PiecePlace; copy?: number }) {
  const dur = Math.round((sheet.cols / (sheet.fps || 10)) * 1000);
  return (
    <span
      className={"piece piece-" + name}
      aria-hidden="true"
      style={
        {
          left: `${at.x}%`,
          top: `${at.y}%`,
          width: `${at.w}%`,
          aspectRatio: `${sheet.w} / ${sheet.h}`,
          animationDelay: copy ? `-${Math.round(dur * ((copy * 0.37) % 1))}ms` : undefined,
          "--piece": `url(/floor/${sheet.src})`,
          "--cols": sheet.cols,
          "--rows": sheet.rows,
          "--piece-dur": `${dur}ms`,
        } as CSSProperties
      }
    />
  );
}

/**
 * The box the art layers cover: the 1536×1024 image scaled to cover the room and
 * anchored on the horizon, as `background-size: cover` places it. Set pieces sit
 * inside it, in the art's own percentages, so they stay on their window or wall.
 */
function useStage(room: React.RefObject<HTMLDivElement>, on: boolean): CSSProperties | null {
  const [box, setBox] = useState<CSSProperties | null>(null);
  useEffect(() => {
    const el = room.current;
    if (!on || !el || typeof ResizeObserver === "undefined") return setBox(null);
    const fit = () => {
      const W = el.clientWidth;
      const H = el.clientHeight;
      const s = Math.max(W / ART_W, H / ART_H);
      const w = ART_W * s;
      const h = ART_H * s;
      const horizon = 1 - parseFloat(getComputedStyle(el).getPropertyValue("--horizon") || "46") / 100;
      setBox({ left: (W - w) / 2, top: (H - h) * horizon, width: w, height: h });
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [room, on]);
  return box;
}

export default function Room({ floor, selected, onSelect, pokeOf, look, hint, compact = false, jump = false }: Props) {
  const { theme, layers, pieces } = look;
  const has = new Set<string>(theme.props.filter((p) => !pieces[p]));
  const room = useRef<HTMLDivElement>(null);

  const stage = useStage(room, !!layers && Object.keys(pieces).length > 0);
  const placed = (tier: PiecePlace["tier"]) => {
    const here = Object.entries(theme.pieces).flatMap(([name, at]) =>
      pieces[name] ? [at].flat().map((p, copy) => ({ name, at: p, copy })).filter((p) => p.at.tier === tier) : [],
    );
    if (!stage || !here.length) return null;
    return (
      <span className="stage" style={stage}>
        {here.map(({ name, at, copy }) => (
          <Piece key={`${name}-${copy}`} name={name} sheet={pieces[name]} at={at} copy={copy} />
        ))}
      </span>
    );
  };

  return (
    <div
      ref={room}
      className={"floor" + (layers ? " has-art" : "") + (compact ? " is-compact" : "")}
      data-theme={theme.id}
      style={
        {
          ...theme.tokens,
          ...(layers
            ? {
                "--layer-sky": `url(/floor/${layers.sky})`,
                "--layer-wall": `url(/floor/${layers.wall})`,
                "--layer-floor": `url(/floor/${layers.floor})`,
              }
            : {}),
        } as CSSProperties
      }
    >
      {/* ── far: the sky through the glazing ── */}
      <span className="sky" aria-hidden="true" />
      {layers && (
        <>
          {/* Outside: drawn under the wall art, so only its glass shows it. */}
          {placed("out")}
          <span className="layer layer-wall" aria-hidden="true" />
          <span className="layer layer-floor" aria-hidden="true" />
        </>
      )}

      {/* ── mid: the back wall ── */}
      <div className="wall" aria-hidden="true">
        {!layers && (
          <>
            <span className="wall-panels" />
            <span className="wall-glow" />
            <span className="window w-left" />
            <span className="window w-right" />
            <span className="pipes" />
            <span className="vent v-left" />
            <span className="vent v-right" />
            <span className="tray" />
            <span className="gantry" />
            <span className="hazard" />
            <span className="bay bay-left">
              <i />
              BAY A · BUILD
            </span>
            <span className="bay bay-right">
              <i />
              BAY B · REVIEW
            </span>
          </>
        )}

        {/* The board the whole room reads. */}
        <div className="board">
          <span className="board-row">
            <b className="board-name">{floor.board.title}</b>
            <span>{floor.board.status}</span>
          </span>
          {/* One cell per phase. State classes are `is-*` on purpose: a bare
              `working` here picks up the build view's global .working panel. */}
          <span
            className="board-bar"
            role="progressbar"
            aria-label="Phases approved"
            aria-valuemin={0}
            aria-valuemax={AGENTS.length}
            aria-valuenow={floor.board.done}
            aria-valuetext={`${floor.board.done} of ${AGENTS.length} approved${
              floor.board.active >= 0 ? `, ${AGENTS[floor.board.active].codename} on deck` : ""
            }`}
          >
            {floor.stations.map((s) => (
              <i key={s.agent.key} className={"board-tick is-" + s.state} />
            ))}
          </span>
          <span className="board-row board-row-dim">
            <span>
              {floor.board.done} OF {AGENTS.length} APPROVED
            </span>
            <span>{floor.board.deck}</span>
          </span>
          <span className="board-row board-row-foot">
            <span className="board-foot">{floor.board.foot}</span>
          </span>
        </div>
        {!layers && <span className="cables" />}
      </div>

      {/* ── the ground ──
          The plane deliberately overshoots the horizon and `.ground` crops it
          there, which is what stops a black band opening up between the wall and
          the floor at any room height. */}
      {!layers && (
        <span className="ground" aria-hidden="true">
          <span className="floor-plane" />
          <span className="floor-marks" />
          <span className="floor-haze" />
        </span>
      )}

      {/* ── back set: stands against the wall, dimmed by distance ── */}
      <div className="set set-back" aria-hidden="true">
        <Prop id="board" has={has}>
          <PixelArt grid={BOARD_PROP} palette={PROP_PALETTE} width={72} />
        </Prop>
        <Prop id="rack" has={has}>
          <PixelArt grid={RACK} palette={PROP_PALETTE} width={34} />
        </Prop>
        <Prop id="monitors" has={has}>
          <PixelArt grid={MONITORS} palette={PROP_PALETTE} width={62} />
        </Prop>
        <Prop id="rack-2" has={has}>
          <PixelArt grid={RACK} palette={PROP_PALETTE} width={30} />
        </Prop>
        <Prop id="crates" has={has}>
          <PixelArt grid={CRATE} palette={PROP_PALETTE} width={38} />
        </Prop>
        <Prop id="plant" has={has}>
          <PixelArt grid={PLANT} palette={PROP_PALETTE} width={42} />
        </Prop>
        {placed("back")}
      </div>

      {/* ── mid tier: the band of floor between the wall and the crew ── */}
      <div className="set set-mid" aria-hidden="true">
        <Prop id="cooler" has={has}>
          <PixelArt grid={COOLER} palette={PROP_PALETTE} width={30} />
        </Prop>
        <Prop id="plant-2" has={has}>
          <PixelArt grid={PLANT} palette={PROP_PALETTE} width={46} />
        </Prop>
        {placed("mid")}
      </div>

      <div className="floor-plan">
        {floor.stations.map((s, i) => {
          const { agent: a } = s;
          const { x, depth } = FLOOR_PLAN[i];
          return (
            <button
              key={a.key}
              className={"station" + (i === selected ? " on" : "")}
              style={
                {
                  "--agent": a.accent,
                  "--x": `${x}%`,
                  "--depth": depth,
                  // Nearer stands in front of further. Occlusion does more for
                  // depth here than the scale or the dimming do.
                  zIndex: Math.round((1 - depth) * 40) + 2,
                } as CSSProperties
              }
              aria-pressed={jump ? undefined : i === selected}
              aria-current={jump && i === floor.board.active ? "step" : undefined}
              aria-label={`${a.codename}, ${a.role}: ${s.plate}${jump ? ". Go to this phase." : ""}`}
              onClick={() => onSelect(i)}
            >
              {/* Only whoever is doing something speaks: working, waiting on you,
                  or sent back. */}
              {s.bubble && (
                <span className={"bubble" + (i === 0 ? " bubble-l" : i === AGENTS.length - 1 ? " bubble-r" : "")}>
                  <span className="bubble-head">
                    {s.bubble.head}
                    {s.bubble.path && <span className="bubble-path"> {s.bubble.path}</span>}
                  </span>
                  {s.bubble.note && (
                    <span className="bubble-note">
                      {s.bubble.noteTag && <b>{s.bubble.noteTag}</b>}
                      {s.bubble.noteTag && " "}
                      {s.bubble.note}
                    </span>
                  )}
                  <i aria-hidden="true" />
                </span>
              )}
              <span className="figure">
                {/* Each agent gets a cabin: a partition behind them with their colour
                    on the rail, a pinned card, a monitor, and a desk in front. */}
                <span className="cabin" aria-hidden="true">
                  <span className="cabin-side cabin-side-l" />
                  <span className="cabin-side cabin-side-r" />
                  <span className="cabin-back">
                    <span className="cabin-cap" />
                    <span className="cabin-pin" />
                    <span className="cabin-screen" />
                    <span className="cabin-prop">
                      <PixelArt grid={a.deskProp} palette={deskPaletteFor(a)} width={26} />
                    </span>
                  </span>
                </span>
                <AgentSprite agent={a} size={72} state={s.state} asleep={floor.asleep} poke={pokeOf(i)} ground />
                <span className="desk" aria-hidden="true">
                  <span className="desk-screen" />
                  <span className="desk-spill" />
                </span>
              </span>
              <span className="plate">{a.codename}</span>
              <span className={"plate-state s-" + s.tone} title={s.plate}>
                {s.plate}
              </span>
            </button>
          );
        })}
      </div>

      {/* ── near set: between you and the crew ── */}
      <div className="set set-near" aria-hidden="true">
        <Prop id="spool" has={has}>
          <PixelArt grid={SPOOL} palette={PROP_PALETTE} width={76} />
        </Prop>
        <Prop id="crate" has={has}>
          <PixelArt grid={CRATE} palette={PROP_PALETTE} width={78} />
        </Prop>
        <Prop id="mug" has={has}>
          <PixelArt grid={MUG} palette={PROP_PALETTE} width={24} />
        </Prop>
        {placed("near")}
      </div>

      {theme.scanlines && <span className="scanlines" aria-hidden="true" />}
      {theme.vignette && <span className="vignette" aria-hidden="true" />}
      <p className="floor-hint" aria-hidden="true">
        {hint}
      </p>
    </div>
  );
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AGENTS,
  PALETTE,
  deskPaletteFor,
  type Persona,
} from "@/components/agents/personas";
import AgentSprite, { type SpriteState } from "@/components/agents/AgentSprite";
import PixelArt from "@/components/agents/PixelArt";
import {
  PLANT,
  RACK,
  BOARD_PROP,
  MONITORS,
  COOLER,
  DRONE,
  CRATE,
  SPOOL,
  MUG,
  PROP_PALETTE,
} from "@/components/agents/props";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";

/**
 * The crew floor.
 *
 * A control room you can actually poke at: the eight agents stand at their
 * stations, you pick a scenario, and the floor plays it. Click anyone to inspect
 * them. It doubles as the honest way to review this work — every state each
 * character can be in, on one screen, with no waiting on a model.
 *
 * The room is built in depth tiers rather than as a decorated panel, because a
 * flat backdrop with sprites pasted on it never reads as a place:
 *
 *   sky → back wall → floor plane → back set → the crew → foreground set
 *
 * Each tier further from the camera is dimmer and less saturated, each nearer
 * tier is larger and darker in outline, and the crew get contact shadows so
 * they stand on the floor instead of hovering over it.
 */

type Scenario = {
  id: string;
  label: string;
  hint: string;
  /** Resolves each agent's state by index. */
  state: (i: number) => SpriteState;
};

const SCENARIOS: Scenario[] = [
  {
    id: "idle",
    label: "Before the build",
    hint: "Nobody has been given anything yet.",
    state: () => "queued",
  },
  {
    id: "mid",
    label: "Mid build",
    hint: "Three phases approved. FORGE has the work.",
    state: (i) => (i < 2 ? "done" : i === 2 ? "working" : "queued"),
  },
  {
    id: "gate",
    label: "Waiting on you",
    hint: "PRISM has finished and is holding for your approval.",
    state: (i) => (i < 3 ? "done" : i === 3 ? "gate" : "queued"),
  },
  {
    id: "reject",
    label: "Sent back",
    hint: "You rejected SIEVE's tests — it is running again.",
    state: (i) => (i < 4 ? "done" : i === 4 ? "rejected" : "queued"),
  },
  {
    id: "shipped",
    label: "Shipped",
    hint: "All eight phases approved.",
    state: () => "done",
  },
];

/**
 * Where each agent actually stands.
 *
 * `x` is across the room, `depth` is how far back (0 = the near edge of the
 * floor, 1 = against the wall). Neither is evenly spaced, deliberately: eight
 * figures at identical distance on a uniform pitch reads as a police lineup,
 * not as a place where people work. So they cluster in pairs the way people
 * standing at shared desks do, and every one of them is at a different distance
 * from you.
 *
 * Depth then pays for itself three ways, which is what sells it: further back
 * is smaller, dimmer, and behind — the nearer figure occludes the further one,
 * and occlusion is the strongest depth cue there is.
 */
const FLOOR_PLAN: { x: number; depth: number }[] = [
  { x: 7,    depth: 0.46 }, // SCOPE
  { x: 18.5, depth: 0.74 }, // ATLAS  — back, at the whiteboard
  { x: 30.5, depth: 0.10 }, // FORGE  — nearest, front left
  { x: 43,   depth: 0.38 }, // PRISM
  { x: 55,   depth: 0.62 }, // SIEVE
  { x: 67,   depth: 0.18 }, // WARDEN — front
  { x: 79.5, depth: 0.54 }, // RELAY
  { x: 92,   depth: 0.06 }, // LEDGER — nearest, front right
];

const VOICE_FOR: Record<SpriteState, keyof Persona["lines"]> = {
  queued: "queued",
  working: "working",
  gate: "done",
  done: "done",
  rejected: "rejected",
};

const STATE_LABEL: Record<SpriteState, string> = {
  queued: "idle",
  working: "working",
  gate: "needs you",
  done: "done",
  rejected: "re-running",
};

const MOTION_NOTE: Record<Persona["motion"], string> = {
  nod: "Considers, then commits",
  drift: "Thinks in space",
  thrum: "Steady machine rhythm",
  flicker: "Restless, never settles",
  scan: "Sweeps for defects",
  guard: "Braced and watchful",
  launch: "Coils, then goes",
  tally: "Counts, flips, counts",
};

export default function CrewPage() {
  // Sitting under a rail of real builds, a scripted floor reads as one of them.
  // The top bar says otherwise before the room is even on screen.
  useChrome(
    {
      sub: "Crew floor",
      badge: (
        <span className="badge">
          <span className="dot" aria-hidden="true" />
          Demo
        </span>
      ),
    },
    [],
  );

  const [scenario, setScenario] = useState(1);
  const [selected, setSelected] = useState(2);
  const [relay, setRelay] = useState<Record<string, SpriteState> | null>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }, []);
  useEffect(() => clearTimers, [clearTimers]);

  /** Play a full pass: each agent works, hands off, the next wakes. */
  function runRelay() {
    clearTimers();
    const base: Record<string, SpriteState> = {};
    AGENTS.forEach((a) => (base[a.key] = "queued"));
    setRelay({ ...base });

    const STEP = 900;
    AGENTS.forEach((a, i) => {
      timers.current.push(
        setTimeout(() => {
          setRelay((p) => ({ ...(p ?? base), [a.key]: "working" }));
          setSelected(i); // the inspector follows the work
        }, i * STEP),
      );
      timers.current.push(
        setTimeout(
          () => setRelay((p) => ({ ...(p ?? base), [a.key]: "done" })),
          i * STEP + STEP - 120,
        ),
      );
    });
    timers.current.push(setTimeout(() => setRelay(null), AGENTS.length * STEP + 1800));
  }

  function stopRelay() {
    clearTimers();
    setRelay(null);
  }

  const stateOf = (i: number): SpriteState =>
    relay ? (relay[AGENTS[i].key] ?? "queued") : SCENARIOS[scenario].state(i);

  const agent = AGENTS[selected];
  const agentState = stateOf(selected);
  const activeIndex = AGENTS.findIndex(
    (_, i) => stateOf(i) === "working" || stateOf(i) === "gate",
  );
  const doneCount = AGENTS.filter((_, i) => stateOf(i) === "done").length;
  const spriteCols = Math.max(...agent.sprite.map((r) => r.length));

  return (
    <div className="crew-page">
      <div className="crew-head">
        <div>
          <h1 className="crew-h1">The crew floor</h1>
          <p className="prose-lede" style={{ marginTop: 8 }}>
            Eight specialists, one pipeline. Pick a scenario to see how the floor behaves, run the
            relay to watch the work change hands, or click anyone to inspect them.
          </p>
          {/* Every number on this page is scripted. Saying so once, plainly, and
              in the same place your eye lands after the lede, is the difference
              between a reference and a lie. */}
          <p className="crew-demo">
            {Icon.info}
            <span>
              Nothing here is a running build. This is a demo of how the floor behaves — the
              scenarios below drive it. Your real builds are under Recent builds in the navigation,
              and each one has its own relay at the top of its page.
            </span>
          </p>
        </div>
      </div>

      {/* ── the console window ── */}
      <div className="win">
        <div className="win-bar">
          <span className="win-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span className="win-title">CREW FLOOR · 8 STATIONS</span>
          {/* "2/8 DONE · FORGE ACTIVE" is exactly the shape of live telemetry, so
              the count never appears without the word that makes it scripted. */}
          <span className="win-meta">
            <b className="win-demo">DEMO</b>
            {doneCount}/8 DONE
            {activeIndex >= 0 && ` · ${AGENTS[activeIndex].codename} ACTIVE`}
          </span>
        </div>

        <div className="floor-wrap">
          {/* The room, built back to front. */}
          <div className="floor">
            {/* ── far: night sky through the glazing ── */}
            <span className="sky" aria-hidden="true" />

            {/* ── mid: the back wall ── */}
            <div className="wall" aria-hidden="true">
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

              {/* The board the whole room reads. */}
              <div className="board">
                <span className="board-row">
                  <b>AI SWE TEAM</b>
                  <span>{SCENARIOS[scenario].label.toUpperCase()}</span>
                </span>
                <span className="board-bar">
                  {AGENTS.map((a, i) => (
                    <i key={a.key} className={"board-tick " + stateOf(i)} />
                  ))}
                </span>
                <span className="board-row board-row-dim">
                  <span>{doneCount} OF 8 APPROVED</span>
                  <span>
                    {activeIndex >= 0 ? `${AGENTS[activeIndex].codename} ON DECK` : "ALL HANDS"}
                  </span>
                </span>
              </div>
              <span className="cables" />
            </div>

            {/* ── the ground ──
                The plane deliberately overshoots the horizon and `.ground`
                crops it there, which is what stops a black band opening up
                between the wall and the floor at any room height. */}
            <span className="ground" aria-hidden="true">
              <span className="floor-plane" />
              <span className="floor-marks" />
              <span className="floor-haze" />
            </span>

            {/* ── back set: stands against the wall, dimmed by distance ── */}
            <div className="set set-back" aria-hidden="true">
              <span className="prop p-board">
                <PixelArt grid={BOARD_PROP} palette={PROP_PALETTE} width={72} />
              </span>
              <span className="prop p-rack">
                <PixelArt grid={RACK} palette={PROP_PALETTE} width={34} />
              </span>
              <span className="prop p-monitors">
                <PixelArt grid={MONITORS} palette={PROP_PALETTE} width={62} />
              </span>
              <span className="prop p-rack-2">
                <PixelArt grid={RACK} palette={PROP_PALETTE} width={30} />
              </span>
              <span className="prop p-crates">
                <PixelArt grid={CRATE} palette={PROP_PALETTE} width={38} />
              </span>
              <span className="prop p-plant">
                <PixelArt grid={PLANT} palette={PROP_PALETTE} width={42} />
              </span>
            </div>

            {/* ── mid tier: the band of floor between the wall and the crew,
                which is otherwise the one place in the room where nothing
                happens ── */}
            <div className="set set-mid" aria-hidden="true">
              <span className="prop p-cooler">
                <PixelArt grid={COOLER} palette={PROP_PALETTE} width={30} />
              </span>
              <span className="prop p-plant-2">
                <PixelArt grid={PLANT} palette={PROP_PALETTE} width={46} />
              </span>
            </div>

            {/* ── mid air: RELAY's courier, permanently mid-errand ── */}
            <span className="prop p-drone" aria-hidden="true">
              <PixelArt grid={DRONE} palette={PROP_PALETTE} width={38} />
            </span>

            <div className="floor-plan">
              {AGENTS.map((a, i) => {
                const st = stateOf(i);
                const live = st === "working" || st === "gate" || st === "rejected";
                const { x, depth } = FLOOR_PLAN[i];
                return (
                  <button
                    key={a.key}
                    className={"station" + (i === selected ? " on" : "")}
                    style={{
                      ["--agent" as string]: a.accent,
                      ["--x" as string]: `${x}%`,
                      ["--depth" as string]: depth,
                      // Nearer stands in front of further. Occlusion does more
                      // for depth here than the scale or the dimming do.
                      zIndex: Math.round((1 - depth) * 40) + 2,
                    }}
                    aria-pressed={i === selected}
                    aria-label={`${a.codename}, ${a.role} — ${STATE_LABEL[st]}`}
                    onClick={() => setSelected(i)}
                  >
                    {/* Only whoever is actually doing something speaks — including
                        an agent that was sent back and is running again. */}
                    {live && (
                      <span className="bubble">
                        {a.lines[VOICE_FOR[st]]}
                        <i aria-hidden="true" />
                      </span>
                    )}
                    <span className="figure">
                      {/* Each agent gets a cabin: a partition behind them with
                          their colour on the rail, a pinned card, a monitor,
                          and a desk in front. Eight people on open floor is a
                          group photo; eight people at their own stations is a
                          place of work. */}
                      <span className="cabin" aria-hidden="true">
                        <span className="cabin-side cabin-side-l" />
                        <span className="cabin-side cabin-side-r" />
                        <span className="cabin-back">
                          <span className="cabin-cap" />
                          <span className="cabin-pin" />
                          <span className="cabin-screen" />
                          <span className="cabin-prop">
                            <PixelArt
                              grid={a.deskProp}
                              palette={deskPaletteFor(a)}
                              width={26}
                            />
                          </span>
                        </span>
                      </span>
                      <AgentSprite agent={a} size={72} state={st} ground />
                      <span className="desk" aria-hidden="true">
                        <span className="desk-screen" />
                        <span className="desk-spill" />
                      </span>
                    </span>
                    <span className="plate">{a.codename}</span>
                    <span className={"plate-state s-" + st}>{STATE_LABEL[st]}</span>
                  </button>
                );
              })}
            </div>

            {/* ── near set: between you and the crew ── */}
            <div className="set set-near" aria-hidden="true">
              <span className="prop p-spool">
                <PixelArt grid={SPOOL} palette={PROP_PALETTE} width={76} />
              </span>
              <span className="prop p-crate">
                <PixelArt grid={CRATE} palette={PROP_PALETTE} width={78} />
              </span>
              <span className="prop p-mug">
                <PixelArt grid={MUG} palette={PROP_PALETTE} width={24} />
              </span>
            </div>

            <span className="scanlines" aria-hidden="true" />
            <span className="vignette" aria-hidden="true" />
            <p className="floor-hint">Click an agent to inspect them</p>
          </div>

          {/* ── inspector ── */}
          <aside className="inspect" style={{ ["--agent" as string]: agent.accent }}>
            <div className="inspect-head">
              <span className="win-title">STATION {agent.n}</span>
              <span className={"plate-state s-" + agentState}>{STATE_LABEL[agentState]}</span>
            </div>

            <div className="inspect-portrait">
              <AgentSprite agent={agent} size={104} state={agentState} />
            </div>

            <h2 className="inspect-name">{agent.codename}</h2>
            <p className="inspect-role">{agent.role}</p>

            <dl className="rows">
              <div className="row">
                <dt>Owns</dt>
                <dd>{agent.discipline}</dd>
              </div>
              <div className="row">
                <dt>Ships</dt>
                <dd className="mono">{agent.deliver}</dd>
              </div>
              <div className="row">
                <dt>Trait</dt>
                <dd style={{ color: "var(--agent)" }}>{agent.trait}</dd>
              </div>
              <div className="row">
                <dt>Spot by</dt>
                <dd>{agent.silhouette}</dd>
              </div>
              <div className="row">
                <dt>Moves</dt>
                <dd>{MOTION_NOTE[agent.motion]}</dd>
              </div>
              <div className="row">
                <dt>Now</dt>
                <dd className={"agent-say" + (agentState === "working" ? " live" : "")}>
                  {agent.lines[VOICE_FOR[agentState]]}
                </dd>
              </div>
            </dl>

            <p className="inspect-tagline">{agent.tagline}</p>

            <div className="inspect-grid">
              <div className="inspect-grid-head">
                <span className="win-title">
                  SPRITE {spriteCols}×{agent.sprite.length}
                </span>
                <span className="ramp" aria-hidden="true">
                  <i style={{ background: agent.accentLit }} />
                  <i style={{ background: agent.accent }} />
                  <i style={{ background: agent.accentDim }} />
                </span>
              </div>
              <div
                className="pixel-grid"
                style={{
                  gridTemplateColumns: `repeat(${spriteCols}, 1fr)`,
                  gridTemplateRows: `repeat(${agent.sprite.length}, 1fr)`,
                }}
                aria-hidden="true"
              >
                {agent.sprite.map((row, y) =>
                  row.split("").map((ch, x) => (
                    <span
                      key={`${x}-${y}`}
                      style={{
                        background:
                          ch === "."
                            ? "transparent"
                            : ch === "A"
                              ? agent.accent
                              : ch === "H"
                                ? agent.accentLit
                                : ch === "B"
                                  ? agent.accentDim
                                  : PALETTE[ch],
                      }}
                    />
                  )),
                )}
              </div>
            </div>
          </aside>
        </div>

        {/* ── scenario bar ── */}
        <div className="win-foot">
          <div className="scenarios" role="group" aria-label="Scenario">
            {SCENARIOS.map((s, i) => (
              <button
                key={s.id}
                className="scenario"
                aria-pressed={!relay && scenario === i}
                disabled={!!relay}
                onClick={() => setScenario(i)}
              >
                {s.label}
              </button>
            ))}
          </div>
          {relay ? (
            <button className="btn btn-sm" onClick={stopRelay}>
              Stop relay
            </button>
          ) : (
            <button className="btn btn-sm btn-primary" onClick={runRelay}>
              {Icon.sparkle} Run the relay
            </button>
          )}
        </div>
      </div>

      <p className="field-hint" style={{ marginTop: 12 }} aria-live="polite">
        {relay ? "Relay running — the floor is driving itself." : SCENARIOS[scenario].hint}
      </p>
    </div>
  );
}

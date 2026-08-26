"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AGENTS, PALETTE, type Persona } from "@/components/agents/personas";
import AgentSprite, { type SpriteState } from "@/components/agents/AgentSprite";
import PixelArt from "@/components/agents/PixelArt";
import { PLANT, RACK, PROP_PALETTE } from "@/components/agents/props";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";

/**
 * The crew floor.
 *
 * A control room you can actually poke at: the eight agents stand at their
 * stations, you pick a scenario, and the floor plays it. Click anyone to inspect
 * them. It doubles as the honest way to review this work — every state each
 * character can be in, on one screen, with no waiting on a model.
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
  useChrome({ sub: "Crew floor" }, []);

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

  return (
    <div className="crew-page">
      <div className="crew-head">
        <div>
          <h1 className="crew-h1">The crew floor</h1>
          <p className="prose-lede" style={{ marginTop: 8 }}>
            Eight specialists, one pipeline. Pick a scenario to see how the floor behaves, run the
            relay to watch the work change hands, or click anyone to inspect them.
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
          <span className="win-meta">
            {doneCount}/8 DONE
            {activeIndex >= 0 && ` · ${AGENTS[activeIndex].codename} ACTIVE`}
          </span>
        </div>

        <div className="floor-wrap">
          {/* The room: back wall with the status board, a floor receding under
              it, and the crew at their consoles on the seam between them. */}
          <div className="floor">
            <div className="wall" aria-hidden="true">
              <span className="wall-glow" />
              {/* Windows onto a night outside. */}
              <span className="window w-left" />
              <span className="window w-right" />
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
                  <span>{activeIndex >= 0 ? `${AGENTS[activeIndex].codename} ON DECK` : "ALL HANDS"}</span>
                </span>
              </div>
              {/* Cable run along the top of the wall. */}
              <span className="cables" />
            </div>

            <div className="floor-plane" aria-hidden="true" />

            <div className="props props-left" aria-hidden="true">
              <PixelArt grid={RACK} palette={PROP_PALETTE} width={44} />
            </div>
            <div className="props props-right" aria-hidden="true">
              <PixelArt grid={PLANT} palette={PROP_PALETTE} width={52} />
            </div>

            <div className="floor-row">
              {AGENTS.map((a, i) => {
                const st = stateOf(i);
                const live = st === "working" || st === "gate" || st === "rejected";
                return (
                  <button
                    key={a.key}
                    className={"station" + (i === selected ? " on" : "")}
                    style={{ ["--agent" as string]: a.accent }}
                    aria-pressed={i === selected}
                    onClick={() => setSelected(i)}
                    title={`${a.codename} — ${STATE_LABEL[st]}`}
                  >
                    {/* Only whoever is actually doing something speaks — including
                        an agent that was sent back and is running again. */}
                    {live && (
                      <span className="bubble">
                        {a.lines[VOICE_FOR[st]]}
                        <i aria-hidden="true" />
                      </span>
                    )}
                    <AgentSprite agent={a} size={54} state={st} />
                    <span className="console" aria-hidden="true">
                      <span className="console-screen" />
                    </span>
                    <span className="plate">{a.codename}</span>
                    <span className={"plate-state s-" + st}>{STATE_LABEL[st]}</span>
                  </button>
                );
              })}
            </div>
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
              <span className="win-title">SPRITE 16×16</span>
              <div className="pixel-grid" aria-hidden="true">
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

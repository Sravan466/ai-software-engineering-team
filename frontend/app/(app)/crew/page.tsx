"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api, type CrewRecord, type Project } from "@/lib/api";
import { AGENTS } from "@/components/agents/personas";
import type { SpriteState } from "@/components/agents/AgentSprite";
import { useFloorTheme } from "@/components/agents/themes";
import { useProject } from "@/components/build/useProject";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { STATUS_DOT, STATUS_TEXT, floorBuild, isLive, statusOf } from "@/lib/buildStatus";
import Room, { type Courier } from "@/components/crew/Room";
import Inspector from "@/components/crew/Inspector";
import { AlsoRunning, BuildSwitcher, ReplayBar, ThemePicker } from "@/components/crew/Controls";
import { useBoardTick } from "@/components/crew/useBoardTick";
import { useFloorEvents } from "@/components/crew/useFloorEvents";
import {
  SCENARIOS,
  emptyFloor,
  handoffLine,
  liveFloor,
  replayFloor,
  timeline,
  tourFloor,
  type Floor,
} from "@/components/crew/floor";

/**
 * The crew floor (#91): your build, as the crew sees it.
 *
 * The room shows a real build: each agent's state comes from that build's phase
 * rows, the live agent's bubble says the step the Build tab's feed shows, the board
 * on the back wall reads the real count, and a poll that moves the work from one
 * phase to the next plays the hand-off. A finished build can be replayed from its
 * own timestamps. The scripted scenarios survive only as a tour, for an account
 * with no builds, and the tour says it is made up.
 */

/** How long a whole replay takes, end to end. */
const REPLAY_MS = 8000;

/** An agent named by phase key or codename, as `?agent=` gives it. */
function agentIndex(want: string | null | undefined): number {
  if (!want) return -1;
  return AGENTS.findIndex((a) => a.key === want || a.codename.toLowerCase() === want.toLowerCase());
}

/**
 * `?project=` follows the build on the floor, so a refresh or a shared link keeps it.
 * `?agent=` goes with a build picked on the page; it stays when the page picks the
 * build an agent link didn't name.
 */
function writeProjectParam(id: string | null, keepAgent = false) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("project", id);
  else url.searchParams.delete("project");
  if (!keepAgent) url.searchParams.delete("agent");
  window.history.replaceState(window.history.state, "", url.pathname + url.search);
}

export default function CrewPage() {
  // useSearchParams needs a boundary to render under on a static page.
  return (
    <Suspense fallback={null}>
      <CrewFloor />
    </Suspense>
  );
}

function CrewFloor() {
  // ── which build ────────────────────────────────────────────────────────────
  // The URL leads: `?project=` and `?agent=` from a link, and again on a crew link
  // followed from this page, or back and forward.
  const params = useSearchParams();
  const urlProject = params.get("project");
  const urlAgent = params.get("agent");
  const agentParam = useRef(urlAgent);
  agentParam.current = urlAgent;
  // A switch made on the page: the URL's `?agent=` belonged to the build before.
  const skipAgent = useRef(false);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [listError, setListError] = useState("");
  const [chosen, setChosen] = useState<string | null>(urlProject);
  // A deep link to a build that isn't there any more.
  const [missing, setMissing] = useState(false);

  const refreshList = useCallback(async () => {
    try {
      setProjects(await api.listProjects());
      setListError("");
    } catch (e: any) {
      setListError(e.message);
      setProjects((p) => p ?? []);
    }
  }, []);

  useEffect(() => {
    refreshList();
  }, [refreshList]);

  const chosenNow = useRef(chosen);
  chosenNow.current = chosen;
  useEffect(() => {
    // A different build than the one on screen: a link followed, or back/forward.
    // (The page writing its own choice into the URL lands here with the same id.)
    if (urlProject && urlProject !== chosenNow.current) {
      setMissing(false);
      setChosen(urlProject);
    }
  }, [urlProject]);

  // Without a deep link, the build in hand, or else the latest one.
  useEffect(() => {
    if (chosen || !projects) return;
    const pick = floorBuild(projects);
    if (pick) {
      setChosen(pick.id);
      writeProjectParam(pick.id, true);
    }
  }, [chosen, projects]);

  // The switcher's dots and the "also running" strip stay current while anything runs.
  const anyLive = !!projects?.some(isLive);
  useEffect(() => {
    if (!anyLive) return;
    const t = setInterval(refreshList, 10000);
    return () => clearInterval(t);
  }, [anyLive, refreshList]);

  const poll = useProject(chosen);
  const project = poll.project && poll.project.id === chosen ? poll.project : null;

  // A link to a build that can't be loaded and isn't among yours: drop it from the
  // URL and say so, and the floor falls back to your latest build, or the empty state.
  useEffect(() => {
    if (!chosen || !poll.notFound || !projects || listError) return;
    if (projects.some((p) => p.id === chosen)) return;
    setMissing(true);
    setChosen(null);
    writeProjectParam(null);
  }, [chosen, poll.notFound, projects, listError]);

  // A build that finishes or stops should read the same in the switcher.
  const status = project ? statusOf(project) : "";
  useEffect(() => {
    if (status) refreshList();
  }, [status, refreshList]);

  // ── each agent's record across every build ─────────────────────────────────
  const [record, setRecord] = useState<Record<string, CrewRecord> | null>(null);
  // Again whenever a phase finishes or the build stops, so the work just done counts.
  // Not on a switch to another build: nothing finished, so nothing to count again.
  const settled = project ? project.phases.filter((r) => r.status !== "running").length : 0;
  const recordFor = useRef<{ id: string | null; have: boolean }>({ id: null, have: false });
  useEffect(() => {
    const id = project?.id ?? null;
    const switched = recordFor.current.id !== id;
    recordFor.current.id = id;
    // A switch alone is no news once a record is on its way or shown.
    if (switched && recordFor.current.have) return;
    recordFor.current.have = true;
    api
      .crewRecord()
      .then((r) => setRecord(r.phases))
      .catch(() => {
        // Keep the figures already shown; the next finished phase asks again.
        recordFor.current.have = false;
      });
  }, [project?.id, status, settled]);

  // ── the room ──────────────────────────────────────────────────────────────
  const look = useFloorTheme();

  // The board's clock moves between polls instead of in 2.5s jumps.
  const tick = useBoardTick(project, poll.loadedAt);

  // Replay: only once a build is out of anyone's hands.
  const line = useMemo(() => (project ? timeline(project) : null), [project]);
  const canReplay = !!project && !isLive(project) && !!line;
  const [replaying, setReplaying] = useState(false);
  const [u, setU] = useState(0);
  const [playing, setPlaying] = useState(false);
  const uNow = useRef(u);
  uNow.current = u;
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let last = performance.now();
    let at = uNow.current;
    const step = (t: number) => {
      at = Math.min(1, at + (t - last) / REPLAY_MS);
      last = t;
      setU(at);
      if (at >= 1) return setPlaying(false);
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing]);
  useEffect(() => {
    if (!canReplay) {
      setReplaying(false);
      setPlaying(false);
    }
  }, [canReplay]);

  // The tour: today's five scenarios and the scripted relay, for an empty account.
  const [touring, setTouring] = useState(false);
  const [scenario, setScenario] = useState(1);
  const [relay, setRelay] = useState<SpriteState[] | null>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }, []);
  useEffect(() => clearTimers, [clearTimers]);

  // Only an answer of "none" is empty: a list that failed to load says so instead.
  const empty = projects !== null && projects.length === 0 && !chosen && !listError;
  const tour = empty && touring;

  let floor: Floor;
  if (tour) {
    floor = tourFloor(relay ?? AGENTS.map((_, i) => SCENARIOS[scenario].state(i)), relay ? "The relay" : SCENARIOS[scenario].label, !!relay);
  } else if (project && replaying && line) {
    floor = replayFloor(project, line, line.at(u));
  } else if (project) {
    floor = liveFloor(project, tick);
  } else {
    floor = emptyFloor();
    if (!empty) floor.board.foot = poll.error ? "BUILD NOT FOUND" : "LOADING";
  }

  // ── selection, and what changed: the log, the bow, the courier ─────────────
  const [selected, setSelected] = useState(0);
  // Once you pick someone, the inspector stays on them; until then it follows the work.
  const [pinned, setPinned] = useState(false);
  const viewKey = tour ? "tour" : project ? `${project.id}:${replaying ? "replay" : "live"}` : "none";
  const { log, setLog, flight, pokeAgent, pokeOf, clearPoke } = useFloorEvents(floor, viewKey, {
    quiet: tour,
    onFresh: (next) => {
      if (!pinned && next.board.active >= 0) setSelected(next.board.active);
    },
    onActive: (i) => {
      if (!pinned) setSelected(i);
    },
  });
  // `?agent=` (an agent card on the home page) selects that agent, whether or not
  // there is a build to show.
  useEffect(() => {
    const i = agentIndex(urlAgent);
    if (i >= 0) {
      setSelected(i);
      setPinned(true);
    }
  }, [urlAgent]);
  function select(i: number) {
    setSelected(i);
    setPinned(true);
    pokeAgent(i);
  }

  // A new build starts with a clean slate. Not before one is chosen: `?agent=` is
  // read here once, for the build it came with.
  useEffect(() => {
    if (!chosen) return;
    setLog([]);
    setReplaying(false);
    setPlaying(false);
    setU(0);
    const i = skipAgent.current ? -1 : agentIndex(agentParam.current);
    skipAgent.current = false;
    if (i >= 0) {
      setSelected(i);
      setPinned(true);
    } else {
      setPinned(false);
    }
  }, [chosen]);

  const courier: Courier | null =
    flight && project
      ? { ...flight, label: handoffLine(project, flight.from, flight.to, floor.stations[flight.to]?.row) }
      : null;

  function pickBuild(id: string) {
    if (id === chosen) return;
    setMissing(false);
    skipAgent.current = true;
    clearTimers();
    setRelay(null);
    setChosen(id);
    writeProjectParam(id);
  }

  function runRelay() {
    clearTimers();
    clearPoke();
    const base: SpriteState[] = AGENTS.map(() => "queued");
    setRelay([...base]);
    const STEP = 900;
    AGENTS.forEach((_, i) => {
      timers.current.push(
        setTimeout(() => {
          setRelay((p) => (p ?? base).map((s, j) => (j === i ? "working" : s)));
          setSelected(i);
        }, i * STEP),
      );
      timers.current.push(
        setTimeout(() => setRelay((p) => (p ?? base).map((s, j) => (j === i ? "done" : s))), i * STEP + STEP - 120),
      );
    });
    timers.current.push(setTimeout(() => setRelay(null), AGENTS.length * STEP + 1800));
  }

  function stopTour() {
    clearTimers();
    setRelay(null);
    setTouring(false);
  }

  // ── chrome ────────────────────────────────────────────────────────────────
  const title = project ? project.name || project.idea : null;
  useChrome(
    tour
      ? {
          sub: "Crew floor",
          badge: (
            <span className="badge">
              <span className="dot" aria-hidden="true" />
              Tour
            </span>
          ),
        }
      : project
        ? {
            sub: "Crew floor",
            badge: (
              <span className="badge">
                <span className={"dot " + (STATUS_DOT[status] ?? "")} aria-hidden="true" />
                {STATUS_TEXT[status] ?? status}
              </span>
            ),
          }
        : { sub: "Crew floor" },
    [tour, title, status],
  );

  const station = floor.stations[selected] ?? floor.stations[0];
  const live = floor.board.active >= 0 ? AGENTS[floor.board.active].codename : null;
  const hint = log.length && !tour ? log[log.length - 1] : "Click an agent to see their work";

  return (
    <div className="crew-page">
      <div className="crew-head">
        <h1 className="crew-h1">The crew floor</h1>
        <p className="prose-lede" style={{ marginTop: 8 }}>
          {empty
            ? "No builds yet, so the crew is asleep. Start one and it shows up here as it runs."
            : "What the crew is doing on your build, as it happens. Click an agent to see their work on this build and across all your builds."}
        </p>
        {tour && (
          // The tour is made up, and says so where your eye lands after the lede.
          <p className="crew-note">
            {Icon.info}
            <span>This is the tour. The office, the build and every number on it are made up.</span>
          </p>
        )}
        {missing && (
          <p className="crew-note" role="status">
            {Icon.info}
            <span>
              That build isn&apos;t there any more.{" "}
              {projects?.length ? "The floor is showing your latest one." : "You have no builds yet."}
            </span>
          </p>
        )}
        {listError && !projects?.length && (
          <div className="notice notice-bad" role="alert" style={{ marginTop: 14 }}>
            {Icon.alert}
            <div className="notice-body">
              <span className="notice-title">Couldn&apos;t load your builds</span>
              <span className="notice-text">{listError}</span>
            </div>
          </div>
        )}
        {poll.error && chosen && (
          <div className="notice notice-bad" role="alert" style={{ marginTop: 14 }}>
            {Icon.alert}
            <div className="notice-body">
              <span className="notice-title">Couldn&apos;t load that build</span>
              <span className="notice-text">{poll.error}</span>
              {projects && projects.some((p) => p.id !== chosen) && (
                <div className="notice-actions">
                  <button className="btn btn-sm" onClick={() => pickBuild(floorBuild(projects.filter((p) => p.id !== chosen))!.id)}>
                    Show my latest build
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* ── the console window ── */}
      <div className="win">
        <div className="win-bar">
          <span className="win-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span className="win-title">
            CREW FLOOR · {tour ? "TOUR" : title ? title.toUpperCase() : "8 STATIONS"}
          </span>
          <ThemePicker themes={look.available} current={look.theme} onPick={look.choose} />
          <span className="win-meta">
            {tour && <b className="win-tag">TOUR</b>}
            {replaying && <b className="win-tag">REPLAY</b>}
            {floor.board.done}/8 DONE
            {live && ` · ${live} ACTIVE`}
          </span>
        </div>

        <div className="floor-wrap">
          <Room
            floor={floor}
            selected={selected}
            onSelect={select}
            pokeOf={pokeOf}
            look={look}
            courier={tour || !project ? null : courier}
            hint={hint}
          />
          <Inspector
            station={station}
            project={tour ? null : project}
            record={record?.[station.agent.key]}
            asleep={floor.asleep}
            poke={pokeOf(selected)}
            showRecord={!tour && !empty}
          />
        </div>

        <div className="win-foot">
          {tour ? (
            <>
              <div className="scenarios" role="group" aria-label="Tour scenario">
                {SCENARIOS.map((s, i) => (
                  <button
                    key={s.id}
                    className="scenario"
                    aria-pressed={!relay && scenario === i}
                    disabled={!!relay}
                    onClick={() => {
                      clearPoke();
                      setScenario(i);
                    }}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
              <div className="crew-foot-actions">
                {relay ? (
                  <button className="btn btn-sm" onClick={() => (clearTimers(), setRelay(null))}>
                    Stop the relay
                  </button>
                ) : (
                  <button className="btn btn-sm" onClick={runRelay}>
                    {Icon.play} Run the relay
                  </button>
                )}
                <button className="btn btn-sm" onClick={stopTour}>
                  End the tour
                </button>
              </div>
            </>
          ) : empty ? (
            <div className="crew-foot-actions crew-foot-empty">
              <Link className="btn btn-sm btn-primary" href="/">
                Start a build {Icon.arrowRight}
              </Link>
              <button className="btn btn-sm" aria-pressed={false} onClick={() => setTouring(true)}>
                {Icon.sparkle} Take the tour
              </button>
            </div>
          ) : replaying && line ? (
            <ReplayBar
              line={line}
              u={u}
              playing={playing}
              onPlay={() => {
                if (u >= 1) setU(0);
                setPlaying(true);
              }}
              onPause={() => setPlaying(false)}
              onSeek={(v) => {
                setPlaying(false);
                setU(v);
              }}
              onExit={() => {
                setPlaying(false);
                setReplaying(false);
              }}
            />
          ) : (
            <>
              <BuildSwitcher projects={projects ?? []} current={project} onPick={pickBuild} />
              <AlsoRunning projects={projects ?? []} current={project} onPick={pickBuild} />
              {canReplay && (
                <button
                  className="btn btn-sm crew-replay-start"
                  onClick={() => {
                    setU(0);
                    setReplaying(true);
                    setPlaying(true);
                  }}
                >
                  {Icon.play} Replay
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {/* What changed, for a screen reader. The line under the room shows the latest. */}
      <div className="sr-only" role="log" aria-live="polite" aria-label="Crew floor activity">
        {log.map((l, i) => (
          <p key={`${i}:${l}`}>{l}</p>
        ))}
      </div>
    </div>
  );
}

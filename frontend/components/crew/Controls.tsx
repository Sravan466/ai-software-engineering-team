"use client";

import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import type { Project } from "@/lib/api";
import { AGENTS } from "@/components/agents/personas";
import type { FloorTheme, ThemeId } from "@/components/agents/themes";
import { Icon } from "@/components/shell/icons";
import { STATUS_DOT, STATUS_TEXT, isLive, statusOf } from "@/lib/buildStatus";
import { clock, type Timeline } from "./floor";

/** Close on a click outside or Escape, and give focus back to the button that opened it. */
function useMenu() {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open]);
  const close = useCallback((refocus = true) => {
    setOpen(false);
    if (refocus) button.current?.focus();
  }, []);
  return { open, setOpen, box, button, close };
}

/**
 * A single-choice list in a popover: arrows move, Home/End jump, Enter or Space
 * picks, Escape closes. The listbox pattern, because what is picked is a value
 * (a build, a room), not a command.
 */
function Options<T extends string>({
  id,
  label,
  items,
  value,
  onPick,
  onClose,
  render,
  className,
}: {
  id: string;
  label: string;
  items: T[];
  value: T | null;
  onPick: (v: T) => void;
  onClose: () => void;
  render: (v: T) => ReactNode;
  className: string;
}) {
  const [at, setAt] = useState(() => Math.max(0, value ? items.indexOf(value) : 0));
  const list = useRef<HTMLUListElement>(null);
  useEffect(() => {
    list.current?.focus();
  }, []);
  useEffect(() => {
    list.current?.querySelector(`[data-i="${at}"]`)?.scrollIntoView({ block: "nearest" });
  }, [at]);
  function onKey(e: KeyboardEvent<HTMLUListElement>) {
    const last = items.length - 1;
    if (e.key === "ArrowDown") setAt((i) => Math.min(last, i + 1));
    else if (e.key === "ArrowUp") setAt((i) => Math.max(0, i - 1));
    else if (e.key === "Home") setAt(0);
    else if (e.key === "End") setAt(last);
    else if (e.key === "Enter" || e.key === " ") items[at] !== undefined && onPick(items[at]);
    else if (e.key === "Escape" || e.key === "Tab") return onClose();
    else return;
    e.preventDefault();
  }
  return (
    <ul
      ref={list}
      id={id}
      role="listbox"
      aria-label={label}
      tabIndex={-1}
      className={"crew-menu " + className}
      aria-activedescendant={items.length ? `${id}-${at}` : undefined}
      onKeyDown={onKey}
    >
      {items.map((v, i) => (
        <li
          key={v}
          id={`${id}-${i}`}
          data-i={i}
          role="option"
          aria-selected={v === value}
          className={"crew-opt" + (i === at ? " is-at" : "")}
          onPointerEnter={() => setAt(i)}
          onClick={() => onPick(v)}
        >
          {render(v)}
        </li>
      ))}
    </ul>
  );
}

function ago(iso: string): string {
  const t = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(t)) return "";
  const m = Math.floor(Math.max(0, Date.now() - t) / 60000);
  if (m < 1) return "now";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`;
}

const nameOf = (p: Project) => p.name || p.idea;

/** The build on the floor, and the way to put another one there. */
export function BuildSwitcher({
  projects,
  current,
  onPick,
}: {
  projects: Project[];
  current: Project | null;
  onPick: (id: string) => void;
}) {
  const m = useMenu();
  const id = useId();
  const sorted = [...projects].sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
  const byId = new Map(sorted.map((p) => [p.id, p]));
  const state = current ? statusOf(current) : "";
  return (
    <div className="crew-switch" ref={m.box}>
      <button
        ref={m.button}
        type="button"
        className="crew-switch-btn"
        aria-haspopup="listbox"
        aria-expanded={m.open}
        aria-controls={m.open ? id : undefined}
        aria-label={current ? `Build on the floor: ${nameOf(current)}, ${STATUS_TEXT[state] ?? state}. Change build` : "Choose a build"}
        onClick={() => m.setOpen((o) => !o)}
      >
        <span className={"dot " + (STATUS_DOT[state] ?? "")} aria-hidden="true" />
        <span className="crew-switch-name">{current ? nameOf(current) : "Choose a build"}</span>
        <span className="crew-switch-state">{current ? STATUS_TEXT[state] ?? state : ""}</span>
        <span className="crew-switch-chev" aria-hidden="true">
          {Icon.chevron}
        </span>
      </button>
      {m.open && (
        <Options
          id={id}
          label="Builds"
          className="crew-menu-up"
          items={sorted.map((p) => p.id)}
          value={current?.id ?? null}
          onClose={() => m.close()}
          onPick={(v) => {
            m.close();
            onPick(v);
          }}
          render={(v) => {
            const p = byId.get(v)!;
            const s = statusOf(p);
            return (
              <>
                <span className={"dot " + (STATUS_DOT[s] ?? "")} aria-hidden="true" />
                <span className="crew-opt-main">
                  <span className="crew-opt-name">{nameOf(p)}</span>
                  <span className="crew-opt-sub">{STATUS_TEXT[s] ?? s}</span>
                </span>
                <span className="crew-opt-time">{ago(p.updated_at)}</span>
              </>
            );
          }}
        />
      )}
    </div>
  );
}

/** Other builds in someone's hands right now, one click from the floor. */
export function AlsoRunning({
  projects,
  current,
  onPick,
}: {
  projects: Project[];
  current: Project | null;
  onPick: (id: string) => void;
}) {
  const others = projects.filter((p) => isLive(p) && p.id !== current?.id);
  if (!others.length) return null;
  return (
    <div className="crew-also" role="group" aria-label="Also running">
      <span className="crew-also-label">Also running</span>
      {others.map((p) => {
        const s = statusOf(p);
        return (
          <button key={p.id} type="button" className="crew-also-btn" onClick={() => onPick(p.id)} title={STATUS_TEXT[s] ?? s}>
            <span className={"dot " + (STATUS_DOT[s] ?? "")} aria-hidden="true" />
            <span className="crew-also-name">{nameOf(p)}</span>
          </button>
        );
      })}
    </div>
  );
}

/** The room around the crew, per viewer. In the window bar, not a panel. */
export function ThemePicker({
  themes,
  current,
  onPick,
}: {
  themes: FloorTheme[];
  current: FloorTheme;
  onPick: (id: ThemeId) => void;
}) {
  const m = useMenu();
  const id = useId();
  if (themes.length < 2) return null;
  const byId = new Map(themes.map((t) => [t.id, t]));
  const swatch = (t: FloorTheme) => (
    <span
      className="crew-swatch"
      aria-hidden="true"
      style={{ ["--s1" as string]: t.swatch[0], ["--s2" as string]: t.swatch[1], ["--s3" as string]: t.swatch[2] } as CSSProperties}
    />
  );
  return (
    <div className="crew-theme" ref={m.box}>
      <button
        ref={m.button}
        type="button"
        className="crew-theme-btn"
        aria-haspopup="listbox"
        aria-expanded={m.open}
        aria-controls={m.open ? id : undefined}
        aria-label={`Room: ${current.name}. Change room`}
        onClick={() => m.setOpen((o) => !o)}
      >
        {swatch(current)}
        <span className="crew-theme-name">{current.name.toUpperCase()}</span>
        <span className="crew-switch-chev" aria-hidden="true">
          {Icon.chevron}
        </span>
      </button>
      {m.open && (
        <Options
          id={id}
          label="Rooms"
          className="crew-menu-down"
          items={themes.map((t) => t.id)}
          value={current.id}
          onClose={() => m.close()}
          onPick={(v) => {
            m.close();
            onPick(v);
          }}
          render={(v) => {
            const t = byId.get(v)!;
            return (
              <>
                {swatch(t)}
                <span className="crew-opt-main">
                  <span className="crew-opt-name">{t.name}</span>
                  <span className="crew-opt-sub">{t.blurb}</span>
                </span>
              </>
            );
          }}
        />
      )}
    </div>
  );
}

/**
 * A finished run, replayed from its own phase rows: play it through in about eight
 * seconds, or drag to any moment. The track under the handle shows every attempt
 * in its agent's colour, sent-back ones hatched, so you can aim for one.
 */
export function ReplayBar({
  line,
  u,
  playing,
  onPlay,
  onPause,
  onSeek,
  onExit,
}: {
  line: Timeline;
  u: number;
  playing: boolean;
  onPlay: () => void;
  onPause: () => void;
  onSeek: (u: number) => void;
  onExit: () => void;
}) {
  const t = line.at(u);
  const elapsed = clock((t - line.first) / 1000);
  const total = clock((line.at(1) - line.first) / 1000);
  return (
    <div className="crew-replay">
      <button
        type="button"
        className="btn btn-sm crew-replay-play"
        onClick={playing ? onPause : onPlay}
        aria-label={playing ? "Pause the replay" : u >= 1 ? "Play the replay again" : "Play the replay"}
      >
        {playing ? Icon.stop : Icon.play}
      </button>
      <div className="crew-scrub">
        <span className="crew-scrub-track" aria-hidden="true">
          {line.attempts.map((a) => {
            const x0 = line.pos(a.start);
            const x1 = line.pos(a.end);
            return (
              <i
                key={a.row.id}
                className={"crew-scrub-seg" + (a.row.status === "rejected" ? " is-back" : a.row.status === "failed" ? " is-failed" : "")}
                style={
                  {
                    left: `${x0 * 100}%`,
                    width: `max(2px, ${(x1 - x0) * 100}%)`,
                    "--agent": AGENTS[a.index].accent,
                  } as CSSProperties
                }
              />
            );
          })}
          <i className="crew-scrub-fill" style={{ width: `${u * 100}%` }} />
        </span>
        <input
          type="range"
          className="crew-scrub-input"
          min={0}
          max={1000}
          step={1}
          value={Math.round(u * 1000)}
          aria-label="Replay position"
          aria-valuetext={`${elapsed} into the run`}
          onChange={(e) => onSeek(Number(e.target.value) / 1000)}
          onPointerDown={onPause}
        />
      </div>
      <span className="crew-replay-time mono" aria-hidden="true">
        {elapsed} / {total}
      </span>
      <button type="button" className="btn btn-sm" onClick={onExit}>
        Back to now
      </button>
    </div>
  );
}

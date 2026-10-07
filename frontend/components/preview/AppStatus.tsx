"use client";

import { useEffect, useState, type CSSProperties } from "react";
import type { PreviewApp, PreviewAppEdit, PreviewState } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/**
 * What the Preview tab is showing, and what is happening to it (#78).
 *
 * The tab can show two different things — the generated app, running, or a sketch
 * drawn from the plan — and approving the build while looking at the wrong one is
 * the mistake this exists to prevent. So one line above the frame always says which
 * is on screen and why, in the same place, in words: never a colour alone.
 */

function ago(iso: string | null | undefined, now: number): string | null {
  if (!iso) return null;
  const t = +new Date(iso);
  if (Number.isNaN(t)) return null;
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

function useNow(every = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), every);
    return () => clearInterval(t);
  }, [every]);
  return now;
}

export type Tone = "live" | "busy" | "sketch" | "bad";

/** The words for what is on screen: [tone, what, why]. */
export function describeSource(state: PreviewState, now: number): [Tone, string, string | null] {
  const app = state.app;
  if (state.source === "app" && app) {
    const editing = app.editing?.active;
    if (app.status === "starting" && !app.url) return ["busy", "Starting the app", app.step ?? null];
    if (app.status === "idle" && !app.url) return ["busy", "Starting the app", null];
    if (app.status === "waiting" || editing) return ["busy", "Running the built app", "the crew is changing the code"];
    if (app.stale || app.status === "starting" || app.status === "idle") return ["busy", "Running the built app", "restarting with the latest code"];
    const when = ago(app.built_at, now);
    const backend =
      app.backend?.status === "down" || app.backend?.status === "none"
        ? app.backend?.why
          ? " · frontend only"
          : ""
        : "";
    return ["live", "Running the built app", `from the Frontend phase${when ? ` ${when}` : ""}${backend}`];
  }
  if (!app || app.status === "none") return ["sketch", "Sketch", "the crew hasn't written the frontend yet"];
  if (app.status === "failed") return ["bad", "Sketch", app.fault === "sandbox" ? "the app couldn't start here" : "the app failed to build"];
  return ["sketch", "Sketch", app.reason ? "the app can't run here" : null];
}

/** One line above the frame: the app or the sketch, and why. */
export function SourceLine({ state, onOpenBuild }: { state: PreviewState; onOpenBuild?: () => void }) {
  const now = useNow();
  const [tone, what, why] = describeSource(state, now);
  const failed = state.app?.status === "failed" && state.app?.fault !== "sandbox";
  const title = state.source === "sketch" ? state.app?.reason ?? state.source_note ?? undefined : state.app?.backend?.why || undefined;
  return (
    <span className="pv-source" data-tone={tone} role="status" aria-live="polite" title={title}>
      <span className="pv-source-dot" aria-hidden="true" />
      <span className="pv-source-what">{what}</span>
      {why && <span className="pv-source-why">· {why}</span>}
      {failed && onOpenBuild && (
        <button type="button" className="pv-source-link" onClick={onOpenBuild}>
          See Build
        </button>
      )}
    </span>
  );
}

/** The steps a start takes, in order, as the backend names them. */
function stepsFor(app: PreviewApp): string[] {
  const build = app.stack === "vite" ? "vite build" : "next build";
  return ["Assembling the code", "npm install", "install scripts (offline)", build, "starting the server"];
}

const STEP_WORDS: Record<string, string> = {
  "Assembling the code": "Gathering the code the crew wrote",
  "npm install": "Installing packages",
  "install scripts (offline)": "Running install scripts, offline",
  "next build": "Building it with",
  "vite build": "Building it with",
  "starting the server": "Starting its server",
};

/** In the frame's place while the app builds: the start, as the steps it takes. */
export function AppStarting({ app, restarting }: { app: PreviewApp; restarting?: boolean }) {
  const steps = stepsFor(app);
  const at = app.step ? steps.indexOf(app.step) : -1;
  const queued = app.status === "idle" || app.step === "Queued" || app.step === "Waiting for a free build slot";
  const current = at < 0 ? (queued ? -1 : 0) : at;
  return (
    <div className="pv-start" role="status" aria-live="polite" style={{ "--agent": "var(--run)", "--agent-lit": "var(--run)" } as CSSProperties}>
      <div className="pv-start-in">
        <h3 className="pv-start-title">{restarting ? "Restarting the app with your change" : "Starting the app"}</h3>
        <p className="pv-start-lead">
          The same build the download and the deploy get — installed and built in a sandbox with no network, then
          served here. It takes about a minute.
        </p>
        <ol className="ap-lines pv-start-steps">
          {queued && (
            <li className="ap-line is-live">
              <div className="ap-line-in">
                <span className="ap-head">
                  <span className="ap-title">
                    <span className="ap-title-ink">{app.step === "Waiting for a free build slot" ? "Waiting for a free build slot" : "Queued"}</span>
                    <span className="ap-sheen" aria-hidden="true">
                      {app.step === "Waiting for a free build slot" ? "Waiting for a free build slot" : "Queued"}
                    </span>
                  </span>
                </span>
              </div>
            </li>
          )}
          {steps.map((s, i) => {
            const state = i < current ? "is-done" : i === current ? "is-live" : "is-todo";
            const words = STEP_WORDS[s] ?? s;
            const chip = s === "next build" || s === "vite build" ? s : null;
            return (
              <li key={s} className={`ap-line pv-start-step ${state}`}>
                <div className="ap-line-in">
                  <span className="ap-head">
                    <span className="ap-icon" aria-hidden="true">
                      {i < current ? Icon.check : <span className="pv-start-tick" />}
                    </span>
                    <span className="ap-title">
                      <span className="ap-title-ink">{words}</span>
                      <span className="ap-sheen" aria-hidden="true">
                        {words}
                      </span>
                    </span>
                    {chip && <span className="ap-chip">{chip}</span>}
                  </span>
                </div>
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

const DOING: Record<PreviewAppEdit["kind"], string> = {
  ask: "The crew is changing",
  patch: "Writing your changes into",
  theme: "Writing the site style into",
  undo: "Bringing back the code before your last change",
  redo: "Putting your change back",
};

/**
 * A change made on the app, while it is being made: the file it is going into, and
 * then the restart. It sits where the sketch's before/after strip does, and opens
 * and folds its own height.
 */
export function ChangeStrip({
  edit,
  restarting,
  onDismiss,
}: {
  edit: PreviewAppEdit | null;
  restarting: boolean;
  onDismiss: () => void;
}) {
  const refused = edit?.status === "refused";
  const open = Boolean(edit && (edit.active || refused || restarting));
  // Kept while it folds away, so its words don't vanish before the box does.
  const [shown, setShown] = useState(edit);
  useEffect(() => {
    if (open) setShown(edit);
  }, [open, edit]);
  const e = open ? edit : shown;
  const file = e?.files?.[0];
  let body: React.ReactNode = null;
  if (e?.status === "refused") {
    body = (
      <>
        {Icon.alert}
        <span className="pv-change-text">
          <b>That change wasn&apos;t kept.</b> {e.reason}
        </span>
        <button type="button" className="pv-x pv-dismiss" onClick={onDismiss} aria-label="Dismiss">
          {Icon.close}
        </button>
      </>
    );
  } else if (e && (e.active || !restarting)) {
    const verb = DOING[e.kind] ?? "Changing";
    const target = e.kind === "undo" || e.kind === "redo" ? null : file;
    body = (
      <>
        <span className="btn-spinner" aria-hidden="true" />
        <span className="pv-change-text">
          {verb}
          {target ? (
            <>
              {" "}
              <code className="pv-change-file">{target.replace(/^frontend\//, "")}</code>
            </>
          ) : null}
          <span className="pv-change-meta"> — then it&apos;s checked and built before the app restarts.</span>
        </span>
      </>
    );
  } else if (e) {
    const back = e.kind === "undo" ? "The earlier code is back." : e.kind === "redo" ? "Your change is back in the code." : "Your change is in the code.";
    body = (
      <>
        <span className="btn-spinner" aria-hidden="true" />
        <span className="pv-change-text">
          <b>{back}</b> Restarting the app with it…
        </span>
      </>
    );
  }
  return (
    <div className="pv-fold" data-open={open} aria-hidden={!open}>
      <div className="pv-fold-in">
        <div className="pv-change" data-tone={refused ? "bad" : "run"} role="status" aria-live="polite">
          {body}
        </div>
      </div>
    </div>
  );
}

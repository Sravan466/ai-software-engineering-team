"use client";

import type { ReactNode } from "react";
import type { Activity, ActivityFileState, Generation, Project } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/**
 * How a code phase writes its code (#81), while it does and after.
 *
 * The Backend and Frontend Engineers plan their files, then write them a few at a
 * time. While that happens the build used to show one spinner for minutes; now it
 * says which file is being written, and the list fills in as files land. Once the
 * phase is done, its Details view keeps the plan beside the files it produced.
 */

/** The running phase's own report, when it is about `phaseKey`. */
export function activityFor(project: Project, phaseKey: string): Activity | null {
  const a = project.activity;
  return a && a.phase === phaseKey ? a : null;
}

/** "a/b/c.tsx" → ["a/b/", "c.tsx"]: the folder quiet, the file name read first. */
function split(path: string): [string, string] {
  const at = path.lastIndexOf("/");
  return at < 0 ? ["", path] : [path.slice(0, at + 1), path.slice(at + 1)];
}

export function FilePath({ path }: { path: string }) {
  const [dir, base] = split(path);
  return (
    <span className="wf-path" title={path}>
      {dir && <span className="wf-dir">{dir}</span>}
      <span className="wf-base">{base}</span>
    </span>
  );
}

const FILE_STATE: Record<ActivityFileState, { label: string; icon: ReactNode; hint: string }> = {
  planned: { label: "Planned", icon: Icon.ring, hint: "In the plan, not written yet." },
  writing: { label: "Writing", icon: Icon.pen, hint: "Being written now." },
  ok: {
    label: "Written",
    icon: Icon.check,
    hint: "Written. Python and JSON are parsed as they land; everything is checked together once the last file is in.",
  },
  fixing: { label: "Fixing", icon: Icon.rotate, hint: "It didn't parse, so it's being written again with the problem named." },
  failed: { label: "Has problems", icon: Icon.alert, hint: "Still doesn't parse after its repair round." },
  missing: { label: "Not written", icon: Icon.close, hint: "Asked for twice and never returned." },
};

/** How far through its plan the phase is, 0–1, or null when there is no plan yet. */
function activityShare(activity: Activity | null): number | null {
  if (!activity || activity.stage === "planning" || activity.total <= 0) return null;
  return Math.min(activity.done / activity.total, 1);
}

/** The file list under a running code phase: fills in as files land. */
export function FileProgress({ activity }: { activity: Activity }) {
  if (activity.stage === "planning" || activity.files.length === 0) {
    return (
      <div className="writing writing-planning">
        <p className="writing-head">
          <span className="writing-count">Planning the files</span>
          <span className="writing-meta">each one is written next, a few per call</span>
        </p>
      </div>
    );
  }
  const share = activityShare(activity) ?? 0;
  const perCall =
    activity.per_call > 1 ? `${activity.per_call} files per call` : "one file per call";
  return (
    <div className="writing">
      <p className="writing-head">
        <span className="writing-count">
          {activity.done} of {activity.total} files
        </span>
        <span
          className="writing-meter"
          role="progressbar"
          aria-label="Files written"
          aria-valuemin={0}
          aria-valuemax={activity.total}
          aria-valuenow={activity.done}
        >
          <span style={{ transform: `scaleX(${share})` }} />
        </span>
        <span className="writing-meta">{perCall}</span>
      </p>
      <ol className="writing-files" aria-label="Files in the plan">
        {activity.files.map((f) => {
          const s = FILE_STATE[f.state] ?? FILE_STATE.planned;
          const now = f.state === "writing" || f.state === "fixing";
          return (
            <li
              key={f.path}
              className={`wf wf-${f.state}`}
              aria-current={now ? "step" : undefined}
              title={s.hint}
            >
              <span className="wf-mark">{s.icon}</span>
              <FilePath path={f.path} />
              <span className="wf-state">{s.label}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

const MODE: Record<Generation["mode"], string> = {
  one: "File by file",
  batch: "In batches",
  whole: "In one reply",
};

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * How a finished code phase wrote its code, and the plan it wrote from — shown in
 * the phase's Details view, beside the fields the plan carried.
 */
export function HowWritten({ generation }: { generation: Generation }) {
  const g = generation;
  const unwritten = new Set(g.unwritten ?? []);
  const facts = [
    g.mode !== "whole" ? `${g.files_written} of ${plural(g.files_planned, "planned file")} written` : null,
    plural(g.calls, "call"),
    g.mode === "batch" ? `up to ${g.files_per_call} files per call` : null,
    g.repairs ? plural(g.repairs, "repair") : null,
    g.truncated_replies ? `${plural(g.truncated_replies, "reply", "replies")} cut off` : null,
  ].filter(Boolean);

  return (
    <section className="detail-block written">
      <h4 className="detail-h">
        How the code was written
        <span className={`written-mode written-${g.mode}`}>{MODE[g.mode] ?? g.mode}</span>
      </h4>
      <p className="written-facts">{facts.join(" · ")}</p>
      {g.mode === "whole" && g.reason && g.reason !== "configured" && (
        <p className="detail-p">{g.reason}</p>
      )}
      {g.plan && g.plan.length > 0 && (
        <ol className="plan-list" aria-label="The plan">
          {g.plan.map((p) => {
            const state: ActivityFileState = unwritten.has(p.path)
              ? "missing"
              : g.files?.[p.path] === "failed"
                ? "failed"
                : g.files?.[p.path]
                  ? "ok"
                  : "missing";
            const s = FILE_STATE[state];
            return (
              <li key={p.path} className={`plan-row wf-${state}`}>
                <span className="wf-mark" title={s.hint}>
                  {s.icon}
                  <span className="sr-only">{s.label}</span>
                </span>
                <div className="plan-main">
                  <span className="plan-top">
                    <FilePath path={p.path} />
                    {p.origin === "split" && <span className="plan-tag">Split out</span>}
                    {p.origin === "unplanned" && <span className="plan-tag warn">Not in the plan</span>}
                  </span>
                  {p.purpose && <span className="plan-purpose">{p.purpose}</span>}
                  {(p.exports?.length > 0 || p.imports?.length > 0) && (
                    <span className="plan-names">
                      {p.exports?.length > 0 && (
                        <span>
                          <span className="plan-k">Exports</span> {p.exports.join(", ")}
                        </span>
                      )}
                      {p.imports?.length > 0 && (
                        <span>
                          <span className="plan-k">Imports</span>{" "}
                          {p.imports.map((i) => split(i)[1]).join(", ")}
                        </span>
                      )}
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
      {(g.left_to_platform?.length ?? 0) > 0 && (
        <p className="written-note">
          Left to the platform, which writes them itself: {g.left_to_platform!.join(", ")}
        </p>
      )}
    </section>
  );
}

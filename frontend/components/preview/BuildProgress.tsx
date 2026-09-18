"use client";

import type { PreviewJob } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/**
 * A mockup being built, pass by pass.
 *
 * The mockup is a build of many model calls now — a design system, a page plan,
 * sample data, then one call per section — and on a local model that is minutes. A
 * spinner and "30–60 seconds" would be a claim the screen cannot keep, so this shows
 * where the build actually is: which pass, how many sections of how many, and for
 * how long. The section count is the one honest progress bar the build has; the
 * passes before it are single calls, so they get a state, not a percentage.
 */

const PASSES: { key: PreviewJob["stage"]; label: string }[] = [
  { key: "design", label: "Design system" },
  { key: "plan", label: "Pages & data" },
  { key: "seed", label: "Sample records" },
  { key: "sections", label: "Sections" },
  { key: "verify", label: "Checks" },
];

const ORDER: PreviewJob["stage"][] = ["queued", "design", "plan", "seed", "sections", "verify", "done"];

function clock(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export default function BuildProgress({ job }: { job: PreviewJob }) {
  const at = ORDER.indexOf(job.stage);
  const sections = job.stage === "sections" && job.total > 0;
  const pct = sections ? Math.round((job.done / job.total) * 100) : 0;

  return (
    <div className="mk-build" role="status" aria-live="polite">
      <div className="mk-build-head">
        <span className="dot dot-run dot-pulse" aria-hidden="true" />
        <span className="mk-build-title">
          {job.stage === "queued" ? "Waiting for the model" : "Building the mockup"}
        </span>
        <span className="mk-build-time mono">{clock(job.elapsed_s)}</span>
        <span className="rule" />
        <span className="field-hint">
          {job.origin === "pipeline" ? "Started by the Frontend phase" : "Started by you"}
        </span>
      </div>

      <ol className="mk-steps">
        {PASSES.map((pass) => {
          const index = ORDER.indexOf(pass.key);
          const state = index < at ? "done" : index === at ? "active" : "todo";
          return (
            <li key={pass.key} className="mk-step" data-state={state}>
              <span className="mk-step-mark" aria-hidden="true">
                {state === "done" ? Icon.check : null}
              </span>
              <span className="mk-step-label">
                {pass.label}
                {pass.key === "sections" && job.total > 0 && (
                  <span className="mono mk-step-count">
                    {" "}
                    {state === "done" ? job.total : job.done}/{job.total}
                  </span>
                )}
              </span>
              <span className="sr-only">
                {state === "done" ? " — done" : state === "active" ? " — in progress" : " — waiting"}
              </span>
            </li>
          );
        })}
      </ol>

      {sections && (
        <div className="mk-meter" role="progressbar" aria-valuemin={0} aria-valuemax={job.total} aria-valuenow={job.done} aria-label="Sections built">
          <span style={{ transform: `scaleX(${pct / 100})` }} />
        </div>
      )}
      <p className="mk-build-detail">
        {sections
          ? job.done > 0 && job.detail
            ? `Last finished: ${job.detail}. Each section is its own call, checked against what it has to do before it is kept.`
            : "Each section is its own call, checked against what it has to do before it is kept."
          : job.stage === "queued"
            ? "The pipeline is still using the model; the mockup starts as soon as it is free."
            : "One call per pass, sized to the model you chose. A local model takes minutes for the whole site."}
      </p>
    </div>
  );
}

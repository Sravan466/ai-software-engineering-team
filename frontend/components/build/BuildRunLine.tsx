"use client";

import type { BuildRun, BuildRunStep } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/**
 * The real build of one code phase (#75), as one line on its card.
 *
 * "Installed 105 packages · `next build` passed in 12 s", or "`next build` failed ·
 * 3 errors" — and, folded under it, each step with the last lines it printed. A
 * failed build opens itself: the terminal is the evidence for the problems the crew
 * was sent, and reading it shouldn't take a click. Silent for a phase that wasn't
 * built (`null`), because "not built" must not read as "built and fine".
 */
export default function BuildRunLine({ run }: { run: BuildRun | null | undefined }) {
  if (!run) return null;
  const failed = run.status === "failed";
  const ran = run.steps.filter((s) => !s.skipped || s.tail);
  const where = [runnerLabel(run.runner), run.image].filter(Boolean).join(" · ");

  return (
    <details className="buildrun" data-state={run.status} open={failed || undefined}>
      <summary>
        <span className="buildrun-chev" aria-hidden="true">
          {Icon.chevron}
        </span>
        <span className="buildrun-mark" aria-hidden="true">
          {run.status === "ok" ? Icon.check : failed ? Icon.alert : Icon.info}
        </span>
        <span className="buildrun-title">Build</span>
        <span className="buildrun-summary">
          <Ticks text={run.summary} />
          <span className="sr-only">
            {run.status === "ok" ? " — passed" : failed ? " — failed" : " — not built"}
          </span>
        </span>
        {where && <span className="buildrun-where">{where}</span>}
      </summary>

      <div className="buildrun-body">
        {run.note && <p className="buildrun-note">{run.note}</p>}
        {ran.length > 0 ? (
          <ol className="buildrun-steps">
            {ran.map((s, i) => (
              <StepRow key={`${s.label}-${i}`} step={s} />
            ))}
          </ol>
        ) : (
          <p className="buildrun-note">
            {run.reason ?? "Nothing ran."} The files were still parsed and their imports checked.
          </p>
        )}
      </div>
    </details>
  );
}

function runnerLabel(runner: string | null): string | null {
  if (runner === "docker") return "Docker sandbox";
  if (runner === "builder") return "Builder service";
  return null;
}

function stepState(s: BuildRunStep): "ok" | "bad" | "skip" | "env" {
  if (s.inconclusive) return "env";
  if (s.skipped || s.timed_out) return "skip";
  return s.ok ? "ok" : "bad";
}

function StepRow({ step }: { step: BuildRunStep }) {
  const state = stepState(step);
  const outcome =
    state === "env"
      ? "stopped at its database"
      : step.timed_out
        ? "ran out of time"
        : step.skipped
          ? "didn't run"
          : step.exit_code === 0
            ? "exit 0"
            : `exit ${step.exit_code ?? "?"}`;
  return (
    <li className="buildrun-step" data-state={state}>
      <div className="buildrun-step-head">
        <span className="buildrun-step-mark" aria-hidden="true">
          {state === "ok" ? Icon.check : state === "bad" ? Icon.alert : state === "env" ? Icon.database : Icon.dot}
        </span>
        <code className="buildrun-cmd">{step.label}</code>
        <span className="buildrun-step-meta">
          {outcome}
          {!step.skipped && <> · {formatSeconds(step.seconds)}</>}
        </span>
      </div>
      {step.tail && (
        <pre className="buildrun-term" tabIndex={0} aria-label={`Output of ${step.label}`}>
          {step.tail}
        </pre>
      )}
    </li>
  );
}

function formatSeconds(s: number): string {
  if (s < 1) return "<1 s";
  if (s < 60) return `${Math.round(s)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

/** `npm run build` in backticks becomes code; everything else stays text. */
export function Ticks({ text }: { text: string }) {
  const parts = text.split("`");
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <code key={i} className="buildrun-code">
            {part}
          </code>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

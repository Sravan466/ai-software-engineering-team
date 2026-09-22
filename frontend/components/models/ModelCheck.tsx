"use client";

import type { CheckLevel, CheckReason, ModelCheck, PreflightCheck } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import { Icon } from "@/components/shell/icons";
import { listOf } from "@/lib/text";

/**
 * Will this model run a build here — said in a word, a dot and a reason.
 *
 * The verdict is the backend's (`compat.py`), read and never re-derived. Each level
 * is a word beside a dot, never the dot's colour alone, and the reason that set it
 * is one sentence away. The same vocabulary appears on every model row in Settings
 * and in the check the composer runs before Start, so a person learns it once.
 */

export const VERDICT: Record<CheckLevel, { word: string; dot: string; tone: string }> = {
  fits: { word: "Fits", dot: "dot-ok", tone: "ok" },
  degraded: { word: "Runs with caveats", dot: "dot-warn", tone: "warn" },
  blocked: { word: "Won't run", dot: "dot-bad", tone: "bad" },
  unknown: { word: "Not checked", dot: "", tone: "none" },
};

/** The verdict as a compact chip — a button when it opens the reasons. */
export function VerdictChip({
  check,
  expanded,
  onToggle,
  controls,
}: {
  check: ModelCheck | null | undefined;
  expanded?: boolean;
  onToggle?: () => void;
  controls?: string;
}) {
  if (!check) {
    return (
      <span className="verdict verdict-pending" aria-live="polite">
        <span className="btn-spinner" aria-hidden="true" />
        Checking
      </span>
    );
  }
  const v = VERDICT[check.level];
  const inner = (
    <>
      <span className={`dot ${v.dot}`} aria-hidden="true" />
      {v.word}
      {onToggle && (
        <span className={"verdict-chev" + (expanded ? " open" : "")} aria-hidden="true">
          {Icon.chevron}
        </span>
      )}
    </>
  );
  if (!onToggle) {
    return (
      <span className="verdict" data-tone={v.tone}>
        {inner}
      </span>
    );
  }
  return (
    <button
      type="button"
      className="verdict"
      data-tone={v.tone}
      aria-expanded={expanded}
      aria-controls={controls}
      onClick={onToggle}
      title={check.summary}
    >
      {inner}
      <span className="sr-only">: {check.summary} Show why.</span>
    </button>
  );
}

function gb(bytes: number): string {
  const value = bytes / 2 ** 30;
  return `${value < 10 ? value.toFixed(1) : value.toFixed(0)} GB`;
}

/**
 * Weights and a minimal cache against the memory of the computer that runs the
 * model. Real numbers from the check, drawn to scale, with the same sentence in
 * words beside it — the bar is never the only place the answer is.
 */
export function MemoryMeter({ check }: { check: ModelCheck }) {
  const { weights_bytes: weights, memory_needed_bytes: needed, ram_bytes: ram } = check.facts;
  if (!weights || !ram) return null;
  const cache = Math.max((needed ?? weights) - weights, 0);
  const pct = (n: number) => `${Math.min((n / ram) * 100, 100).toFixed(1)}%`;
  const over = (needed ?? weights) > ram * 0.75;
  const words = `${gb(weights)} of weights${cache ? ` and ${gb(cache)} of cache` : ""}, on a computer with ${gb(ram)}`;
  return (
    <div className="memory">
      <div className="memory-bar" role="img" aria-label={words} data-over={over || undefined}>
        <span className="memory-weights" style={{ width: pct(weights) }} />
        {cache > 0 && <span className="memory-cache" style={{ width: pct(cache) }} />}
        {/* Past this line the machine starts swapping. */}
        <span className="memory-tick" style={{ left: "75%" }} aria-hidden="true" />
      </div>
      <p className="memory-legend">
        <span className="memory-key memory-key-weights" aria-hidden="true" />
        <span className="mono">{gb(weights)}</span> weights
        {cache > 0 && (
          <>
            <span className="memory-key memory-key-cache" aria-hidden="true" />
            <span className="mono">{gb(cache)}</span> cache
          </>
        )}
        <span className="dim">
          {" "}
          · of <span className="mono">{gb(ram)}</span> on this computer
        </span>
      </p>
    </div>
  );
}

const REASON_ICON: Record<CheckReason["level"], keyof typeof Icon> = {
  blocked: "alert",
  degraded: "alert",
  unknown: "info",
  note: "info",
  fits: "check",
};

/** Every finding, heaviest first, then what to pick instead and the memory picture. */
export function CheckDetail({ check, id, notes = true }: { check: ModelCheck; id?: string; notes?: boolean }) {
  const order: CheckReason["level"][] = ["blocked", "degraded", "unknown", "note"];
  const reasons = [...check.reasons]
    .filter((r) => notes || r.level !== "note")
    .sort((a, b) => order.indexOf(a.level) - order.indexOf(b.level));
  return (
    <div className="check-detail" id={id}>
      {reasons.length === 0 ? (
        <p className="check-reason" data-level="fits">
          {Icon.check}
          <span>Nothing known stands in its way.</span>
        </p>
      ) : (
        <ul className="check-reasons">
          {reasons.map((r) => (
            <li key={r.text} className="check-reason" data-level={r.level}>
              {Icon[REASON_ICON[r.level]]}
              <span>{r.text}</span>
            </li>
          ))}
        </ul>
      )}
      {check.suggestion && (
        <p className="check-suggestion">
          <span className="check-suggestion-label">Instead</span> {check.suggestion}
        </p>
      )}
      <MemoryMeter check={check} />
    </div>
  );
}

const SUPPORT_ROLES: Record<string, string> = { debate: "the debate", preview: "the mockup" };

/** "every agent", or "FORGE, PRISM and the mockup" — who runs on this model. */
export function rolesText(roles: string[], allAgents: number): string {
  const agents = roles.filter((r) => AGENT_BY_KEY[r]);
  if (roles.length === 0) return "the rest of the run";
  if (agents.length >= allAgents) return "every agent";
  const named = roles.map((r) => AGENT_BY_KEY[r]?.codename ?? SUPPORT_ROLES[r] ?? r);
  return listOf(named);
}

/**
 * The check the composer runs before Start, one line per model the build will use.
 *
 * Quiet when everything fits — one line saying it was checked — and specific when
 * it doesn't: the verdict, who runs on it, and every reason that costs something.
 * A model that won't run also blocks Start; the notice below says what to do.
 */
export function PreStartCheck({
  checks,
  checking,
  allAgents,
}: {
  checks: PreflightCheck[] | undefined;
  checking: boolean;
  allAgents: number;
}) {
  if (!checks || checks.length === 0) {
    return checking ? (
      <div className="precheck" aria-live="polite">
        <p className="precheck-line precheck-quiet">
          <span className="btn-spinner" aria-hidden="true" />
          Checking the models this build will use…
        </p>
      </div>
    ) : null;
  }
  const allFit = checks.every((c) => c.level === "fits");
  if (allFit) {
    return (
      <div className="precheck" aria-live="polite">
        <p className="precheck-line precheck-quiet">
          <span className="precheck-ok" aria-hidden="true">
            {Icon.check}
          </span>
          <span>
            Checked before Start:{" "}
            {checks.map((c, i) => (
              <span key={c.spec}>
                {i > 0 && (i === checks.length - 1 ? " and " : ", ")}
                <span className="mono">{c.model}</span> on {c.source_label}
              </span>
            ))}{" "}
            {checks.length === 1 ? "fits" : "all fit"}.
          </span>
        </p>
      </div>
    );
  }
  return (
    <div className="precheck" aria-live="polite">
      <h2 className="precheck-title">Checked before Start</h2>
      <ul className="precheck-rows">
        {checks.map((c) => (
          <li key={c.spec} className="precheck-row" data-level={c.level}>
            <div className="precheck-line">
              <VerdictChip check={c} />
              <span className="mono precheck-model">{c.model}</span>
              <span className="precheck-where">
                on {c.source_label} · {rolesText(c.roles, allAgents)}
              </span>
            </div>
            {c.level !== "fits" && <CheckDetail check={c} notes={false} />}
          </li>
        ))}
      </ul>
    </div>
  );
}

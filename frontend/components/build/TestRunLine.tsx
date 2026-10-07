"use client";

import type { BuildProblem, TestFailure, TestRun, TestRunSide } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { Icon } from "@/components/shell/icons";
import { Ticks } from "./BuildRunLine";

/**
 * QA's tests, run for real (#76), as one line on the Test Cases card.
 *
 * "24 passed · 2 failed · 61% lines (jest, 38 s)" — and, folded under it, a tally of
 * what passed and failed, the coverage the runner measured (and which tool measured
 * it), each failure with its file, line and assertion, and the run's last lines. A
 * red suite opens itself. A suite that wasn't run says why, in the warning style,
 * and never shows a number: nothing measured one.
 */
export default function TestRunLine({ run }: { run: TestRun | null | undefined }) {
  if (!run) return null;
  if (run.status === "not_run") {
    return (
      <div className="testrun" data-state="not_run" role="status">
        <div className="testrun-head">
          <span className="testrun-mark" aria-hidden="true">{Icon.alert}</span>
          <span className="testrun-title">Tests</span>
          <span className="testrun-summary">{run.summary}</span>
        </div>
      </div>
    );
  }
  const failed = run.status === "failed";
  const sides = run.runs.filter((r) => r.status !== "not_run" || r.reason);
  const frameworks = Array.from(new Set(run.runs.map((r) => r.framework).filter(Boolean))).join(", ");

  return (
    <details className="testrun" data-state={run.status} open={failed || undefined}>
      <summary>
        <span className="testrun-chev" aria-hidden="true">{Icon.chevron}</span>
        <span className="testrun-mark" aria-hidden="true">{failed ? Icon.alert : Icon.check}</span>
        <span className="testrun-title">Tests</span>
        <span className="testrun-summary">
          <Ticks text={run.summary} />
          <span className="sr-only">{failed ? " — failing" : " — passing"}</span>
        </span>
        {run.kept && <span className="testrun-tag">Same tests, run on the fix</span>}
        {frameworks && <span className="testrun-where">Docker sandbox · {frameworks}</span>}
      </summary>

      <div className="testrun-body">
        {sides.map((side) => (
          <SideRun key={side.side} run={side} labelled={sides.length > 1} />
        ))}
      </div>
    </details>
  );
}

function SideRun({ run, labelled }: { run: TestRunSide; labelled: boolean }) {
  const output = run.steps.find((s) => s.name === "test") ?? run.steps[run.steps.length - 1];
  return (
    <section className="testrun-side" aria-label={`${run.side} tests`}>
      {labelled && (
        <h5 className="testrun-side-head">
          <span>{run.side}</span>
          {run.framework && <span className="testrun-side-fw">{run.framework}</span>}
          <span className="testrun-side-sum">{run.summary}</span>
        </h5>
      )}
      {run.status === "not_run" ? (
        <p className="testrun-note">Not run: {run.reason}</p>
      ) : (
        <>
          <Tally run={run} />
          <CoverageLine run={run} />
          {run.problems.length > 0 && <CouldntRun problems={run.problems} />}
          {run.failures.length > 0 && <TestFailures failures={run.failures} side={run.side} />}
          {output?.tail && (
            <details className="testrun-output">
              <summary>
                <span className="testrun-chev" aria-hidden="true">{Icon.chevron}</span>
                Output of <code className="buildrun-code">{output.label}</code>
              </summary>
              <pre className="buildrun-term" tabIndex={0} aria-label={`Output of ${output.label}`}>
                {output.tail}
              </pre>
            </details>
          )}
        </>
      )}
    </section>
  );
}

/** What passed, failed and couldn't run, as a bar — with every number written out. */
function Tally({ run }: { run: TestRunSide }) {
  const parts = [
    { key: "ok", n: run.passed, label: "passed" },
    { key: "bad", n: run.failed, label: "failed" },
    { key: "err", n: run.errored, label: "couldn't run" },
    { key: "skip", n: run.skipped, label: "skipped" },
  ].filter((p) => p.n > 0);
  const total = parts.reduce((n, p) => n + p.n, 0);
  if (total === 0) return null;
  return (
    <div className="testrun-tally">
      <span className="testrun-bar" aria-hidden="true">
        {parts.map((p) => (
          <span key={p.key} data-part={p.key} style={{ flexGrow: p.n }} />
        ))}
      </span>
      <ul className="testrun-legend">
        {parts.map((p) => (
          <li key={p.key} data-part={p.key}>
            <b>{p.n}</b> {p.label}
          </li>
        ))}
      </ul>
    </div>
  );
}

function pct(value: number): string {
  return value >= 10 || value === 0 ? `${Math.round(value)}%` : `${value.toFixed(1)}%`;
}

/** The coverage the runner measured, and what measured it — or that nothing did. */
function CoverageLine({ run }: { run: TestRunSide }) {
  const c = run.coverage;
  if (!c || c.lines_pct === null) {
    return (
      <p className="testrun-cov" data-measured="false">
        <span className="testrun-cov-label">Coverage</span>
        <span>Not measured.{run.reason ? ` ${run.reason}` : ""}</span>
      </p>
    );
  }
  return (
    <p className="testrun-cov">
      <span className="testrun-cov-label">Coverage</span>
      <span className="testrun-cov-meter" aria-hidden="true">
        <span style={{ width: `${Math.min(Math.max(c.lines_pct, 0), 100)}%` }} />
      </span>
      <span>
        <b>{pct(c.lines_pct)}</b> of lines
        {c.branches_pct !== null && (
          <>
            {" · "}
            <b>{pct(c.branches_pct)}</b> of branches
          </>
        )}
        <span className="testrun-cov-tool"> · measured by {c.tool}</span>
      </span>
    </p>
  );
}

const OWNER: Record<string, string> = { backend: "backend_engineer", frontend: "frontend_engineer" };

const KIND: Record<TestFailure["kind"], string | null> = {
  assertion: null,
  error: "setup broke",
  environment: "needs the network",
};

/** Each test that ran and failed: name, where, the assertion, and whose code it checks. */
export function TestFailures({ failures, side }: { failures: (TestFailure & { side?: string })[]; side?: string }) {
  return (
    <ul className="testrun-failures" aria-label="Failing tests">
      {failures.map((f, i) => {
        const owner = AGENT_BY_KEY[OWNER[f.side ?? side ?? ""] ?? ""];
        const where = f.line ? `${f.path}:${f.line}` : f.path;
        const kind = KIND[f.kind];
        return (
          <li key={`${f.path}-${f.name}-${i}`} className="testrun-failure">
            <div className="testrun-failure-head">
              <span className="testrun-failure-mark" aria-hidden="true">{Icon.alert}</span>
              <b className="testrun-failure-name">{f.name}</b>
              {kind && <span className="testrun-failure-kind">{kind}</span>}
            </div>
            <div className="testrun-failure-meta">
              <code className="mono">{where}</code>
              {owner && (
                <span
                  className="testrun-owner"
                  style={{ ["--agent" as string]: owner.accent }}
                  title={`The code this test checks is ${owner.codename}'s`}
                >
                  <AgentSprite agent={owner} size={16} state="queued" />
                  {owner.codename}&apos;s code
                </span>
              )}
            </div>
            {f.message && <pre className="testrun-assert mono">{f.message}</pre>}
          </li>
        );
      })}
    </ul>
  );
}

/** Test files the runner couldn't even load: QA's to fix, like a compile error. */
function CouldntRun({ problems }: { problems: BuildProblem[] }) {
  return (
    <ul className="testrun-failures" aria-label="Test files that couldn't run">
      {problems.map((p, i) => (
        <li key={`${p.path}-${i}`} className="testrun-failure" data-kind="load">
          <div className="testrun-failure-head">
            <span className="testrun-failure-mark" aria-hidden="true">{Icon.alert}</span>
            <b className="testrun-failure-name">Couldn&apos;t run</b>
            <code className="mono">{p.line ? `${p.path}:${p.line}` : p.path}</code>
          </div>
          <pre className="testrun-assert mono">{p.message}</pre>
        </li>
      ))}
    </ul>
  );
}

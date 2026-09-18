"use client";

import type { BuildProblem } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import { Icon } from "@/components/shell/icons";

/**
 * What still does not compile, by file, with the way to send each one back.
 *
 * Every line here already went back to its agent once, with the error named, and
 * came back still broken — so this is the reviewer's list, not the model's. Grouped
 * by file because a fix is per file, and each group can be aimed at the agent that
 * wrote it: "this import does not exist" is a note for Prism, not for Ledger.
 */

const KIND: Record<string, string> = {
  syntax: "Doesn't parse",
  reference: "Undeclared name",
  import: "Missing file",
  package: "Unknown package",
};

/** "uses `Header`, which…" with the name set as code rather than as backticks. */
export function withCode(message: string) {
  return message.split("`").map((part, i) =>
    i % 2 ? (
      <code key={i} className="mono">
        {part}
      </code>
    ) : (
      part
    ),
  );
}

export default function BuildProblems({
  problems,
  onRedoFile,
}: {
  problems: BuildProblem[];
  onRedoFile?: (phase: string, path: string) => void;
}) {
  const byFile = new Map<string, BuildProblem[]>();
  for (const p of problems) byFile.set(p.path, [...(byFile.get(p.path) ?? []), p]);

  return (
    <div className="build-problems">
      <div className="build-problems-head">
        <span className="label">Still doesn&apos;t compile</span>
        <span className="rule" />
        <span className="field-hint">
          {byFile.size} file{byFile.size === 1 ? "" : "s"} · {problems.length} problem
          {problems.length === 1 ? "" : "s"}
        </span>
      </div>
      <ul className="build-files">
        {[...byFile.entries()].map(([path, items]) => {
          const phase = items[0]?.phase;
          const agent = phase ? AGENT_BY_KEY[phase] : undefined;
          return (
            <li key={path} className="build-file">
              <div className="build-file-head">
                <span className="build-file-mark" aria-hidden="true">
                  {Icon.file}
                </span>
                <span className="mono build-file-path">{path}</span>
                {agent && <span className="field-hint">by {agent.codename}</span>}
                <span className="rule" />
                {phase && onRedoFile && (
                  <button
                    className="btn btn-sm btn-danger"
                    onClick={() => onRedoFile(phase, path)}
                    title={`Send this file back to ${agent?.codename ?? phase} with a note`}
                  >
                    {Icon.undo} Redo this file
                  </button>
                )}
              </div>
              <ul className="build-lines">
                {items.map((p, i) => (
                  <li key={i}>
                    <span className="badge badge-bad">{KIND[p.kind] ?? "Error"}</span>
                    {p.line ? <span className="mono build-line">line {p.line}</span> : null}
                    <span className="build-msg">{withCode(p.message)}</span>
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

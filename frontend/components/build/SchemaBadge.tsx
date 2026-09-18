import type { PhaseResult } from "@/lib/api";

/**
 * Whether an agent delivered what it was asked for — in shape, in stack, and in
 * whether its code compiles.
 *
 * Silent on the happy path: a badge on every one of eight phases saying "fine" is
 * noise, and noise is what let the original bug hide. It speaks only when something
 * happened. `null` (a row written before a check existed) says nothing either,
 * because "we did not check" must not read as "we checked and it was fine".
 *
 * The verdicts are separate badges rather than one, because they are separate
 * problems: a deliverable can match its declared shape perfectly, agree with the
 * stack, and still not parse — and a reviewer told only the loudest of the three
 * goes looking for one problem when there are two.
 */
export default function SchemaBadge({ row }: { row: PhaseResult }) {
  const badges = [];

  if (row.stack_status === "violated") {
    badges.push(
      <span
        key="stack"
        className="badge badge-bad"
        title={
          (row.stack_note ?? [])
            .map((n) => n.replace(/`/g, ""))
            .join(" ") ||
          "This phase was written against a different stack than the architecture froze."
        }
      >
        Wrong stack
      </span>,
    );
  }
  if (row.build_status === "failed") {
    const problems = row.build_note ?? [];
    const files = new Set(problems.map((p) => p.path)).size;
    badges.push(
      <span
        key="build"
        className="badge badge-bad"
        title={
          problems
            .slice(0, 4)
            .map((p) => `${p.path}${p.line ? ` line ${p.line}` : ""} — ${p.message.replace(/`/g, "")}`)
            .join("\n") || "This phase's code does not compile."
        }
      >
        {files > 1 ? `${files} files don't compile` : "Doesn't compile"}
      </span>,
    );
  } else if (row.build_status === "unchecked") {
    badges.push(
      <span
        key="build"
        className="badge"
        title="No compiler was available for some of this phase's files, so they were not checked — which is not the same as passing."
      >
        Not compiled
      </span>,
    );
  }
  if (row.schema_status === "repaired") {
    badges.push(
      <span
        key="schema"
        className="badge badge-warn"
        title={
          "This output missed the shape this agent declares and was corrected on a " +
          "second attempt. What you're reading is the corrected version."
        }
      >
        Shape repaired
      </span>,
    );
  } else if (row.schema_status === "invalid") {
    badges.push(
      <span key="schema" className="badge badge-bad" title={row.schema_note ?? undefined}>
        Shape mismatch
      </span>,
    );
  }
  return badges.length ? <>{badges}</> : null;
}

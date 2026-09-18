import type { PhaseResult } from "@/lib/api";

/**
 * Whether an agent delivered what it was asked for — in shape, and in stack.
 *
 * Silent on the happy path: a badge on every one of eight phases saying "fine" is
 * noise, and noise is what let the original bug hide. It speaks only when something
 * happened. `null` (a row written before a check existed) says nothing either,
 * because "we did not check" must not read as "we checked and it was fine".
 *
 * The two verdicts are separate badges rather than one, because they are separate
 * problems: a deliverable can match its declared shape perfectly and still be
 * written against a database nothing else in the build uses.
 */
export default function SchemaBadge({ row }: { row: PhaseResult }) {
  if (row.stack_status === "violated") {
    return (
      <span
        className="badge badge-bad"
        title={
          (row.stack_note ?? [])
            .map((n) => n.replace(/`/g, ""))
            .join(" ") ||
          "This phase was written against a different stack than the architecture froze."
        }
      >
        Wrong stack
      </span>
    );
  }
  if (row.schema_status === "repaired") {
    return (
      <span
        className="badge badge-warn"
        title={
          "This output missed the shape this agent declares and was corrected on a " +
          "second attempt. What you're reading is the corrected version."
        }
      >
        Shape repaired
      </span>
    );
  }
  if (row.schema_status === "invalid") {
    return (
      <span className="badge badge-bad" title={row.schema_note ?? undefined}>
        Shape mismatch
      </span>
    );
  }
  return null;
}

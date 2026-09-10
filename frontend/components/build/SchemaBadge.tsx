import type { PhaseResult } from "@/lib/api";

/**
 * Whether an agent returned the shape it declares.
 *
 * Silent on the happy path — a badge on every one of eight phases saying "fine" is
 * noise, and noise is what let the original bug hide. It speaks only when something
 * happened: a second attempt was needed, or the shape never arrived. `null` (a row
 * written before the check existed) says nothing either, because "we did not check"
 * must not read as "we checked and it was fine".
 */
export default function SchemaBadge({ row }: { row: PhaseResult }) {
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

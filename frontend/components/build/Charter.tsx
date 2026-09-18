import type { Charter, CharterCategory } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/**
 * The stack every agent after the architecture is held to.
 *
 * It is shown for the same reason it is enforced. One run produced a PostgreSQL
 * architecture, a Mongoose backend and pytest files aimed at `.js` controllers, and
 * the only place all three appeared together was the `.zip`. Six lines on the review
 * panel is where they appear together now, before anyone approves anything.
 *
 * `source` is on screen because it is the strength of the claim: "settled by the
 * debate" and "follows from FastAPI" are both decisions, and only one of them was
 * argued. A reviewer who disagrees with an implied choice is looking at a different
 * problem than one who disagrees with a deliberated one.
 */

const ORDER: { key: CharterCategory; label: string }[] = [
  { key: "language", label: "Language" },
  { key: "backend_framework", label: "Backend" },
  { key: "frontend_framework", label: "Frontend" },
  { key: "database", label: "Database" },
  { key: "test_runner", label: "Tests" },
  { key: "package_manager", label: "Packages" },
];

const SOURCE: Record<string, string> = {
  debate: "settled by the debate",
  system_design: "chosen by the architecture",
  implied: "follows from the above",
};

export default function CharterPanel({
  charter,
  compact = false,
}: {
  charter: Charter | null | undefined;
  /** Drop the heading when the panel already sits under one. */
  compact?: boolean;
}) {
  const rows = charter
    ? ORDER.map((o) => [o, charter[o.key]] as const).filter(
        (pair): pair is readonly [(typeof ORDER)[number], NonNullable<Charter[CharterCategory]>] =>
          Boolean(pair[1]),
      )
    : [];

  if (rows.length === 0) {
    // Saying nothing here would read as "no constraints", which is true but only by
    // accident — and the reviewer is the person who most needs to know the
    // difference between a build held to a stack and one that isn't.
    return (
      <div className="charter charter-empty">
        {Icon.info}
        <p>
          The architecture didn&apos;t name a stack this pipeline recognises, so nothing downstream
          is being checked against one. Read the code for yourself before shipping it.
        </p>
      </div>
    );
  }

  return (
    <div className="charter">
      {!compact && (
        <div className="sec-head" style={{ marginBottom: 12 }}>
          <h3 className="label">Stack charter</h3>
          <span className="rule" />
          <span className="field-hint">Frozen here · binding on every phase after</span>
        </div>
      )}
      <dl className="charter-grid">
        {rows.map(([meta, choice]) => (
          <div className="charter-item" key={meta.key} data-source={choice.source}>
            <dt>{meta.label}</dt>
            <dd>
              <span className="charter-value">{choice.label}</span>
              <span className="charter-source">{SOURCE[choice.source] ?? "agreed"}</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/**
 * Where a phase disagrees with the charter, in the words it was sent back with.
 *
 * Rendered wherever that phase is read, not only at the gate: a contradiction that
 * only appears on the stop screen is one that disappears the moment someone scrolls
 * to the file it is about.
 */
export function StackViolations({ notes }: { notes: string[] | null | undefined }) {
  if (!notes || notes.length === 0) return null;
  return (
    <div className="notice notice-bad stack-violations" role="alert">
      {Icon.alert}
      <div className="notice-body">
        <span className="notice-title">
          {notes.length === 1
            ? "This phase contradicts the stack charter"
            : `This phase contradicts the stack charter in ${notes.length} places`}
        </span>
        <ul className="stack-violation-list">
          {notes.map((note) => (
            <li key={note}>{note.replace(/`/g, "")}</li>
          ))}
        </ul>
        <span className="notice-text">
          It was sent back with these named and came back disagreeing again. Approving puts two
          incompatible halves in one archive — send it back with a note, or redo the architecture.
        </span>
      </div>
    </div>
  );
}

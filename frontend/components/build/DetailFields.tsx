import type { ReactNode } from "react";
import { BADGE_COLUMNS, badgeTone, labelize, type Cell, type Field } from "./payload";

/**
 * The rest of the payload, rendered as data rather than prose.
 *
 * `content_md` already flattens all of this into a bulleted markdown dump, which is
 * fine for a paragraph of rationale and useless for twenty API endpoints. Here a
 * uniform list of records becomes a real table with column headers you can scan down,
 * and the closed-vocabulary columns — severity, priority, HTTP method — become the
 * same badges the rest of the app uses for state.
 */

function CellValue({ column, value }: { column: string; value: Cell }) {
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="dim">—</span>;
    return (
      <ul className="cell-list">
        {value.map((v, i) => (
          <li key={i}>{v}</li>
        ))}
      </ul>
    );
  }
  if (!value) return <span className="dim">—</span>;

  if (BADGE_COLUMNS.has(column.toLowerCase())) {
    return <span className={"badge " + badgeTone(value)}>{value}</span>;
  }
  // Paths and identifiers read as data, so they get the data face.
  if (column.toLowerCase() === "path" || /^\/\S/.test(value)) {
    return <span className="mono">{value}</span>;
  }
  return <>{value}</>;
}

function FieldBlock({ field, depth }: { field: Field; depth: number }): ReactNode {
  switch (field.kind) {
    case "value":
      return (
        <div className="fact">
          <dt>{field.label}</dt>
          <dd>{field.value}</dd>
        </div>
      );

    case "text":
      return (
        <section className="detail-block">
          <h4 className="detail-h">{field.label}</h4>
          {field.value.split(/\n{2,}/).map((para, i) => (
            <p key={i} className="detail-p">
              {para.trim()}
            </p>
          ))}
        </section>
      );

    case "list":
      return (
        <section className="detail-block">
          <h4 className="detail-h">{field.label}</h4>
          <ul className="detail-list">
            {field.items.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </section>
      );

    case "table":
      return (
        <section className="detail-block">
          <h4 className="detail-h">
            {field.label} <span className="detail-count">{field.rows.length}</span>
          </h4>
          <div className="detail-table-wrap">
            <table className="detail-table">
              <thead>
                <tr>
                  {field.columns.map((c) => (
                    <th key={c} scope="col">
                      {labelize(c)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {field.rows.map((row, i) => (
                  <tr key={i}>
                    {field.columns.map((c) => (
                      <td key={c}>
                        <CellValue column={c} value={row[c] ?? ""} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      );

    case "group":
      return (
        <section className="detail-block">
          <h4 className="detail-h">{field.label}</h4>
          <DetailFields fields={field.fields} depth={depth + 1} />
        </section>
      );
  }
}

export default function DetailFields({
  fields,
  depth = 0,
}: {
  fields: Field[];
  depth?: number;
}) {
  if (fields.length === 0) return null;

  // Scalars are collected into one facts grid at the top; everything longer
  // follows in order. Reading "FastAPI · 12 endpoints" first is what makes the
  // rest scannable rather than a wall.
  const facts = fields.filter((f) => f.kind === "value");
  const rest = fields.filter((f) => f.kind !== "value");

  return (
    // The nesting rule lives on the container, not on each child: a nested *list*
    // (tech_stack.frontend) needs the same indent as a nested group, and hanging it
    // off the group case alone left those flush with their parent heading.
    <div className={"details" + (depth > 0 ? " details-nested" : "")}>
      {facts.length > 0 && (
        <dl className="facts">
          {facts.map((f) => (
            <FieldBlock key={f.label} field={f} depth={depth} />
          ))}
        </dl>
      )}
      {rest.map((f) => (
        <FieldBlock key={f.label} field={f} depth={depth} />
      ))}
    </div>
  );
}

/**
 * Turning an agent's raw JSON output into something a reviewer can actually read.
 *
 * Every phase used to be summarised down to `content_md` — two sentences of prose —
 * and that was the only thing shown at the approval gate. But the payload carries the
 * work itself: the Backend and Frontend agents return whole source trees under `files`,
 * System Design returns an API surface, a data model and a Mermaid diagram, QA returns
 * test files. Approving from the prose alone is approving something you never saw.
 *
 * The shapes vary by agent, so nothing here is keyed to a phase. Three passes over the
 * output, in order of how much a reviewer needs them:
 *
 *   1. `extractFiles`  — any list of `{path, code|content}`, wherever it lives.
 *   2. `extractMermaid`— a diagram definition, which is meant to be drawn, not printed.
 *   3. `toFields`      — everything left, shaped by what it *is* rather than what it's
 *                        called: uniform object lists become tables, string lists become
 *                        lists, long strings become prose, short ones become values.
 */

export type PayloadFile = {
  path: string;
  content: string;
  language: string;
  /** The output key it came from — `files`, `test_files`, `ci_cd`, … */
  sourceKey: string;
  lines: number;
};

export type Cell = string | string[];

export type Field =
  | { kind: "value"; label: string; value: string }
  | { kind: "text"; label: string; value: string }
  | { kind: "list"; label: string; items: string[] }
  | { kind: "table"; label: string; columns: string[]; rows: Record<string, Cell>[] }
  | { kind: "group"; label: string; fields: Field[] };

// Keys whose contents are surfaced by a dedicated view, so the details pass skips them.
// `diagram` is included because models rename the spec'd key about as often as they
// honour it — but see `looksLikeMermaid`: a key alone is not enough to claim a value.
const MERMAID_KEYS = ["architecture_diagram_mermaid", "diagram_mermaid", "mermaid", "diagram"];

// A Mermaid definition opens by naming its diagram type. Requiring that keeps a prose
// description that happens to live under `diagram` out of the diagram view — where it
// would render as a parse failure *and* vanish from Details, which skips these keys.
const MERMAID_OPENERS =
  /^(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(-v2)?|erDiagram|journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|requirementDiagram|sankey(-beta)?|xychart(-beta)?|block(-beta)?|architecture(-beta)?|C4Context)\b/;

function looksLikeMermaid(src: string): boolean {
  const firstLine = src.split("\n").find((l) => l.trim() && !l.trim().startsWith("%%"));
  return MERMAID_OPENERS.test((firstLine ?? "").trim());
}

// Acronyms that look wrong in sentence case. Everything else is lowercased after the
// first word, matching the design system's label voice.
const ACRONYMS = new Set([
  "api", "ui", "ux", "url", "uri", "id", "ci", "cd", "db", "mvp", "usd", "sql", "cors",
  "csrf", "xss", "http", "https", "jwt", "sdk", "cli", "cpu", "ram", "seo", "p0", "p1",
  "p2", "qa", "io", "aws", "gcp", "k8s", "orm", "rest", "crud", "ssl", "tls", "mfa",
]);

/** `api_endpoints` → `API endpoints`; `total_monthly_low_usd` → `Total monthly low USD`. */
export function labelize(key: string): string {
  const words = String(key).replace(/[_-]+/g, " ").trim().split(/\s+/);
  return words
    .map((word, i) => {
      const lower = word.toLowerCase();
      if (ACRONYMS.has(lower)) return lower.toUpperCase();
      if (i === 0) return lower.charAt(0).toUpperCase() + lower.slice(1);
      return lower;
    })
    .join(" ")
    .trim();
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function scalarToString(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  return String(v);
}

// ── 1. files ─────────────────────────────────────────────────────────────────
/**
 * Every file-like item in the output, from any key.
 *
 * Mirrors the backend's `artifacts._iter_files` deliberately: what the gate shows and
 * what lands in the .zip must be the same set, or approving one delivers the other.
 */
export function extractFiles(output: Record<string, unknown> | null | undefined): PayloadFile[] {
  if (!isPlainObject(output)) return [];
  // Keyed by path, last occurrence winning — the same rule the backend applies when
  // it assembles the .zip. Parity is the entire point of this function, so the tie
  // break has to match too.
  const byPath = new Map<string, PayloadFile>();

  for (const [key, value] of Object.entries(output)) {
    if (!Array.isArray(value)) continue;
    for (const item of value) {
      if (!isPlainObject(item)) continue;
      const rawPath = item.path;
      const content = item.code ?? item.content;
      if (typeof rawPath !== "string" || !rawPath.trim()) continue;
      if (typeof content !== "string" || !content.trim()) continue;

      const path = rawPath.trim().replace(/^\/+/, "");

      const language =
        (typeof item.language === "string" && item.language) ||
        (typeof item.framework === "string" && item.framework) ||
        (typeof item.tool === "string" && item.tool) ||
        "";

      byPath.set(path, {
        path,
        content,
        language,
        sourceKey: key,
        lines: content.split("\n").length,
      });
    }
  }
  return [...byPath.values()].sort((a, b) => a.path.localeCompare(b.path));
}

/**
 * Output keys whose contents the Files view is showing — the details pass skips these.
 *
 * Derived from the raw payload rather than from `extractFiles`, because a key that
 * lost the duplicate-path tie-break contributed nothing to the final list yet is
 * still file content. Reading it off the deduped result let such a key reappear in
 * Details as a raw dump of source code.
 */
export function fileKeys(output: Record<string, unknown> | null | undefined): Set<string> {
  const keys = new Set<string>();
  if (!isPlainObject(output)) return keys;
  for (const [key, value] of Object.entries(output)) {
    if (!Array.isArray(value)) continue;
    const isFileList = value.some(
      (item) =>
        isPlainObject(item) &&
        typeof item.path === "string" &&
        item.path.trim() !== "" &&
        typeof (item.code ?? item.content) === "string",
    );
    if (isFileList) keys.add(key);
  }
  return keys;
}

// ── 2. diagram ───────────────────────────────────────────────────────────────
/** The drawable diagram and the key it came from, or null when there is none. */
function findMermaid(
  output: Record<string, unknown> | null | undefined,
): { key: string; src: string } | null {
  if (!isPlainObject(output)) return null;
  for (const key of MERMAID_KEYS) {
    const raw = output[key];
    if (typeof raw !== "string") continue;
    // Models wrap the definition in a fence about half the time.
    const src = raw.replace(/^\s*```(?:mermaid)?\s*/i, "").replace(/\s*```\s*$/, "").trim();
    if (src && looksLikeMermaid(src)) return { key, src };
  }
  return null;
}

/** The Mermaid definition, unfenced. Returns null when the agent produced none. */
export function extractMermaid(
  output: Record<string, unknown> | null | undefined,
): string | null {
  return findMermaid(output)?.src ?? null;
}

// ── 3. everything else ───────────────────────────────────────────────────────
/** A string long enough to want a paragraph rather than a single line. */
function isProse(v: string): boolean {
  return v.includes("\n") || v.length > 140;
}

function toCell(value: unknown): Cell {
  if (Array.isArray(value)) {
    return value.map((v) => (isPlainObject(v) ? JSON.stringify(v) : scalarToString(v)));
  }
  if (isPlainObject(value)) {
    return Object.entries(value).map(([k, v]) => `${labelize(k)}: ${scalarToString(v)}`);
  }
  return scalarToString(value);
}

/**
 * A list of objects becomes a table when the objects agree on their shape — which is
 * how a reviewer wants to read an API surface or a findings list. A wide or ragged
 * list would make a table with mostly-empty cells, so those stay as grouped records.
 */
function tableColumns(items: Record<string, unknown>[]): string[] | null {
  const columns: string[] = [];
  for (const item of items) {
    for (const key of Object.keys(item)) {
      if (!columns.includes(key)) columns.push(key);
    }
  }
  if (columns.length === 0 || columns.length > 5) return null;

  // Ragged: a column present in only a minority of rows makes a sparse table.
  const shared = columns.filter(
    (c) => items.filter((i) => i[c] !== undefined && i[c] !== null).length >= items.length * 0.6,
  );
  return shared.length === columns.length ? columns : null;
}

function fieldFor(label: string, value: unknown, depth: number): Field | null {
  if (value === null || value === undefined || value === "") return null;

  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    const objects = value.filter(isPlainObject) as Record<string, unknown>[];

    if (objects.length === value.length) {
      const columns = depth === 0 ? tableColumns(objects) : null;
      if (columns) {
        return {
          kind: "table",
          label,
          columns,
          rows: objects.map((item) =>
            Object.fromEntries(columns.map((c) => [c, toCell(item[c])])),
          ),
        };
      }
      // Too wide or too ragged for a table — each record gets its own block.
      const fields = objects
        .map((item, i) => {
          const nested = toFields(item, new Set(), depth + 1);
          return nested.length
            ? ({ kind: "group", label: `${label} ${i + 1}`, fields: nested } as Field)
            : null;
        })
        .filter((f): f is Field => f !== null);
      return fields.length ? { kind: "group", label, fields } : null;
    }

    return { kind: "list", label, items: value.map(scalarToString).filter(Boolean) };
  }

  if (isPlainObject(value)) {
    const fields = toFields(value, new Set(), depth + 1);
    return fields.length ? { kind: "group", label, fields } : null;
  }

  const text = scalarToString(value);
  if (!text) return null;
  return isProse(text) ? { kind: "text", label, value: text } : { kind: "value", label, value: text };
}

/**
 * The output's remaining keys as renderable fields, in the agent's own order.
 *
 * That order is not arbitrary — it comes from each agent's `output_spec`, which is
 * written to read top-down (System Design leads with `architecture_overview`, then the
 * stack, then components, then the API surface). Re-sorting by shape moved the lede
 * below a bullet list. Scalars are the one exception, and `DetailFields` hoists those
 * into a single facts grid rather than this function reordering anything.
 */
export function toFields(
  output: Record<string, unknown> | null | undefined,
  skipKeys: Set<string> = new Set(),
  depth = 0,
): Field[] {
  if (!isPlainObject(output)) return [];

  // Exactly one key is claimed by the diagram view. Any *other* diagram-ish key —
  // a `diagram` holding a prose description alongside a real mermaid definition —
  // still belongs in Details rather than disappearing between the two views.
  const drawnKey = findMermaid(output)?.key;

  const fields: Field[] = [];
  for (const [key, value] of Object.entries(output)) {
    if (skipKeys.has(key)) continue;
    if (key === drawnKey) continue;
    const field = fieldFor(labelize(key), value, depth);
    if (field) fields.push(field);
  }

  return fields;
}

// ── presentation hints ───────────────────────────────────────────────────────
/** Columns whose values are a small closed vocabulary and read better as badges. */
export const BADGE_COLUMNS = new Set(["priority", "severity", "method", "status", "risk"]);

/** Maps a severity/priority value onto the design system's state colours. */
export function badgeTone(value: string): string {
  const v = value.trim().toLowerCase();
  if (["critical", "high", "p0", "blocker"].includes(v)) return "badge-bad";
  if (["medium", "p1", "warning", "moderate"].includes(v)) return "badge-warn";
  if (["low", "p2", "info", "minor"].includes(v)) return "badge-ok";
  if (["get", "head", "options"].includes(v)) return "badge-run";
  if (["post", "put", "patch"].includes(v)) return "badge-warn";
  if (v === "delete") return "badge-bad";
  return "";
}

/** "1,240 lines across 7 files" — the one-line answer to "what am I approving?". */
export function fileSummary(files: PayloadFile[]): string {
  if (files.length === 0) return "";
  const lines = files.reduce((n, f) => n + f.lines, 0);
  return `${files.length} file${files.length === 1 ? "" : "s"} · ${lines.toLocaleString()} lines`;
}

"use client";

import { useEffect, useMemo, useState } from "react";
import type { PreviewLocate, PreviewSection, PreviewTheme, ThemeTokens } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import * as tw from "./tw";

/**
 * The inspector: what is selected, and every way to change it.
 *
 * Direct edits — text, size, colour, spacing, layout, shape — show in the mockup at
 * once and wait in Pending changes until Apply saves them as one revision. None of
 * them asks a model. "Ask the crew" is the one control that does, and it sends only
 * the selected element. Site style changes the design tokens every page shares.
 *
 * Its controls are the Tailwind scale, stepped, rather than free numbers: the
 * mockup is written in it, and a value off the scale is one the model's next edit
 * would not recognise.
 *
 * On the running app (#78) the same controls change the code: the selection names the
 * file and lines that draw it, a direct edit is written into that JSX when the code
 * says it plainly (and says why not when it doesn't), and "Ask the crew" names the file
 * it will change before anything is sent. Colours are the app's own Tailwind families.
 */

export type ElementInfo = {
  oid: string;
  tag: string;
  name: string;
  label: string;
  crumbs: { oid: string; name: string }[];
  classes: string;
  /** The words, when they can be typed over; null when they can't. */
  text: string | null;
  /** Cloned from a list's template: one of many rows drawn from one pattern. */
  templated: boolean;
  /** Filled in from the mockup's data (a record field, a count, the year). */
  filled: boolean;
  section: string | null;
  hasChildren: boolean;
  attrs: { href: string | null; src: string | null; alt: string | null };
  style: Record<string, string>;
};
export type Selection = {
  info: ElementInfo;
  others: { oid: string; name: string; templated: boolean; classes: string }[];
};

export type Pending = { id: number; oid: string; key: string; label: string };

type Props = {
  selection: Selection | null;
  /** The classes the selected element has now, pending changes included. */
  classesOf: (oid: string, fallback: string) => string;
  theme: PreviewTheme | null;
  sections: { key: string; title: string; sections: PreviewSection[] }[];
  pending: Pending[];
  canUndoPending: boolean;
  canRedoPending: boolean;
  busy: boolean;
  onClasses: (key: string, label: string, compute: (classes: string) => string[]) => void;
  onText: (text: string) => void;
  onAttr: (name: "href" | "alt", value: string) => void;
  onSelect: (oid: string | null) => void;
  onSection: (id: string) => void;
  onTypeOnPage: () => void;
  onAsk: (instruction: string) => Promise<boolean>;
  onUndoPending: () => void;
  onRedoPending: () => void;
  onDiscard: (id: number) => void;
  onDiscardAll: () => void;
  onApply: () => void;
  onTheme: (changes: Partial<ThemeTokens>) => Promise<boolean>;
  /** What the selection is part of: the running app, whose code it changes, or the sketch. */
  scope?: "app" | "sketch";
  /** On the app: where the selected element is drawn, and what can change there. */
  where?: PreviewLocate | null;
  /** On the app: why nothing can be changed right now. */
  blocked?: string | null;
};

type Tab = "element" | "site";

/** `frontend/components/Navbar.tsx` → `components/Navbar.tsx`. */
function short(path: string): string {
  return path.replace(/^frontend\//, "");
}

export default function Inspector(props: Props) {
  const [tab, setTab] = useState<Tab>("element");
  return (
    <aside className="pv-inspector" aria-label="Inspector">
      <div className="pv-tabs" role="tablist" aria-label="What to edit">
        <button
          type="button"
          role="tab"
          id="pv-tab-element"
          aria-selected={tab === "element"}
          aria-controls="pv-panel"
          className="pv-tab"
          onClick={() => setTab("element")}
        >
          Element
        </button>
        <button
          type="button"
          role="tab"
          id="pv-tab-site"
          aria-selected={tab === "site"}
          aria-controls="pv-panel"
          className="pv-tab"
          onClick={() => setTab("site")}
        >
          Site style
        </button>
      </div>
      <div className="pv-panel" id="pv-panel" role="tabpanel" aria-labelledby={`pv-tab-${tab}`}>
        {props.blocked && (
          <div className="notice notice-warn pv-note" role="note">
            {Icon.info}
            <div className="notice-body">
              <span className="notice-text">{props.blocked}</span>
            </div>
          </div>
        )}
        {tab === "element" ? (
          <ElementPanel {...props} />
        ) : (
          <SitePanel theme={props.theme} busy={props.busy || Boolean(props.blocked)} onTheme={props.onTheme} app={props.scope === "app"} />
        )}
      </div>
      <PendingBar {...props} />
    </aside>
  );
}

// ── the element tab ──────────────────────────────────────────────────────────
function ElementPanel(p: Props) {
  const sel = p.selection;
  if (!sel) {
    return (
      <div className="pv-empty">
        <p className="pv-empty-lead">
          {p.scope === "app" ? "Click anything in the app to select it, and see the code that draws it." : "Click anything in the sketch to select it."}
        </p>
        <ul className="pv-keys">
          <li>
            <kbd>↑</kbd> / <kbd>Esc</kbd> parent · <kbd>↵</kbd> first child
          </li>
          <li>
            <kbd>⇧</kbd>-click to add to the selection · right-click for every layer under the pointer
          </li>
          <li>
            Double-click text to type over it · <kbd>S</kbd> or <kbd>⌘I</kbd> to use the site
          </li>
        </ul>
        {p.sections.length > 0 && (
          <div className="pv-sections">
            {p.sections.map((g) => (
              <div key={g.key} className="pv-sec-group">
                <span className="pv-label">{g.title}</span>
                <div className="pv-sec-list">
                  {g.sections.map((s) => (
                    <button key={s.id} type="button" className="pv-sec" onClick={() => p.onSection(s.id)}>
                      {s.label}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }
  return <ElementEditor key={sel.info.oid + ":" + sel.others.map((o) => o.oid).join(",")} {...p} selection={sel} />;
}

function ElementEditor(p: Props & { selection: Selection }) {
  const { info, others } = p.selection;
  const classes = p.classesOf(info.oid, info.classes);
  const onApp = p.scope === "app";
  const where = onApp ? p.where ?? null : null;
  // On the app: written by someone other than the frontend, or not found in the code.
  const foreign = Boolean(where && (where.owner || (where.why && where.start_line == null)));
  const textWhy = where && where.text && where.text.editable === false ? where.text.why ?? null : null;
  const classWhy = where && where.classes && where.classes.editable === false ? where.classes.why ?? null : null;
  const direct = !info.templated && !foreign && !(onApp && p.blocked);
  const styled = direct && !classWhy;
  const parent = info.crumbs.length > 1 ? info.crumbs[info.crumbs.length - 2] : null;
  const count = others.length + 1;
  const isText = /^(h[1-6]|p|span|a|button|li|label|strong|em|small|td|th|blockquote|figcaption)$/.test(info.tag) || info.text !== null;
  const layoutish = /flex|grid/.test(info.style.display || "") || info.hasChildren;

  return (
    <div className="pv-editor">
      <header className="pv-sel">
        <nav className="pv-crumbs" aria-label="Ancestors">
          {info.crumbs.map((c, i) => (
            <span key={c.oid} className="pv-crumb-wrap">
              {i > 0 && (
                <span className="pv-crumb-sep" aria-hidden="true">
                  ›
                </span>
              )}
              <button
                type="button"
                className="pv-crumb"
                aria-current={i === info.crumbs.length - 1 ? "true" : undefined}
                onClick={() => p.onSelect(c.oid)}
              >
                {c.name}
              </button>
            </span>
          ))}
        </nav>
        <div className="pv-sel-head">
          <h3 className="pv-sel-name">{count > 1 ? `${count} elements` : info.name}</h3>
          <span className="badge badge-mono">{count > 1 ? "multi" : `<${info.tag}>`}</span>
        </div>
        {onApp && (
          <p className="pv-src" title={where ? `${where.path}${where.start_line ? `, lines ${where.start_line}–${where.end_line}` : ""}` : undefined}>
            {Icon.file}
            {where ? (
              <>
                <code className="pv-src-path">{short(where.path)}</code>
                {where.start_line ? (
                  <span className="pv-src-lines">
                    {where.start_line === where.end_line ? `line ${where.start_line}` : `lines ${where.start_line}–${where.end_line}`}
                  </span>
                ) : null}
              </>
            ) : (
              <span className="pv-src-lines">Finding it in the code…</span>
            )}
          </p>
        )}
        <div className="pv-sel-actions">
          <button type="button" className="btn btn-sm" disabled={!parent} onClick={() => parent && p.onSelect(parent.oid)} title="Select the parent (↑ or Esc)">
            {Icon.arrowUp} Parent
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => p.onSelect(null)} title="Deselect">
            {Icon.close} Deselect
          </button>
        </div>
      </header>

      {foreign && where && (
        <div className="notice notice-warn pv-note" role="note">
          {Icon.info}
          <div className="notice-body">
            <span className="notice-title">Not the crew&apos;s frontend code</span>
            <span className="notice-text">{where.why}</span>
          </div>
        </div>
      )}

      {info.templated && (
        <div className="notice notice-warn pv-note" role="note">
          {Icon.info}
          <div className="notice-body">
            <span className="notice-title">One row of a list</span>
            <span className="notice-text">
              Every row comes from one pattern, so you can&apos;t edit it directly. Ask the crew below to
              change the pattern for all rows.
            </span>
          </div>
        </div>
      )}

      {direct && count === 1 && (info.text !== null || info.tag === "a" || info.tag === "img") && (
        <Group title="Content">
          {info.text !== null && !textWhy && <TextField value={info.text} onCommit={p.onText} onTypeOnPage={p.onTypeOnPage} />}
          {info.text !== null && textWhy && <p className="pv-hint pv-why">{textWhy}</p>}
          {info.filled && <p className="pv-hint">These words come from the mockup&apos;s sample data.</p>}
          {info.tag === "a" && <LinkField value={info.attrs.href ?? ""} onCommit={(v) => p.onAttr("href", v)} />}
          {info.tag === "img" && (
            <Field label="Alt text">
              <CommitInput value={info.attrs.alt ?? ""} placeholder="Describe the picture" onCommit={(v) => p.onAttr("alt", v)} />
            </Field>
          )}
        </Group>
      )}

      {direct && classWhy && <p className="pv-hint pv-why">{classWhy}</p>}

      {styled && (
        <>
          {isText && (
            <Group title="Typography">
              <Row label="Size">
                <Stepper
                  value={tw.read(classes, tw.G.size)}
                  list={tw.SIZES}
                  fallback="base"
                  hint={info.style.fontSize}
                  variants={tw.hasVariants(classes, tw.G.size)}
                  onChange={(v) => p.onClasses("size", "Size", (c) => tw.set(c, tw.G.size, v))}
                />
              </Row>
              <Row label="Weight">
                <Select
                  value={tw.read(classes, tw.G.weight)}
                  options={tw.WEIGHTS}
                  placeholder={`Inherit (${info.style.fontWeight})`}
                  onChange={(v) => p.onClasses("weight", "Weight", (c) => tw.set(c, tw.G.weight, v))}
                />
              </Row>
              <Row label="Font">
                <Seg
                  value={tw.read(classes, tw.G.family)}
                  options={
                    onApp
                      ? [
                          ["sans", "Sans"],
                          ["serif", "Serif"],
                          ["mono", "Mono"],
                        ]
                      : [
                          ["display", "Display"],
                          ["sans", "Body"],
                          ["mono", "Mono"],
                        ]
                  }
                  onChange={(v) => p.onClasses("family", "Font", (c) => tw.set(c, tw.G.family, v))}
                />
              </Row>
              <Row label="Line">
                <Select
                  value={tw.read(classes, tw.G.leading)}
                  options={tw.LEADING}
                  placeholder="Inherit"
                  onChange={(v) => p.onClasses("leading", "Line height", (c) => tw.set(c, tw.G.leading, v))}
                />
              </Row>
              <Row label="Align">
                <Seg
                  value={tw.read(classes, tw.G.align)}
                  options={tw.ALIGN.map((a) => [a, a[0].toUpperCase() + a.slice(1)] as [string, string])}
                  onChange={(v) => p.onClasses("align", "Alignment", (c) => tw.set(c, tw.G.align, v))}
                />
              </Row>
            </Group>
          )}

          <Group title="Colour">
            <Row label="Text">
              <Swatches
                theme={p.theme}
                app={onApp}
                value={tw.read(classes, tw.G.color)}
                onChange={(v) => p.onClasses("color", "Text colour", (c) => tw.set(c, tw.G.color, v))}
              />
            </Row>
            <Row label="Fill">
              <Swatches
                theme={p.theme}
                app={onApp}
                value={tw.read(classes, tw.G.bg)}
                onChange={(v) => p.onClasses("bg", "Background", (c) => tw.set(c, tw.G.bg, v))}
              />
            </Row>
          </Group>

          <Group title="Spacing">
            <div className="pv-grid2">
              <Row label="Pad ↔">
                <Stepper
                  value={tw.readSpace(classes, "p", "x")}
                  list={tw.SPACE}
                  fallback="0"
                  hint={(info.style.padding || "").split(" ")[1]}
                  onChange={(v) => p.onClasses("px", "Padding", (c) => tw.setSpace(c, "p", "x", v))}
                />
              </Row>
              <Row label="Pad ↕">
                <Stepper
                  value={tw.readSpace(classes, "p", "y")}
                  list={tw.SPACE}
                  fallback="0"
                  hint={(info.style.padding || "").split(" ")[0]}
                  onChange={(v) => p.onClasses("py", "Padding", (c) => tw.setSpace(c, "p", "y", v))}
                />
              </Row>
              <Row label="Margin ↔">
                <Stepper
                  value={tw.readSpace(classes, "m", "x")}
                  list={[...tw.SPACE, "auto"]}
                  fallback="0"
                  hint={(info.style.margin || "").split(" ")[1]}
                  onChange={(v) => p.onClasses("mx", "Margin", (c) => tw.setSpace(c, "m", "x", v))}
                />
              </Row>
              <Row label="Margin ↕">
                <Stepper
                  value={tw.readSpace(classes, "m", "y")}
                  list={tw.SPACE}
                  fallback="0"
                  hint={(info.style.margin || "").split(" ")[0]}
                  onChange={(v) => p.onClasses("my", "Margin", (c) => tw.setSpace(c, "m", "y", v))}
                />
              </Row>
            </div>
          </Group>

          {layoutish && (
            <Group title="Layout">
              <Row label="Display">
                <Seg
                  value={tw.read(classes, tw.G.display)}
                  options={[
                    ["block", "Block"],
                    ["flex", "Flex"],
                    ["grid", "Grid"],
                  ]}
                  onChange={(v) => p.onClasses("display", "Display", (c) => tw.set(c, tw.G.display, v))}
                />
              </Row>
              {/flex/.test(tw.read(classes, tw.G.display) ?? info.style.display ?? "") && (
                <Row label="Direction">
                  <Seg
                    value={tw.read(classes, tw.G.direction)}
                    options={[
                      ["row", "Row"],
                      ["col", "Column"],
                    ]}
                    onChange={(v) => p.onClasses("direction", "Direction", (c) => tw.set(c, tw.G.direction, v))}
                  />
                </Row>
              )}
              <Row label="Align">
                <Seg
                  value={tw.read(classes, tw.G.items)}
                  options={tw.ITEMS.map((a) => [a, a[0].toUpperCase() + a.slice(1)] as [string, string])}
                  onChange={(v) => p.onClasses("items", "Align items", (c) => tw.set(c, tw.G.items, v))}
                />
              </Row>
              <Row label="Justify">
                <Seg
                  value={tw.read(classes, tw.G.justify)}
                  options={tw.JUSTIFY.map((a) => [a, a === "between" ? "Spread" : a[0].toUpperCase() + a.slice(1)] as [string, string])}
                  onChange={(v) => p.onClasses("justify", "Justify", (c) => tw.set(c, tw.G.justify, v))}
                />
              </Row>
              <Row label="Gap">
                <Stepper
                  value={tw.read(classes, tw.G.gap)}
                  list={tw.SPACE}
                  fallback="0"
                  hint={info.style.gap !== "normal" ? info.style.gap : undefined}
                  onChange={(v) => p.onClasses("gap", "Gap", (c) => tw.set(c, tw.G.gap, v))}
                />
              </Row>
            </Group>
          )}

          <Group title="Shape">
            <Row label="Radius">
              <Select
                value={tw.read(classes, tw.G.radius)}
                options={tw.RADII}
                labels={{ "": "default" }}
                placeholder="None set"
                onChange={(v) => p.onClasses("radius", "Radius", (c) => tw.set(c, tw.G.radius, v))}
              />
            </Row>
            <Row label="Shadow">
              <Select
                value={tw.read(classes, tw.G.shadow)}
                options={tw.SHADOWS}
                labels={{ "": "default" }}
                placeholder="None set"
                onChange={(v) => p.onClasses("shadow", "Shadow", (c) => tw.set(c, tw.G.shadow, v))}
              />
            </Row>
            <Row label="Border">
              <Seg
                value={tw.read(classes, tw.G.border)}
                options={[
                  ["0", "None"],
                  ["", "1px"],
                  ["2", "2px"],
                ]}
                onChange={(v) =>
                  p.onClasses("border", "Border", (c) => {
                    const next = tw.set(c, tw.G.border, v);
                    // A border you can see needs a colour; the hairline token is the site's own.
                    const hairline = onApp ? `border-${appFamilies(p.theme).neutral}-200` : "border-line";
                    return v !== "0" && v !== null && tw.read(next.join(" "), tw.G.borderColor) === null ? [...next, hairline] : next;
                  })
                }
              />
            </Row>
          </Group>
        </>
      )}

      <AskBox
        busy={p.busy}
        blocked={p.pending.length > 0}
        name={info.name}
        onAsk={p.onAsk}
        file={onApp ? (where && !foreign ? short(where.path) : null) : undefined}
        refused={onApp ? (foreign ? where?.why ?? null : p.blocked ?? null) : null}
      />
    </div>
  );
}

// ── pieces ───────────────────────────────────────────────────────────────────
function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="pv-group">
      <h4 className="pv-label">{title}</h4>
      <div className="pv-group-body">{children}</div>
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="pv-row">
      <span className="pv-row-label">{label}</span>
      <div className="pv-row-ctl">{children}</div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="pv-field">
      <span className="pv-row-label">{label}</span>
      {children}
    </label>
  );
}

function Stepper({
  value,
  list,
  fallback,
  hint,
  variants,
  onChange,
}: {
  value: string | null;
  list: string[];
  fallback: string;
  hint?: string;
  variants?: boolean;
  onChange: (v: string) => void;
}) {
  const at = value === null ? -1 : list.indexOf(value);
  return (
    <div className="pv-stepper">
      <button
        type="button"
        className="pv-step"
        aria-label="Smaller"
        disabled={at === 0}
        onClick={() => onChange(tw.step(list, value, -1, fallback))}
      >
        −
      </button>
      <span className="pv-step-val mono" title={variants ? "Changes here replace the per-screen-size values too." : undefined}>
        {value === null ? <span className="pv-muted">{hint ?? "—"}</span> : value === "" ? "default" : value}
        {variants && <span className="pv-dot" aria-label="has per-screen-size values" />}
      </span>
      <button
        type="button"
        className="pv-step"
        aria-label="Larger"
        disabled={at === list.length - 1}
        onClick={() => onChange(tw.step(list, value, +1, fallback))}
      >
        +
      </button>
    </div>
  );
}

function Select({
  value,
  options,
  placeholder,
  labels,
  onChange,
}: {
  value: string | null;
  options: string[];
  placeholder: string;
  labels?: Record<string, string>;
  onChange: (v: string | null) => void;
}) {
  return (
    <select
      className="select pv-select"
      value={value === null ? "__none" : value}
      onChange={(e) => onChange(e.target.value === "__none" ? null : e.target.value)}
    >
      <option value="__none">{placeholder}</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {labels?.[o] ?? o}
        </option>
      ))}
    </select>
  );
}

function Seg({
  value,
  options,
  onChange,
}: {
  value: string | null;
  options: [string, string][];
  onChange: (v: string | null) => void;
}) {
  return (
    <div className="pv-seg" role="group">
      {options.map(([v, label]) => (
        <button
          key={v || "default"}
          type="button"
          className="pv-seg-btn"
          aria-pressed={value === v}
          onClick={() => onChange(value === v ? null : v)}
          title={value === v ? "Click again to clear" : undefined}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

const BASIC: [string, string][] = [
  ["white", "#ffffff"],
  ["gray-100", "#f3f4f6"],
  ["gray-500", "#6b7280"],
  ["gray-900", "#111827"],
  ["blue-600", "#2563eb"],
  ["red-600", "#dc2626"],
  ["green-600", "#16a34a"],
  ["amber-500", "#f59e0b"],
];

/** The app's own palette families (#78): what its classes already say `bg-indigo-600` with. */
function appFamilies(theme: PreviewTheme | null): { primary: string; accent: string; neutral: string } {
  const f = (theme as (PreviewTheme & { families?: Record<string, string | null> }) | null)?.families ?? {};
  return { primary: f.primary || "blue", accent: f.accent || "amber", neutral: f.neutral || "gray" };
}

function Swatches({
  theme,
  value,
  app = false,
  onChange,
}: {
  theme: PreviewTheme | null;
  value: string | null;
  app?: boolean;
  onChange: (v: string | null) => void;
}) {
  const list: [string, string][] = useMemo(() => {
    if (app) {
      // Real Tailwind classes, in the families the app already uses — a class the app's
      // config doesn't define would show here and do nothing once built.
      const fam = appFamilies(theme);
      const P = theme?.palette.primary ?? {};
      const A = theme?.palette.accent ?? {};
      const N = theme?.palette.neutral ?? {};
      return [
        [`${fam.neutral}-900`, N["900"] ?? "#111827"],
        [`${fam.neutral}-600`, N["600"] ?? "#4b5563"],
        [`${fam.neutral}-200`, N["200"] ?? "#e5e7eb"],
        [`${fam.neutral}-50`, N["50"] ?? "#f9fafb"],
        ["white", "#ffffff"],
        [`${fam.primary}-600`, theme?.current.primary ?? "#2563eb"],
        [`${fam.primary}-700`, P["700"] ?? "#1d4ed8"],
        [`${fam.primary}-100`, P["100"] ?? "#dbeafe"],
        [`${fam.primary}-50`, P["50"] ?? "#eff6ff"],
        [`${fam.accent}-500`, A["500"] ?? "#f59e0b"],
        [`${fam.accent}-100`, A["100"] ?? "#fef3c7"],
      ] as [string, string][];
    }
    if (!theme) return BASIC;
    const P = theme.palette.primary;
    const A = theme.palette.accent;
    const N = theme.palette.neutral;
    return [
      ["ink", N["900"]],
      ["muted", N["600"]],
      ["line", N["200"]],
      ["bg", N["50"]],
      ["white", "#ffffff"],
      ["primary", theme.current.primary],
      ["primary-700", P["700"]],
      ["primary-100", P["100"]],
      ["primary-50", P["50"]],
      ["accent", theme.current.accent],
      ["accent-100", A["100"]],
    ];
  }, [theme, app]);
  return (
    <div className="pv-swatches" role="group">
      <button type="button" className="pv-swatch pv-swatch-none" aria-pressed={value === null} onClick={() => onChange(null)} title="Inherit">
        <span className="sr-only">Inherit</span>
      </button>
      {list.map(([token, hex]) => (
        <button
          key={token}
          type="button"
          className="pv-swatch"
          style={{ background: hex }}
          aria-pressed={value === token}
          onClick={() => onChange(token)}
          title={token}
        >
          <span className="sr-only">{token}</span>
        </button>
      ))}
    </div>
  );
}

function CommitInput({ value, placeholder, onCommit }: { value: string; placeholder?: string; onCommit: (v: string) => void }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => {
    if (draft !== value) onCommit(draft);
  };
  return (
    <input
      className="input pv-input"
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") setDraft(value);
      }}
    />
  );
}

function TextField({ value, onCommit, onTypeOnPage }: { value: string; onCommit: (v: string) => void; onTypeOnPage: () => void }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => {
    const next = draft.replace(/\s+/g, " ").trim();
    if (next && next !== value) onCommit(next);
  };
  return (
    <div className="pv-text">
      <textarea
        className="textarea pv-textarea"
        aria-label="Text"
        rows={Math.min(5, Math.max(2, Math.ceil(value.length / 34)))}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            commit();
          }
          if (e.key === "Escape") setDraft(value);
        }}
      />
      <button type="button" className="pv-link" onClick={onTypeOnPage}>
        Type on the page instead
      </button>
    </div>
  );
}

function LinkField({ value, onCommit }: { value: string; onCommit: (v: string) => void }) {
  return (
    <Field label="Link">
      <CommitInput value={value} placeholder="#/page or https://…" onCommit={onCommit} />
    </Field>
  );
}

function AskBox({
  busy,
  blocked,
  name,
  onAsk,
  file,
  refused,
}: {
  busy: boolean;
  blocked: boolean;
  name: string;
  onAsk: (instruction: string) => Promise<boolean>;
  /** On the app: the file the crew will change — null while it's being found. */
  file?: string | null;
  /** On the app: why the crew can't change this. */
  refused?: string | null;
}) {
  const [text, setText] = useState("");
  const onApp = file !== undefined;
  const cannot = onApp && (Boolean(refused) || !file);
  const submit = async () => {
    if (!text.trim() || busy || blocked || cannot) return;
    if (await onAsk(text.trim())) setText("");
  };
  return (
    <section className="pv-group pv-ask">
      <h4 className="pv-label">
        {Icon.sparkle} Ask the crew
      </h4>
      <textarea
        className="textarea pv-textarea"
        rows={3}
        aria-label={`Describe a change to ${name}`}
        placeholder="e.g. add a small badge that says New, and make it feel more premium"
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
        }}
      />
      <div className="pv-ask-foot">
        <span className="pv-hint">
          {blocked ? (
            "Apply or discard your pending changes first."
          ) : onApp ? (
            refused ? (
              refused
            ) : file ? (
              <>
                The crew will change <code className="pv-src-path">{file}</code>, then it&apos;s checked and built. ⌘↵ to send.
              </>
            ) : (
              "Finding the file that draws it…"
            )
          ) : (
            "Only this element goes to the model. ⌘↵ to send."
          )}
        </span>
        <button type="button" className="btn btn-sm btn-accent" disabled={busy || blocked || cannot || !text.trim()} onClick={submit}>
          {busy && <span className="btn-spinner" aria-hidden="true" />}
          {busy ? "Working…" : "Ask"}
        </button>
      </div>
    </section>
  );
}

// ── pending changes ──────────────────────────────────────────────────────────
function PendingBar(p: Props) {
  if (!p.pending.length && !p.canRedoPending) return null;
  return (
    <div className="pv-pending" aria-live="polite">
      <div className="pv-pending-head">
        <span className="pv-pending-title">
          {p.pending.length ? `${p.pending.length} unsaved change${p.pending.length === 1 ? "" : "s"}` : "No unsaved changes"}
        </span>
        <div className="pv-pending-hist">
          <button type="button" className="prev-tool" disabled={!p.canUndoPending} onClick={p.onUndoPending} aria-label="Undo change" title="Undo (⌘Z)">
            {Icon.undo}
          </button>
          <button type="button" className="prev-tool" disabled={!p.canRedoPending} onClick={p.onRedoPending} aria-label="Redo change" title="Redo (⇧⌘Z)">
            {Icon.redo}
          </button>
        </div>
      </div>
      {p.pending.length > 0 && (
        <>
          <ul className="pv-pending-list">
            {p.pending.map((c) => (
              <li key={c.id}>
                <span className="pv-pending-label" title={c.label}>
                  {c.label}
                </span>
                <button type="button" className="pv-x" onClick={() => p.onDiscard(c.id)} aria-label={`Discard ${c.label}`} title="Discard this change">
                  {Icon.close}
                </button>
              </li>
            ))}
          </ul>
          <div className="pv-pending-actions">
            <button type="button" className="btn btn-sm btn-ghost" disabled={p.busy} onClick={p.onDiscardAll}>
              Discard all
            </button>
            <button type="button" className="btn btn-sm btn-primary" disabled={p.busy} onClick={p.onApply}>
              {p.busy && <span className="btn-spinner" aria-hidden="true" />}
              Apply {p.pending.length}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// ── site style ───────────────────────────────────────────────────────────────
function SitePanel({
  theme,
  busy,
  onTheme,
  app = false,
}: {
  theme: PreviewTheme | null;
  busy: boolean;
  onTheme: (changes: Partial<ThemeTokens>) => Promise<boolean>;
  /** The app's style (#78): written into its Tailwind config, so it ships. */
  app?: boolean;
}) {
  const [draft, setDraft] = useState<ThemeTokens | null>(theme?.current ?? null);
  useEffect(() => setDraft(theme?.current ?? null), [theme]);

  if (!theme || !draft) {
    return app ? (
      <div className="pv-empty">
        <p className="pv-empty-lead">The app&apos;s style can be changed once it&apos;s running.</p>
      </div>
    ) : (
      <div className="pv-empty">
        <p className="pv-empty-lead">This sketch was drawn before site styles existed.</p>
        <p className="pv-hint">Redraw it to change its fonts and colours from here, on every page at once.</p>
      </div>
    );
  }
  const changed = (Object.keys(draft) as (keyof ThemeTokens)[]).filter((k) => draft[k] !== theme.current[k]);
  const put = <K extends keyof ThemeTokens>(k: K, v: ThemeTokens[K]) => setDraft({ ...draft, [k]: v });
  const font = theme.fonts.find((f) => f.id === draft.font_pair);
  const hexOk = (v: string) => /^#[0-9a-fA-F]{6}$/.test(v);

  return (
    <div className="pv-editor">
      <p className="pv-hint">
        {app
          ? "Written into the app's Tailwind config, so every page — and the download and the deploy — changes at once. No model is asked, and it can be undone."
          : "Every page restyles at once. No model is asked, and it can be undone."}
      </p>
      <Group title="Type">
        <Field label="Font pairing">
          <select className="select pv-select" value={draft.font_pair} onChange={(e) => put("font_pair", e.target.value)}>
            {app && <option value="">The app&apos;s own</option>}
            {theme.fonts.map((f) => (
              <option key={f.id} value={f.id}>
                {f.display === f.body ? f.display : `${f.display} + ${f.body}`}
              </option>
            ))}
          </select>
        </Field>
        {font && <p className="pv-hint">{font.label}</p>}
      </Group>
      <Group title="Colour">
        {(["primary", "accent"] as const).map((k) => (
          <Row key={k} label={k === "primary" ? "Brand" : "Accent"}>
            <div className="pv-color">
              <input
                type="color"
                className="pv-color-well"
                aria-label={`${k} colour`}
                value={hexOk(draft[k]) ? draft[k] : "#000000"}
                onChange={(e) => put(k, e.target.value)}
              />
              <input
                className="input pv-input mono"
                aria-label={`${k} hex`}
                value={draft[k]}
                maxLength={7}
                onChange={(e) => put(k, e.target.value.trim())}
                aria-invalid={!hexOk(draft[k])}
              />
            </div>
          </Row>
        ))}
        <Row label="Greys">
          <Seg value={draft.tint} options={theme.tints.map((t) => [t, t[0].toUpperCase() + t.slice(1)] as [string, string])} onChange={(v) => v && put("tint", v)} />
        </Row>
      </Group>
      <Group title="Shape">
        <Row label="Radius">
          <Seg value={draft.radius} options={theme.radii.map((t) => [t, t === "none" ? "0" : t] as [string, string])} onChange={(v) => v && put("radius", v)} />
        </Row>
        <Row label="Shadow">
          <Select value={draft.shadow || null} options={theme.shadows} placeholder={app ? "The app's own" : "—"} onChange={(v) => v && put("shadow", v)} />
        </Row>
        {theme.densities.length > 0 && (
          <Row label="Density">
            <Seg value={draft.density} options={theme.densities.map((t) => [t, t[0].toUpperCase() + t.slice(1)] as [string, string])} onChange={(v) => v && put("density", v)} />
          </Row>
        )}
      </Group>
      <div className="pv-pending-actions pv-site-actions">
        <button type="button" className="btn btn-sm btn-ghost" disabled={!changed.length || busy} onClick={() => setDraft(theme.current)}>
          Reset
        </button>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          disabled={!changed.length || busy || !hexOk(draft.primary) || !hexOk(draft.accent)}
          onClick={() => onTheme(Object.fromEntries(changed.map((k) => [k, draft[k]])))}
        >
          {busy && <span className="btn-spinner" aria-hidden="true" />}
          Apply to every page
        </button>
      </div>
    </div>
  );
}

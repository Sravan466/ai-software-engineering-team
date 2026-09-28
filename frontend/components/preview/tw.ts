/**
 * Reading and changing one property of an element's Tailwind classes.
 *
 * The mockup is styled with utilities, so "make the heading bigger" is "swap
 * `text-4xl` for `text-5xl`". Each property is a group of classes that exclude one
 * another. Setting one removes the group's classes at every breakpoint (`md:text-6xl`
 * would otherwise win on a desktop and the change would seem to do nothing) and
 * writes the base class, so the change shows at every size.
 *
 * Only classes in the group are touched. Anything else, including classes the
 * mockup's runtime adds (`is-active`), is left as it is.
 */

export type Group = {
  key: string;
  /** Matches a bare (unprefixed) class in the group, capturing its value. */
  re: RegExp;
  prefix: string;
};

const COLOR =
  "(?:ink|muted|primary|accent|on-primary|white|black|bg|surface|line|transparent|current|" +
  "(?:primary|accent|gray|slate|zinc|neutral|stone|blue|indigo|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|violet|purple|fuchsia|pink|rose)-(?:50|[1-9]00|950))(?:\\/\\d{1,3})?";

const g = (key: string, prefix: string, values: string): Group => ({
  key,
  prefix,
  re: new RegExp(`^${prefix.replace(/[-]/g, "\\-")}(${values})$`),
});

export const SIZES = ["xs", "sm", "base", "lg", "xl", "2xl", "3xl", "4xl", "5xl", "6xl", "7xl", "8xl", "9xl"];
export const WEIGHTS = ["light", "normal", "medium", "semibold", "bold", "extrabold", "black"];
export const LEADING = ["none", "tight", "snug", "normal", "relaxed", "loose"];
export const ALIGN = ["left", "center", "right", "justify"];
export const SPACE = ["0", "0.5", "1", "1.5", "2", "2.5", "3", "4", "5", "6", "8", "10", "12", "16", "20", "24", "32"];
export const RADII = ["none", "sm", "", "md", "lg", "xl", "2xl", "3xl", "full"];
export const SHADOWS = ["none", "sm", "", "md", "lg", "xl", "2xl"];
export const JUSTIFY = ["start", "center", "end", "between"];
export const ITEMS = ["start", "center", "end", "stretch"];

export const G = {
  size: g("size", "text-", SIZES.join("|")),
  weight: g("weight", "font-", "thin|extralight|light|normal|medium|semibold|bold|extrabold|black"),
  family: g("family", "font-", "display|sans|serif|mono"),
  leading: g("leading", "leading-", "none|tight|snug|normal|relaxed|loose|\\d+"),
  align: g("align", "text-", ALIGN.join("|")),
  color: g("color", "text-", COLOR),
  bg: g("bg", "bg-", COLOR),
  radius: { key: "radius", prefix: "rounded", re: /^rounded(?:-(none|sm|md|lg|xl|2xl|3xl|full))?$/ } as Group,
  shadow: { key: "shadow", prefix: "shadow", re: /^shadow(?:-(none|sm|md|lg|xl|2xl|inner))?$/ } as Group,
  border: { key: "border", prefix: "border", re: /^border(?:-(0|2|4|8))?$/ } as Group,
  borderColor: g("borderColor", "border-", COLOR),
  gap: g("gap", "gap-", "[\\d.]+|px"),
  display: { key: "display", prefix: "", re: /^(block|inline-block|inline|flex|inline-flex|grid|hidden)$/ } as Group,
  direction: g("direction", "flex-", "row|col|row-reverse|col-reverse"),
  justify: g("justify", "justify-", "start|center|end|between|around|evenly"),
  items: g("items", "items-", "start|center|end|stretch|baseline"),
};

/** A class without its variant prefixes: `md:hover:text-5xl` → `text-5xl`. */
export function bare(cls: string): string {
  const at = cls.lastIndexOf(":");
  return at === -1 ? cls : cls.slice(at + 1);
}

export function split(classes: string): string[] {
  return classes.split(/\s+/).filter(Boolean);
}

/** The group's current base value (`"5xl"` for `text-5xl`), or null. */
export function read(classes: string, group: Group): string | null {
  for (const c of split(classes)) {
    if (c.includes(":")) continue;
    const m = group.re.exec(c);
    if (m) return m[1] ?? "";
  }
  return null;
}

/** Whether any breakpoint variant of the group is present (`md:text-6xl`). */
export function hasVariants(classes: string, group: Group): boolean {
  return split(classes).some((c) => c.includes(":") && group.re.test(bare(c)));
}

export function classFor(group: Group, value: string | null): string | null {
  if (value === null) return null;
  if (group.key === "radius" || group.key === "shadow" || group.key === "border") {
    return value === "" ? group.prefix : `${group.prefix}-${value}`;
  }
  return `${group.prefix}${value}`;
}

/** The classes after setting the group to `value` (null clears it). */
export function set(classes: string, group: Group, value: string | null): string[] {
  const kept = split(classes).filter((c) => {
    const b = bare(c);
    // Hover/focus variants are states, not sizes: keep them.
    if (c.includes(":") && /(^|:)(hover|focus|active|focus-visible|group-hover|disabled):/.test(c)) return true;
    return !group.re.test(b);
  });
  const next = classFor(group, value);
  return next ? [...kept, next] : kept;
}

/** The smallest add/remove that turns `before` into `after`. */
export function diff(before: string[] | string, after: string[] | string): { add: string[]; remove: string[] } {
  const b = typeof before === "string" ? split(before) : before;
  const a = typeof after === "string" ? split(after) : after;
  return { add: a.filter((c) => !b.includes(c)), remove: b.filter((c) => !a.includes(c)) };
}

// ── spacing: two axes, written as px-/py- (or mx-/my-) ──────────────────────
type Axis = "x" | "y";

/** The current value on one axis, reading `p-4` as both. */
export function readSpace(classes: string, kind: "p" | "m", axis: Axis): string | null {
  let both: string | null = null;
  let one: string | null = null;
  const re = new RegExp(`^(-?)${kind}([xytrbl]?)-([\\d.]+|px|auto)$`);
  for (const c of split(classes)) {
    if (c.includes(":")) continue;
    const m = re.exec(c);
    if (!m) continue;
    const [, neg, side, v] = m;
    const value = (neg ? "-" : "") + v;
    if (side === "") both = value;
    else if (side === axis) one = value;
    else if ((axis === "x" && (side === "l" || side === "r")) || (axis === "y" && (side === "t" || side === "b"))) one = one ?? value;
  }
  return one ?? both;
}

/** Classes after setting one axis; the other axis keeps its value. */
export function setSpace(classes: string, kind: "p" | "m", axis: Axis, value: string | null): string[] {
  const other: Axis = axis === "x" ? "y" : "x";
  const keepOther = readSpace(classes, kind, other);
  const re = new RegExp(`^-?${kind}[xytrbl]?-([\\d.]+|px|auto)$`);
  const kept = split(classes).filter((c) => {
    if (c.includes(":") && /(^|:)(hover|focus|active):/.test(c)) return true;
    return !re.test(bare(c));
  });
  const out = [...kept];
  if (keepOther !== null) out.push(`${kind}${other}-${keepOther}`);
  if (value !== null) out.push(`${kind}${axis}-${value}`);
  return out;
}

export function step<T>(list: T[], current: T | null, by: number, fallback: T): T {
  const at = current === null ? list.indexOf(fallback) : list.indexOf(current);
  const from = at === -1 ? list.indexOf(fallback) : at;
  return list[Math.max(0, Math.min(list.length - 1, from + by))];
}

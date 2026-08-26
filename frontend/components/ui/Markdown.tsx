import type { ReactNode } from "react";

/**
 * A small, dependency-free renderer for the markdown the agents emit.
 *
 * The build view's whole job is to let a person READ what an agent produced
 * before approving it, so this covers what those documents actually contain:
 * headings, paragraphs, ordered/unordered lists, fenced and inline code,
 * blockquotes, rules, tables, links and emphasis.
 *
 * It builds React elements rather than an HTML string, so model output is never
 * injected as markup — and link hrefs are additionally restricted to safe
 * schemes so a generated `javascript:` URL can't become a live control.
 */

const SAFE_HREF = /^(https?:|mailto:|#|\/)/i;

function safeHref(href: string): string | undefined {
  const trimmed = href.trim();
  return SAFE_HREF.test(trimmed) ? trimmed : undefined;
}

// ── inline: `code`, **bold**, *italic*, [text](href) ─────────────────────────
const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)\s]+\))/g;

function inline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  INLINE.lastIndex = 0;

  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${keyPrefix}-${m.index}`;

    if (tok.startsWith("`")) {
      nodes.push(<code key={key}>{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith("**")) {
      nodes.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("*")) {
      nodes.push(<em key={key}>{tok.slice(1, -1)}</em>);
    } else {
      const split = tok.indexOf("](");
      const label = tok.slice(1, split);
      const href = safeHref(tok.slice(split + 2, -1));
      nodes.push(
        href ? (
          <a key={key} href={href} target="_blank" rel="noopener noreferrer">
            {label}
          </a>
        ) : (
          label
        ),
      );
    }
    last = m.index + tok.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// ── block level ──────────────────────────────────────────────────────────────
function cells(row: string): string[] {
  return row
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((c) => c.trim());
}

export default function Markdown({ children }: { children: string }) {
  const lines = (children || "").replace(/\r\n?/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;
  const k = () => `b${key++}`;

  while (i < lines.length) {
    const line = lines[i];

    // blank
    if (!line.trim()) {
      i++;
      continue;
    }

    // fenced code
    const fence = line.match(/^\s*```(\w*)/);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
      i++; // closing fence
      out.push(
        <pre key={k()}>
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // horizontal rule
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      out.push(<hr key={k()} />);
      i++;
      continue;
    }

    // heading
    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) {
      const level = Math.min(h[1].length, 4);
      const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4";
      out.push(<Tag key={k()}>{inline(h[2].trim(), `h${i}`)}</Tag>);
      i++;
      continue;
    }

    // table (header row + separator row)
    if (line.includes("|") && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1] || "")) {
      const head = cells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(cells(lines[i++]));
      }
      out.push(
        <table key={k()}>
          <thead>
            <tr>
              {head.map((c, n) => (
                <th key={n}>{inline(c, `th${n}`)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, n) => (
              <tr key={n}>
                {r.map((c, m) => (
                  <td key={m}>{inline(c, `td${n}-${m}`)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }

    // blockquote
    if (/^\s*>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        body.push(lines[i++].replace(/^\s*>\s?/, ""));
      }
      out.push(<blockquote key={k()}>{inline(body.join(" "), `q${i}`)}</blockquote>);
      continue;
    }

    // lists
    const bullet = /^\s*[-*+]\s+/;
    const numbered = /^\s*\d+[.)]\s+/;
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line);
      const marker = ordered ? numbered : bullet;
      const items: string[] = [];
      while (i < lines.length && marker.test(lines[i])) {
        let item = lines[i++].replace(marker, "");
        // fold plain continuation lines into the item
        while (i < lines.length && lines[i].trim() && !marker.test(lines[i]) && !/^\s*(#{1,6}\s|```|>)/.test(lines[i])) {
          item += " " + lines[i++].trim();
        }
        items.push(item);
      }
      const List = ordered ? "ol" : "ul";
      out.push(
        <List key={k()}>
          {items.map((it, n) => (
            <li key={n}>{inline(it, `li${n}`)}</li>
          ))}
        </List>,
      );
      continue;
    }

    // paragraph — gather until a blank line or the start of another block
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*(#{1,6}\s|```|>|[-*+]\s|\d+[.)]\s)/.test(lines[i])
    ) {
      para.push(lines[i++].trim());
    }
    if (para.length) out.push(<p key={k()}>{inline(para.join(" "), `p${i}`)}</p>);
  }

  return <div className="md">{out}</div>;
}

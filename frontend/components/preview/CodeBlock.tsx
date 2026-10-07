"use client";

import { useEffect, useRef } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import vscDarkPlus from "react-syntax-highlighter/dist/esm/styles/prism/vsc-dark-plus";

// Map a file path / artifact language tag to a Prism language id (VS Code Dark+ palette).
const EXT_LANG: Record<string, string> = {
  js: "jsx",
  jsx: "jsx",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "tsx",
  py: "python",
  rb: "ruby",
  go: "go",
  rs: "rust",
  java: "java",
  php: "php",
  json: "json",
  yml: "yaml",
  yaml: "yaml",
  toml: "toml",
  md: "markdown",
  mdx: "markdown",
  css: "css",
  scss: "scss",
  html: "markup",
  htm: "markup",
  xml: "markup",
  svg: "markup",
  vue: "markup",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  sql: "sql",
  graphql: "graphql",
  gql: "graphql",
};

function detectLang(path: string, tag?: string): string {
  const base = (path.split("/").pop() || "").toLowerCase();
  if (base.includes("dockerfile")) return "docker";
  if (base === "makefile") return "makefile";
  if (base.startsWith(".env")) return "bash";
  const ext = base.includes(".") ? base.split(".").pop()! : "";
  if (ext && EXT_LANG[ext]) return EXT_LANG[ext];
  const t = (tag || "").toLowerCase();
  if (EXT_LANG[t]) return EXT_LANG[t];
  // Accept a tag that's already a prism id.
  if (/^[a-z]+$/.test(t)) return t;
  return "text";
}

export default function CodeBlock({
  code,
  path,
  tag,
  mark,
}: {
  code: string;
  path: string;
  tag?: string;
  /** A line a security finding points at (#77): tinted, and scrolled to the middle. */
  mark?: number | null;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!mark) return;
    const pre = box.current?.querySelector("pre");
    const line = box.current?.querySelector<HTMLElement>('[data-mark="true"]');
    if (!pre || !line) return;
    // Inside the code's own scroll box only: the page is already where it should be.
    const offset = line.getBoundingClientRect().top - pre.getBoundingClientRect().top;
    pre.scrollTop = Math.max(pre.scrollTop + offset - pre.clientHeight / 2, 0);
  }, [mark, code]);

  return (
    <div ref={box} style={{ display: "contents" }}>
      <SyntaxHighlighter
        language={detectLang(path, tag)}
        style={vscDarkPlus}
        showLineNumbers
        wrapLines={Boolean(mark)}
        lineProps={
          mark
            ? (n: number) =>
                (n === mark
                  ? { "data-mark": "true", "aria-current": "true", className: "code-line-mark", style: { display: "block" } }
                  : { style: { display: "block" } }) as React.HTMLProps<HTMLElement>
            : undefined
        }
        customStyle={{
          margin: 0,
          padding: "16px 14px",
          maxHeight: 560,
          overflow: "auto",
          background: "var(--bg-sunken)",
          fontSize: "var(--t-xs)",
          lineHeight: 1.65,
        }}
        lineNumberStyle={{
          minWidth: "2.6em",
          paddingRight: "1em",
          color: "var(--ink-4)",
          userSelect: "none",
        }}
        codeTagProps={{
          style: {
            fontFamily: "var(--font-mono), ui-monospace, SFMono-Regular, Menlo, monospace",
          },
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}
